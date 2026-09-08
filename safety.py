#!/usr/bin/env python3
"""
safety.py - the reactive safety layer. Phase 04 of the plan in CLAUDE.md.

This is the fast loop: it polls the sensors at 10 to 20 Hz, holds veto power
over every command, and stops the car itself the moment something goes wrong.
It contains no intelligence and never touches the network. That is deliberate.
The model is one to four seconds behind reality, and at 200 mm per second the
car covers 200 to 800 mm while the model thinks, so the model cannot be the
thing that avoids collisions. This can.

    from elegoo import Car, CarConfig, Direction
    from safety import SafetyLoop

    with Car(CarConfig(host="192.168.1.211")) as car:
        with SafetyLoop(car) as guard:
            verdict = guard.move(Direction.FORWARD, speed=120, ms=400)
            print(verdict)     # allowed, or refused with a reason

Every verdict carries a reason, refusals included, because the architecture
feeds the outcome of each action back to the model on the next turn. "Refused,
obstacle at 18 cm" is information the model can act on. A silent no is not.

WHAT THIS LAYER CANNOT DO, and why the limits are where they are:

* **A far reading is not proof the way is clear.** The ultrasonic saturates at
  raw 150, and that means "at least about 195 cm" or "no echo came back at
  all", with no way to tell which. A dead sensor and an open corridor are the
  same number. So this layer proves the sensor works once at startup, and
  treats a saturated reading as clear only after that. If the sensor dies
  mid-run it will read clear and this layer will not catch it.
* **Nothing watches behind.** There is one range finder and it faces where the
  servo points. Reversing is bounded by duration and nothing else.
* **The cliff stop is thin.** Line sensor channel 2 separates floor from no
  floor by a wide margin, but only 218 counts separate no floor from a *dark*
  floor. Calibrate against the actual floors before trusting it, and expect
  false stops on very dark surfaces rather than missed edges.
* **Motion is open loop.** There are no encoders. This layer bounds how long
  the car moves, not how far.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from elegoo import (Car, CarError, Direction, NotConnected, ReplyTimeout,
                    CLIFF_CHANNEL, CLIFF_THRESHOLD, RAW_CEILING, RAW_TO_CM,
                    DRIVE_MM_PER_MS, DRIVE_SPEED)

__all__ = ["SafetyLoop", "SafetyConfig", "Verdict", "Reading", "Reason"]

# Shorter than this and the car has barely started before it stops, which reads
# as progress in a log and is not. A move clamped below it is refused instead.
MIN_USEFUL_MOVE_MS = 120


class Reason:
    """Why an action was refused, or why the car was stopped.

    Strings rather than an enum because these go into a model prompt verbatim.
    """
    OK = "ok"
    TOO_CLOSE = "obstacle too close"
    OVER_EDGE = "no floor under the front sensors"
    SENSOR_STALE = "no fresh sensor reading"
    SENSOR_UNPROVEN = "range finder has not produced a real reading this session"
    NOT_RUNNING = "safety loop is not running"
    DISCONNECTED = "lost the car"
    TOO_FAST = "speed above the configured limit"
    TOO_LONG = "duration above the configured limit"
    NO_ROOM = "not enough room to move"


@dataclass
class Reading:
    """One sweep of the sensors."""

    at: float
    distance_raw: int
    # Channels the poll did not read are None. Only CLIFF_CHANNEL is read on
    # the hot path, to stay inside the serial link's budget.
    line: Tuple[Optional[int], Optional[int], Optional[int]]

    @property
    def distance_cm(self) -> float:
        """Centimetres, or inf when the reading is saturated.

        `inf` is right for a `d < threshold` comparison and wrong as evidence
        of clearance. See the module docstring.
        """
        if self.distance_raw >= RAW_CEILING:
            return float("inf")
        return self.distance_raw * RAW_TO_CM

    @property
    def at_ceiling(self) -> bool:
        return self.distance_raw >= RAW_CEILING

    @property
    def over_edge(self) -> bool:
        return self.line[CLIFF_CHANNEL] > CLIFF_THRESHOLD

    def __str__(self) -> str:
        d = "far" if self.at_ceiling else f"{self.distance_cm:.0f} cm"
        shown = ", ".join("-" if v is None else str(v) for v in self.line)
        return (f"range {d} (raw {self.distance_raw}), line ({shown})"
                f"{', OVER EDGE' if self.over_edge else ''}")


@dataclass
class Verdict:
    """The result of asking for an action."""

    allowed: bool
    reason: str
    reading: Optional[Reading] = None
    interrupted: bool = False        # allowed, started, then stopped early
    # What the move was actually run for, when that differs from what was
    # asked. None means it ran as requested.
    ms: Optional[int] = None

    def __str__(self) -> str:
        if self.allowed and not self.interrupted:
            return f"allowed ({self.reading})"
        if self.interrupted:
            return f"interrupted: {self.reason} ({self.reading})"
        return f"refused: {self.reason} ({self.reading})"


@dataclass
class SafetyConfig:
    # 8 Hz, and only two queries per poll. This is a bandwidth budget, not a
    # preference. The link to the UNO is 9600 baud, so 960 bytes a second. One
    # query and its reply is about 33 bytes, so:
    #
    #   distance + all three line channels, at 10 Hz  = 1320 B/s = 138% of link
    #   distance + the cliff channel only,  at  8 Hz  =  528 B/s =  55% of link
    #
    # The first of those saturates the line. Commands then queue behind the
    # polling, replies arrive late, and a timed move times out while the car
    # sits there. That is what "it turns but will not drive" turned out to be.
    #
    # Only channel 2 is read because only channel 2 is used: `over_edge` reads
    # CLIFF_CHANNEL and nothing else looks at the other two. Set
    # `poll_all_line_channels` if you want the full triple for diagnostics, and
    # drop the rate if you do.
    poll_hz: float = 8.0
    poll_all_line_channels: bool = False

    # Hard stop distance. 15 cm, lowered from 25 on 2026-09-07 because the car
    # was refusing to approach anything closer than about 40 cm and that made
    # it useless for a task that ends at an object.
    #
    # The arithmetic behind 15: at 0.55 mm/ms the car covers about 7 cm between
    # polls at 8 Hz, plus roughly 3 cm of command round trip, so reaction
    # distance is about 10 cm. That leaves 5 cm of margin, which is thin, and
    # it is only safe alongside `clamp_move_to_clearance` below. Without the
    # clamp a single 800 ms burst covers 44 cm and can cross the whole gap
    # between two polls, which no threshold can save you from.
    stop_cm: float = 15.0

    # Shorten a forward move so it cannot ask to travel further than the space
    # in front of it. This is what actually makes a close stop distance safe:
    # rather than relying on the poll thread to catch the car in time, the move
    # is bounded before it starts by the clearance that was measured just
    # before it. The poll thread stays as the backstop for anything that
    # appears mid-move.
    #
    # The model is told when a move was shortened and by how much, because a
    # silently truncated action would make its own dead reckoning wrong.
    clamp_move_to_clearance: bool = True

    # Bounds on what any single action may ask for. Short actions are the whole
    # design: a bad decision then costs a fraction of a second of motion, and
    # re-observing after each one absorbs the open loop drift.
    # 180, because that is the ceiling the UNO firmware itself imposes on
    # straight line motion, so anything above it is clamped there anyway. This
    # was 160 until 2026-09-07, which was below the speed the car actually
    # needs to start moving forward, so the safety layer refused every forward
    # move. A cap set below the working range is not a safety measure, it is
    # a robot that cannot drive.
    max_speed: int = 180
    max_move_ms: int = 800
    max_reverse_ms: int = 600

    # A reading older than this is not evidence of anything.
    stale_after_s: float = 1.0

    # Require the range finder to return one non-saturated reading before any
    # forward motion is allowed. Cheap proof that it is alive this session.
    require_sensor_proof: bool = True

    # Called with (Reading, reason) whenever the loop stops the car itself.
    on_veto: Optional[Callable[[Reading, str], None]] = None


class SafetyLoop:
    def __init__(self, car: Car, config: Optional[SafetyConfig] = None):
        self.car = car
        self.config = config or SafetyConfig()
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        # Lets the poll thread be held off without shutting the loop down, so
        # going stale can be tested for what it is rather than colliding with
        # the "loop is not running" check and passing for the wrong reason.
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self._reading: Optional[Reading] = None
        self._sensor_proven = False
        self._poll_errors = 0
        self._vetoes: List[Tuple[float, str]] = []
        # Set while a commanded move is in flight, so the poll thread knows it
        # has something to interrupt rather than merely something to refuse.
        self._moving = threading.Event()
        # Which way the in-flight move is going. The interrupt has to know:
        # an obstacle ahead is a reason to stop going forward and no reason at
        # all to stop reversing away from it. Without this, backing off is
        # impossible, which is exactly the escape we need most.
        self._moving_direction: Optional[Direction] = None
        self._interrupted_reason: Optional[str] = None

    # ---------- lifecycle ----------

    def start(self) -> "SafetyLoop":
        if self._running.is_set():
            return self
        self._running.set()
        self._thread = threading.Thread(target=self._poll, name="safety",
                                        daemon=True)
        self._thread.start()
        # Do not return until there is something to make decisions with.
        deadline = time.monotonic() + 3.0
        while self._reading is None and time.monotonic() < deadline:
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=2.0)
        try:
            self.car.stop()
        except CarError:
            pass

    def __enter__(self) -> "SafetyLoop":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------- state ----------

    @property
    def reading(self) -> Optional[Reading]:
        with self._lock:
            return self._reading

    @property
    def running(self) -> bool:
        return self._running.is_set()

    @property
    def sensor_proven(self) -> bool:
        """True once the range finder has returned a real distance."""
        return self._sensor_proven

    @property
    def vetoes(self) -> List[Tuple[float, str]]:
        with self._lock:
            return list(self._vetoes)

    # ---------- the loop ----------

    def _poll(self) -> None:
        interval = 1.0 / max(self.config.poll_hz, 1.0)
        while self._running.is_set():
            started = time.monotonic()
            if self._paused.is_set():
                time.sleep(interval)
                continue
            try:
                raw = self.car.distance()
                if self.config.poll_all_line_channels:
                    line = self.car.line_sensors()
                else:
                    # Only the channel the cliff stop actually consults. The
                    # other two are left as None rather than guessed at.
                    cliff = self.car.line_sensor(CLIFF_CHANNEL)
                    line = tuple(cliff if i == CLIFF_CHANNEL else None
                                 for i in range(3))
                reading = Reading(at=time.monotonic(), distance_raw=raw,
                                  line=line)
                with self._lock:
                    self._reading = reading
                    if not reading.at_ceiling:
                        self._sensor_proven = True
                self._poll_errors = 0
                self._react(reading)
            except (ReplyTimeout, NotConnected, CarError):
                self._poll_errors += 1
                # A couple of dropped replies is normal. A run of them is not,
                # and the safe response to not knowing is to stop.
                if self._poll_errors >= 3:
                    self._panic("lost contact with the sensors")
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval - elapsed))

    def _react(self, reading: Reading) -> None:
        """Stop the car if what we just read is dangerous *for the direction
        it is currently going*.

        Both hazards are in front, so neither is a reason to interrupt a
        retreat. Interrupting one would trap the car against whatever it was
        backing away from.
        """
        if not self._moving.is_set():
            return
        if self._moving_direction in _BACKWARD:
            return
        danger = None
        if reading.over_edge:
            danger = Reason.OVER_EDGE
        elif not reading.at_ceiling and reading.distance_cm < self.config.stop_cm:
            danger = Reason.TOO_CLOSE
        if danger:
            self._panic(danger, reading)

    def _panic(self, reason: str, reading: Optional[Reading] = None) -> None:
        """Stop now, record why. Called from the poll thread."""
        self._interrupted_reason = reason
        self._moving.clear()
        try:
            # No waiting for replies here. This runs on the poll thread, the
            # motors are cut by the first frame `stop` sends, and blocking
            # afterwards on a congested 9600 baud link would only stall the
            # next reading.
            self.car.stop(wait=False)
        except CarError:
            pass
        with self._lock:
            self._vetoes.append((time.monotonic(), reason))
        if self.config.on_veto:
            self.config.on_veto(reading or self._reading, reason)

    # ---------- the veto ----------

    def check(self, direction: Direction, speed: int, ms: int) -> Verdict:
        """Would this action be allowed right now? No side effects."""
        cfg = self.config
        reading = self.reading

        if not self._running.is_set():
            return Verdict(False, Reason.NOT_RUNNING, reading)
        if not self.car.connected:
            return Verdict(False, Reason.DISCONNECTED, reading)
        if reading is None or time.monotonic() - reading.at > cfg.stale_after_s:
            return Verdict(False, Reason.SENSOR_STALE, reading)

        if speed > cfg.max_speed:
            return Verdict(False, f"{Reason.TOO_FAST}: {speed} > {cfg.max_speed}",
                           reading)
        limit = cfg.max_reverse_ms if direction in _BACKWARD else cfg.max_move_ms
        if ms > limit:
            return Verdict(False, f"{Reason.TOO_LONG}: {ms} ms > {limit} ms",
                           reading)

        # An edge under the front sensors blocks anything that is not a retreat.
        if reading.over_edge and direction not in _BACKWARD:
            return Verdict(False, Reason.OVER_EDGE, reading)

        if direction in _FORWARD:
            if cfg.require_sensor_proof and not self._sensor_proven:
                return Verdict(False, Reason.SENSOR_UNPROVEN, reading)
            if not reading.at_ceiling and reading.distance_cm < cfg.stop_cm:
                return Verdict(
                    False,
                    # One decimal, because rounding to whole centimetres
                    # produced "25 cm, limit 25 cm", which reads as a
                    # contradiction to anything, model or human, that has to
                    # act on it.
                    f"{Reason.TOO_CLOSE}: {reading.distance_cm:.1f} cm, "
                    f"limit {cfg.stop_cm:.1f} cm",
                    reading)

        return Verdict(True, Reason.OK, reading)

    def move(self, direction: Direction, speed: int = 120,
             ms: int = 400) -> Verdict:
        """Check, then execute, then watch until it finishes, then STOP.

        **The halt at the end is not belt and braces, it is the stop.** The
        original version relied on the firmware's own move timer and only added
        the ability to cut a move short. That assumption has failed twice on
        this car: once when straight motion moved to command 1, which has no
        timer at all, and again on 2026-09-07 when a command 2 turn kept
        running while the calling script sat at an input prompt, until the car
        was switched off at the mains.

        Whatever the firmware does or does not do with its timer, this method
        now owns the stop, and it is in a `finally` so that an exception,
        an interrupt or a refusal cannot skip it. The brief's rule is that
        anything which starts the motors owns the stop; this is where that
        rule belongs, because every caller goes through here.
        """
        verdict = self.check(direction, speed, ms)
        if not verdict.allowed:
            return verdict

        asked_ms = ms
        ms = self._clamp_to_clearance(direction, speed, ms, verdict.reading)
        if ms is None:
            return Verdict(False, f"{Reason.NO_ROOM}: not enough clear space "
                                  f"ahead for a move worth making",
                           verdict.reading)

        self._interrupted_reason = None
        self._moving_direction = direction
        self._moving.set()
        try:
            try:
                self.car.move(direction, speed=speed, ms=ms)
            except CarError as exc:
                return Verdict(False, f"{Reason.DISCONNECTED}: {exc}",
                               verdict.reading)

            # Wait out the move, letting the poll thread interrupt if needed.
            deadline = time.monotonic() + ms / 1000.0
            while time.monotonic() < deadline and self._moving.is_set():
                time.sleep(0.01)
        finally:
            self._moving.clear()
            self._moving_direction = None
            try:
                self.car.halt()
            except CarError:
                pass

        if self._interrupted_reason:
            return Verdict(True, self._interrupted_reason, self.reading,
                           interrupted=True, ms=ms)
        if ms < asked_ms:
            return Verdict(True, f"shortened from {asked_ms} to {ms} ms: "
                                 f"that is as far as the clear space ahead "
                                 f"allows", self.reading, ms=ms)
        return Verdict(True, Reason.OK, self.reading, ms=ms)

    def _clamp_to_clearance(self, direction: Direction, speed: int, ms: int,
                            reading: Optional[Reading]) -> Optional[int]:
        """Shorten a forward move to the space actually in front of the car.

        This is what makes a 15 cm stop distance safe. Without it the car can
        ask for 800 ms, cover 44 cm, and cross the entire gap between two polls
        of a sensor running at 8 Hz; no threshold survives that. With it, the
        move is bounded before it starts by clearance that was measured
        moments earlier, and the poll thread is left to catch only what appears
        during the move.

        Backwards moves are not clamped: nothing watches behind, so there is no
        clearance figure to clamp against. They are bounded by duration alone,
        which is stated in the module docstring.

        A saturated reading gives no distance to work from, so it is left
        alone; `check` has already established the sensor is alive.
        """
        if not self.config.clamp_move_to_clearance:
            return ms
        if direction is not Direction.FORWARD:
            return ms
        if reading is None or reading.at_ceiling:
            return ms

        usable_cm = reading.distance_cm - self.config.stop_cm
        if usable_cm <= 0:
            return ms          # `check` refuses this case already
        # Rate is measured at the drive speed, so scale for anything slower.
        rate = DRIVE_MM_PER_MS * (speed / float(DRIVE_SPEED))
        allowed = int(usable_cm * 10.0 / max(rate, 0.05))
        # If there is not room for a move worth making, refuse. The first
        # version of this clamped up to a 120 ms floor instead, reasoning that
        # a twitch was better than nothing. It is not: with 0.6 cm of usable
        # space that floor drove 7 cm, straight through the stop distance. A
        # minimum that overrides the limit is not a minimum, it is a hole in
        # the limit.
        if allowed < MIN_USEFUL_MOVE_MS:
            return None
        return min(ms, allowed)

    def emergency_stop(self, reason: str = "asked to stop") -> None:
        self._panic(reason)

    def pause_polling(self) -> None:
        """Stop refreshing readings without shutting the loop down, so that
        going stale can be exercised on its own."""
        self._paused.set()

    def resume_polling(self) -> None:
        self._paused.clear()

    # ---------- reporting ----------

    def describe(self) -> str:
        """One line of state, for feeding to the model alongside a frame."""
        r = self.reading
        if r is None:
            return "sensors: no reading yet"
        age = time.monotonic() - r.at
        proof = "" if self._sensor_proven else ", range finder unproven"
        return (f"sensors: {r} (age {age*1000:.0f} ms){proof}")


_FORWARD = {Direction.FORWARD, Direction.FORWARD_LEFT, Direction.FORWARD_RIGHT}
_BACKWARD = {Direction.BACKWARD, Direction.BACKWARD_LEFT,
             Direction.BACKWARD_RIGHT}
