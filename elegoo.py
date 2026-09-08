#!/usr/bin/env python3
"""
elegoo.py - client library for the Elegoo Smart Robot Car V4.0.

Phase 02 of the plan in CLAUDE.md. Wraps the raw TCP command socket on port
100, the heartbeat that keeps it open, the reply format, and the still frame
grabber on port 80, behind something typed and safe enough to build an agent
loop on top of.

Standard library only, so it runs on whichever machine happens to be joined to
the car.

    from elegoo import Car, Direction

    with Car(host="192.168.4.1") as car:
        print(car.distance())
        car.move(Direction.FORWARD, speed=120, ms=400)
        car.capture_to("frame.jpg")

Entering the context manager connects, clears the stock behaviours and starts
the heartbeat responder. Leaving it stops the car, on the way out of an
exception as well as a clean return.

What this module deliberately does not do is decide anything. There is no
obstacle avoidance and no veto here. That is the fast loop in phase 04, and it
belongs on top of this, not inside it.

Two things in here rest on observed behaviour rather than documentation, both
recorded in CLAUDE.md:

* Replies are not JSON. They are `{<tag>_<value>}` tokens.
* The socket dies after roughly 3.5 s without traffic. The responder thread
  echoes the car's own heartbeat back, which is what holds it open, and which
  also means a wedged host stops the car within about three seconds.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Optional, Tuple

__all__ = [
    "Car", "CarConfig", "Direction", "Reply",
    "CarError", "NotConnected", "ReplyTimeout",
]


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class CarError(Exception):
    """Anything this module raises."""


class NotConnected(CarError):
    """The socket is closed, or was never opened."""


class ReplyTimeout(CarError):
    """The car did not answer a tagged command in time."""


# --------------------------------------------------------------------------
# protocol vocabulary
# --------------------------------------------------------------------------

class Direction(IntEnum):
    FORWARD = 1
    BACKWARD = 2
    LEFT = 3
    RIGHT = 4
    FORWARD_LEFT = 5
    BACKWARD_LEFT = 6
    FORWARD_RIGHT = 7
    BACKWARD_RIGHT = 8
    STOP = 9


class N(IntEnum):
    """Command numbers, as sent in the JSON `N` field."""

    MOTOR_DIRECT = 1
    TIMED_MOVE = 2
    CONTINUOUS_MOVE = 3
    TANK = 4
    SERVO = 5
    LEDS = 8
    ULTRASONIC = 21
    LINE = 22
    GROUND = 23
    STOP = 100
    BUILTIN_MODE = 101
    PROGRAM_MODE = 110


HEARTBEAT = "{Heartbeat}"

# Command 100 answers with a bare `{ok}` and ignores the tag, so a tagged stop
# waits for a reply that never comes. Observed 2026-09-06: every other command
# in the phase 01 capture echoed its tag, but 100 was only ever sent untagged
# and always came back as `{ok}`. Commands listed here are sent without an `H`
# and matched against the next untagged frame instead.
UNTAGGED_REPLY_COMMANDS = frozenset({int(N.STOP)})

# Ultrasonic command 21 does not return centimetres. Calibrated 2026-09-06 at
# 10, 20, 30, 40, 60, 80 and 100 cm: readings 8, 15, 22, 32, 45, 62, 77. Least
# squares over all seven gives raw = 0.7714 * cm - 0.18, so cm = raw * 1.296,
# residuals within 1.3 counts. The intercept is zero within the resolution, so
# it is a scale error rather than a datum error.
RAW_TO_CM = 1.296

# How many frames to throw away before keeping one. See `Car.capture`: the
# camera hands back the oldest queued frame, so the first one after any
# movement shows where the car used to be. Two drains a two-buffer queue.
# Set to 0 once the car is running firmware with CAMERA_GRAB_LATEST.
CAPTURE_DISCARD = 2

# The reading saturates here. Not a distance: it means at least ~195 cm or no
# echo at all, and the two are indistinguishable.
RAW_CEILING = 150

# Per channel midpoints between pale and dark, measured 2026-09-06. Darker
# reads higher. The channels have different baselines, so a single global
# threshold does not work.
LINE_MIDPOINTS = (826, 506, 707)

# Edge detection, measured 2026-09-06. With nothing underneath, the sensors
# saturate near the top of the scale; a real floor reads far lower.
#
#   floor            530 / 155 /   47
#   held in the air 1017 / 725 / 1020
#   over a table edge, front overhanging, identical to in the air
#
# The catch is the dark surface from the calibration run, 926 / 682 / 802,
# which sits alarmingly close to the in-air numbers. Per channel margin
# between "dark floor" and "no floor" is only 91, 43 and 218 counts, so
# channel 2 is the only one with room to work in, and even it is not generous.
CLIFF_CHANNEL = 2
CLIFF_THRESHOLD = 900  # between dark floor at 802 and no floor at 1020

# Tilt servo travel, D11. Re-measured 2026-09-06 after the camera was
# remounted; the earlier figures were for the old bracket and no longer apply.
# Higher is further down.
#
# Measured horizon position, as a fraction down the frame:
#
#   60 and below  87%   real mechanical stop, felt by hand
#   90            67%
#   100           58%
#   120 and above 49%   plateau, cause NOT yet known
#
# These are the FIRMWARE's limits, not the mechanism's. Sweeping the whole
# servo range on 2026-09-06 produced no grinding and no load at either end, the
# camera simply stopped responding past 60 and past 120, and the mechanism
# swings much further than that by hand. A clamp of 90 +/- 30 in the UNO
# firmware fits every observation. The published sources do not document it and
# the official repository's code does not match this unit's behaviour, so this
# rests on measurement.
#
# The clamp here exists to keep `last_tilt` honest rather than to protect the
# servo: the firmware already refuses to overdrive it. Without the clamp,
# commanding 150 would record an aim of 150 while the camera sat at 120.
#
# To reach further down, either re-seat the servo horn a few teeth so this
# 60 to 120 window maps onto a lower part of the arc, or widen the clamp in the
# UNO firmware, which is worth bundling with the battery voltage command.
TILT_MIN = 60
TILT_MAX = 120
TILT_SCAN = 80      # survey: as near horizontal as the current mount gives
TILT_DRIVING = 105  # the floor just ahead, for the last part of an approach

# Speeds that actually work, see the note above move(). Driving needs a bigger
# number than turning, because the straight line correction keeps interrupting
# the motors to read the gyro and turning does not go through that path at all.
DRIVE_SPEED = 165
# Turning is done with command 4, the tank command: one side at this speed and
# the other at zero. See `turn_tank`. Command 2's Left and Right stopped
# turning this car at all, at every speed tried.
TURN_SPEED = 170

# Measured 2026-09-07 with `turn_test.py`: four 400 ms tank bursts at speed 170
# rotated the car 270 degrees, symmetric in both directions. So about 68
# degrees per 400 ms burst, and a right angle is roughly 530 ms.
#
# Open loop, like everything else here, and it will drift with battery and
# floor. Re-measure rather than trusting it to a second decimal place.
TURN_DEG_PER_MS = 0.17

# Measured the same day with `calibrate_motion.py --direct`: 22 cm per 400 ms
# burst at speed 170. An earlier run on a half-charged pack gave 17.6 cm, so
# the 25% spread between two runs on the same floor is the honest error bar.
DRIVE_MM_PER_MS = 0.55
DRIVE_SPEED_MAX = 180   # the firmware clamps straight motion here

# Pan travel, same firmware clamp of 90 plus or minus 30. Whether 90 is
# genuinely straight ahead has not been verified; the trims below exist so it
# can be corrected once by eye without editing every call site.
PAN_MIN = 60
PAN_MAX = 120
PAN_CENTRE = 90

# Where the head goes on connect. Nominally "horizontal".
#
# This was 120, from measurements on 2026-09-06 that put the horizon at 49% of
# the frame there, with 87% at tilt 60. Those numbers no longer describe this
# mount. On 2026-09-07 the frames were consistently aimed too low, and the
# model, given a free choice, drove the tilt down to 60 to 65 and kept it
# there, which is exactly what it would do if 120 were pointing at the floor.
#
# The brief warned this would happen: the servo horn can slip, and the
# angle-to-aim mapping is not stable across knocks. Treat tilt as relative and
# re-check by eye rather than trusting any table.
#
# 85 is a starting point, not a calibration. To find the real value, run
#     python elegoo_probe.py --tilt-range
# which steps the tilt and saves a frame at each stop, then pick the one whose
# horizon sits across the middle and set it here.
TILT_LEVEL = 85

# Correct these once if the head does not actually point where it should.
# Positive pan trim aims further right, positive tilt trim aims further down.
PAN_TRIM = 0
TILT_TRIM = 0


@dataclass(frozen=True)
class Reply:
    """One `{<tag>_<value>}` frame from the car.

    `value` is kept as the raw string because the firmware is inconsistent
    about what it returns: `ok` for actions, `true` or `false` for predicates,
    a bare number for readings. The accessors below convert on demand and
    complain loudly rather than guessing.
    """

    tag: Optional[str]
    value: str
    raw: str
    at: float

    @property
    def ok(self) -> bool:
        return self.value == "ok"

    def as_bool(self) -> bool:
        if self.value in ("true", "false"):
            return self.value == "true"
        raise CarError(f"expected true or false, got {self.raw!r}")

    def as_int(self) -> int:
        try:
            return int(self.value)
        except ValueError:
            raise CarError(f"expected a number, got {self.raw!r}") from None


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

@dataclass
class CarConfig:
    """The address lives here, not in the call sites. It changes once, at
    step 03 of the plan, when the camera module moves to station mode."""

    host: str = "192.168.4.1"
    command_port: int = 100
    http_port: int = 80
    stream_port: int = 81
    connect_timeout: float = 5.0
    reply_timeout: float = 1.5
    http_timeout: float = 5.0
    clear_stock_behaviours: bool = True

    # Drive straight with command 1 (direct motor control) rather than command
    # 2 (timed move). ON, and this time for a reason that is understood.
    #
    # It was on because command 2 forward produced no motion and no reply on
    # this car, which was read as Elegoo's yaw correction loop hanging on an
    # IMU that would not answer. That diagnosis was wrong. The car was running
    # on a 9 V PP3, which cannot supply four motors; command 2 works both ways
    # on 18650 cells, acknowledging a 600 ms move after 0.66 s exactly as the
    # firmware source says it should.
    #
    # Command 2's straight path runs Elegoo's yaw correction, which clamps its
    # low side to 10:
    #
    #     int L = (yaw_So - Yaw) * Kp + speed;   // clamped to 10
    #
    # This car's motors do not start below about 150, measured. So the moment
    # the correction asks for anything near its floor, that side of the car
    # stops dead and the car pivots instead of driving. That is the circling
    # seen at every speed from 100 to 180 on 2026-09-07, on a charged pack,
    # with the sensor polling off. Command 1 has no correction in it and drove
    # straight at every one of those speeds.
    #
    # What this costs is the firmware's move timer: under command 2 the car
    # stops itself when the clock expires even if the host dies, while under
    # command 1 the clock is a `time.sleep` here and a dead host leaves the car
    # running until the ESP32's heartbeat gives up about 3.5 s later. That is
    # the price of driving in a straight line, and durations are capped far
    # below 3.5 s. `halt()` is verified and the heartbeat still backstops it.
    straight_via_direct_motor: bool = True

    # Turn with command 4 (tank, one side at speed and the other at zero)
    # rather than command 2's Left and Right. Measured 2026-09-07: command 2
    # turns drove the car straight ahead instead of rotating it, at speed 120
    # and at 170, while the tank turn rotated it 68 degrees per 400 ms burst in
    # both directions. The car used to pivot in place on command 2, so this is
    # a regression somewhere in the car rather than a design choice, but the
    # tank turn is the better primitive regardless: it never asks the two sides
    # to run in opposite directions, which is the thing that has become
    # unreliable here.
    #
    # Like command 1, command 4 has no timer, so the host owns the stop and it
    # is in a `finally`.
    turn_via_tank: bool = True

    # Centre the head on connect, so every session starts from the same aim
    # rather than wherever the last one left it. Servos hold position when
    # power is removed, so without this the first frame of a run is a lottery.
    home_camera_on_connect: bool = True
    on_frame: Optional[Callable[[str], None]] = None  # raw frame hook

    @classmethod
    def from_env(cls, **overrides) -> "CarConfig":
        """Pick the address up from the environment if it is set.

        ELEGOO_HOST moves once, at plan step 03. ELEGOO_COMMAND_PORT is not
        expected to move on the car at all; it exists so a test harness can
        point any script at a mock without every script growing a flag.
        """
        env = {}
        if os.environ.get("ELEGOO_HOST"):
            env["host"] = os.environ["ELEGOO_HOST"]
        if os.environ.get("ELEGOO_COMMAND_PORT"):
            env["command_port"] = int(os.environ["ELEGOO_COMMAND_PORT"])
        env.update(overrides)
        return cls(**env)


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    reply: Optional[Reply] = None


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------

class Car:
    def __init__(self, config: Optional[CarConfig] = None, **overrides):
        self.config = config or CarConfig.from_env(**overrides)
        self._sock: Optional[socket.socket] = None
        self._alive = threading.Event()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._untagged_waiters = deque()
        # Tags of frames sent with wait off. Their replies still come back and
        # would otherwise be filed as unexplained traffic, which is exactly the
        # signal `untagged` exists to carry. Dropped on arrival instead.
        self._discard_tags = set()
        self._seq = 0
        self._reader_thread: Optional[threading.Thread] = None
        self.last_beat: Optional[float] = None
        # Replies nothing asked for. A diagnostic, so it is bounded: a long run
        # should not be able to grow it without limit.
        self.untagged = deque(maxlen=256)
        # Nothing can read a servo's position back, so the only record of where
        # the head is pointing is what we last asked for. None means unknown,
        # which is the honest state until something has been commanded: on
        # power up the firmware puts the servos wherever it likes.
        self.last_pan: Optional[int] = None
        self.last_tilt: Optional[int] = None

    # ---------- lifecycle ----------

    @property
    def connected(self) -> bool:
        return self._alive.is_set()

    def connect(self) -> "Car":
        cfg = self.config
        self._sock = socket.create_connection(
            (cfg.host, cfg.command_port), timeout=cfg.connect_timeout)
        self._sock.settimeout(None)
        self._alive.set()
        self._reader_thread = threading.Thread(
            target=self._reader, name="elegoo-reader", daemon=True)
        self._reader_thread.start()

        # If the opening handshake fails, tear the socket down rather than
        # leaving it dangling. The caller never entered the context manager,
        # so nothing else is going to close it, and the car takes one client
        # at a time: a leaked socket locks out the next attempt.
        try:
            # Known state first, then clear the stock behaviours so obstacle
            # avoidance is not competing for the motors.
            self.stop()
            if cfg.clear_stock_behaviours:
                self.program_mode()
            if cfg.home_camera_on_connect:
                self.home()
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        """Stop the car, then drop the socket. Safe to call twice."""
        if self._alive.is_set():
            try:
                self.stop(wait=False)
                time.sleep(0.2)
            except CarError:
                pass
        self._alive.clear()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._sock = None
        self._release_pending()

    def _release_pending(self) -> None:
        """Wake anything waiting on a reply that is never going to arrive."""
        with self._pending_lock:
            waiters = list(self._pending.values())
            waiters.extend(self._untagged_waiters)
            self._pending.clear()
            self._untagged_waiters.clear()
        for pending in waiters:
            pending.event.set()

    def _unregister(self, tag, pending) -> None:
        with self._pending_lock:
            if tag is not None:
                self._pending.pop(tag, None)
            else:
                try:
                    self._untagged_waiters.remove(pending)
                except ValueError:
                    pass

    def __enter__(self) -> "Car":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- the wire ----------

    def _reader(self) -> None:
        buf = b""
        sock = self._sock
        while self._alive.is_set():
            try:
                chunk = sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            # Frames are brace delimited. Keep any partial tail for next time.
            while b"{" in buf and b"}" in buf:
                start = buf.index(b"{")
                end = buf.index(b"}", start)
                raw = buf[start:end + 1].decode(errors="replace")
                buf = buf[end + 1:]
                self._on_frame(raw)
        self._alive.clear()
        self._release_pending()

    def _on_frame(self, raw: str) -> None:
        if self.config.on_frame:
            self.config.on_frame(raw)

        if "Heartbeat" in raw:
            self.last_beat = time.monotonic()
            # This echo is the whole reason the connection stays up.
            try:
                self._write(raw)
            except CarError:
                pass
            return

        body = raw.strip("{}")
        tag, sep, value = body.partition("_")
        reply = Reply(tag=tag if sep else None,
                      value=value if sep else body,
                      raw=raw, at=time.monotonic())

        if reply.tag is None:
            # Untagged {ok}, which is how command 100 answers. Hand it to the
            # oldest waiter expecting one, in send order.
            with self._pending_lock:
                waiter = (self._untagged_waiters.popleft()
                          if self._untagged_waiters else None)
            if waiter is not None:
                waiter.reply = reply
                waiter.event.set()
            else:
                self.untagged.append(reply)
            return

        with self._pending_lock:
            pending = self._pending.pop(reply.tag, None)
            discard = pending is None and reply.tag in self._discard_tags
            if discard:
                self._discard_tags.discard(reply.tag)
        if pending is not None:
            pending.reply = reply
            pending.event.set()
        elif not discard:
            self.untagged.append(reply)

    def _write(self, payload: str) -> None:
        if not self._alive.is_set():
            raise NotConnected("socket is closed")
        with self._send_lock:
            try:
                self._sock.sendall(payload.encode())
            except OSError as exc:
                self._alive.clear()
                raise NotConnected(f"send failed: {exc}") from exc

    def _next_tag(self) -> str:
        with self._pending_lock:
            self._seq += 1
            return str(self._seq)

    def send(self, n: int, wait: bool = True, timeout: Optional[float] = None,
             tagged: Optional[bool] = None, **params) -> Optional[Reply]:
        """Send one command frame. Returns the reply, or None if wait is off.

        Commands normally carry a tag, so replies can be matched even with
        several in flight. The exceptions are in UNTAGGED_REPLY_COMMANDS, which
        answer with a bare `{ok}`; those go out without an `H` and are matched
        in send order instead. Pass `tagged` to override the choice.
        """
        number = int(n)
        if tagged is None:
            tagged = number not in UNTAGGED_REPLY_COMMANDS

        tag = self._next_tag() if tagged else None
        frame = {"N": number}
        if tag is not None:
            frame["H"] = tag
        frame.update({k: v for k, v in params.items() if v is not None})
        payload = json.dumps(frame, separators=(",", ":"))

        pending = _Pending()
        with self._pending_lock:
            if wait:
                # Register before sending, or a fast reply races registration.
                if tag is not None:
                    self._pending[tag] = pending
                else:
                    self._untagged_waiters.append(pending)
            elif tag is not None:
                # A reply is still coming. Mark it for the bin.
                if len(self._discard_tags) > 512:
                    self._discard_tags.clear()
                self._discard_tags.add(tag)

        try:
            self._write(payload)
        except CarError:
            if wait:
                self._unregister(tag, pending)
            raise

        if not wait:
            return None

        limit = self.config.reply_timeout if timeout is None else timeout
        if not pending.event.wait(limit):
            self._unregister(tag, pending)
            raise ReplyTimeout(f"no reply to {payload} within {limit:.1f}s")
        if pending.reply is None:
            raise NotConnected("connection dropped while waiting for a reply")
        return pending.reply

    # ---------- motion ----------
    #
    # Speed is nominally 0 to 255 and the firmware does not honour that. From
    # Elegoo's UNO source:
    #
    #   * Turning goes straight to the motor driver at the speed given.
    #   * Forward and backward go through a yaw correction loop that stops
    #     both motors every 10 ms to read the MPU6050, so straight motion is
    #     chopped and is much weaker at the same number.
    #   * Command 2 runs with UpperLimit 180, so straight motion clamps there;
    #     the same loop clamps the bottom at 10.
    #
    # In practice: drive at 150 to 180, turn at 100 to 130. Driving wants a
    # bigger number than turning, which is the opposite of the intuition.

    def move(self, direction: Direction, speed: int = 120,
             ms: int = 400) -> Optional[Reply]:
        """Timed move. The primary action primitive, because the firmware
        bounds it: if the host dies mid move the car still stops itself.

        The reply cannot arrive before the move finishes. Command 2 only sets
        a mode and a start time, prints nothing, and returns; the `{tag_ok}`
        comes from the main loop once the timer expires. So the timeout has to
        include the requested duration. A fixed 1.5 s looked generous and was
        not: it left barely a second of margin on a 400 ms move sharing a
        9600 baud line with the safety layer's polling, and the resulting
        timeouts looked exactly like a car that would not drive.
        """
        if (self.config.straight_via_direct_motor
                and direction in (Direction.FORWARD, Direction.BACKWARD)):
            return self.drive_straight(direction, speed=speed, ms=ms)
        if (self.config.turn_via_tank
                and direction in (Direction.LEFT, Direction.RIGHT)):
            return self.turn_tank(direction, speed=speed, ms=ms)

        # reply_timeout + the move itself + a second of slack. The slack is
        # not padding: the reply is printed by the UNO's main loop when the
        # timer expires, and it then queues behind whatever else is on a
        # 9600 baud line. With the safety layer polling at 8 Hz that line is
        # already half full, and a 400 ms move duly missed a 1.9 s deadline
        # while the car was driving perfectly well. Giving up on a move that
        # is working is worse than waiting.
        timeout = self.config.reply_timeout + ms / 1000.0 + 1.0
        return self.send(N.TIMED_MOVE, timeout=timeout,
                         D1=int(direction), D2=speed, T=ms)

    def motor_direct(self, speed: int, forward: bool = True,
                     selection: int = 0,
                     wait: bool = False) -> Optional[Reply]:
        """Command 1: drive the motors directly, with no yaw correction.

        `selection` 0 is both motors, 1 and 2 are the left and right sides.
        Keeps running until something changes it, so whatever calls this owns
        the stop.

        This exists because command 2's straight line path is unusable on this
        car: it stops the motors every 10 ms to read the IMU, and that read
        does not come back.

        **Does not wait for an acknowledgement by default.** Elegoo's published
        source prints `{tag_ok}` for command 1, and on this car it does not: a
        forward frame timed out at 1.5 s while the wheels were visibly turning,
        in a session where commands 100 and 110 both answered normally. Waiting
        for that ack buys nothing anyway, since there is no useful response to
        its absence, and it costs a great deal: the timeout raises out of the
        caller while the motors are already running, which is precisely how a
        stop ends up being skipped. Pass `wait=True` to probe the behaviour.
        """
        return self.send(N.MOTOR_DIRECT, wait=wait, D1=selection, D2=speed,
                         D3=1 if forward else 2)

    def drive_straight(self, direction: Direction, speed: int = DRIVE_SPEED,
                       ms: int = 400) -> Optional[Reply]:
        """Drive straight for a while, timed here rather than by the firmware.

        No yaw correction, so the car will wander. That is an acceptable trade:
        the correction was not working anyway, and every action is short and
        re-observed.

        Nothing but the halt below is going to stop the car, so it runs in a
        `finally`: if the sleep is interrupted, or anything at all goes wrong
        between starting and stopping, the motors are still cut.
        """
        forward = direction == Direction.FORWARD
        reply = self.motor_direct(speed, forward=forward)
        try:
            time.sleep(ms / 1000.0)
        finally:
            self.halt()
        return reply     # None: command 1 is not acknowledged on this car

    def drive(self, direction: Direction, speed: int = 120) -> Reply:
        """Continuous move with no built in end. Prefer `move`. Anything using
        this needs its own stop, on the error path as well as the happy one."""
        return self.send(N.CONTINUOUS_MOVE, D1=int(direction), D2=speed)

    def tank(self, left: int, right: int,
             wait: bool = False) -> Optional[Reply]:
        """Left and right side speeds directly. Both sides drive FORWARD.

        There is no reverse in this command: the firmware's handler sets two
        forward speeds. That limitation is exactly why it works on this car,
        where asking the two sides to run in opposite directions has become
        unreliable.

        **D1 and D2 are named backwards in Elegoo's firmware, and this method
        corrects for it.** Follow the parameters through:

            CMD_MotorControlSpeed_xxx0(is_Speed_L, is_Speed_R)
              -> Motor_control(direction_just, is_Speed_L,
                               direction_just, is_Speed_R, ...)

            // and inside Motor_control:
            { //A...Right     gets speed_A, which was passed is_Speed_L
            { //B...Left      gets speed_B, which was passed is_Speed_R

        So `D1`, documented as the left speed, drives the **right** motor, and
        `D2` drives the left. Found on 2026-09-07 when the agent asked to turn
        right, said so out loud, and turned left. Every turn was mirrored.

        The crossing is contained here so that callers can use `left` and
        `right` and mean them. Not waited on by default, in line with command
        1: this car's acknowledgements cannot be relied on, and blocking on one
        while the motors run is how a stop gets skipped.
        """
        return self.send(N.TANK, wait=wait, D1=right, D2=left)

    def turn_tank(self, direction: Direction, speed: int = TURN_SPEED,
                  ms: int = 300) -> Optional[Reply]:
        """Turn by driving one side and holding the other at zero.

        The car swings about the stopped side rather than about its centre.
        Wider than a pivot, and entirely usable: every action here is short and
        re-observed, so where the centre of rotation sits does not matter.

        Measured at 68 degrees per 400 ms burst at speed 170, symmetric in both
        directions, which makes a right angle about 530 ms.

        Command 4 has no timer of its own, so the halt below is the only thing
        that will stop the car, and it is in a `finally` for the same reason
        `drive_straight`'s is.
        """
        # To swing left, drive the right side and hold the left. `tank` takes
        # care of the firmware's crossed parameter names.
        turning_left = direction == Direction.LEFT
        reply = self.tank(left=0 if turning_left else speed,
                          right=speed if turning_left else 0)
        try:
            time.sleep(ms / 1000.0)
        finally:
            self.halt()
        return reply

    def halt(self) -> None:
        """Cut the motors. This is the one that physically stops the car.

        One frame: command 1, both motors, speed 0, direction forward. Zero
        pulse width through the same branch that drives the car, which is why
        it is the branch to trust.

        **Command 100 does not stop the motors.** It sets a functional mode and
        prints `{ok}`, and every mode handler's "not my mode" branch clears a
        flag without touching the motors, so the last pulse width written keeps
        being written. Command 2 hid that for a long time because its own timer
        expires and calls `stop_it`.

        A day was spent, 2026-09-07, on a stop that appeared not to work at
        all: seven different frames across four command numbers, none of them
        stopping the wheels, and results that would not reproduce between
        scripts. **The cause was a 9 V PP3 battery.** It cannot supply four
        motors, the rail collapsed when they started, the ESP32's radio browned
        out before anything else, and the UNO carried on writing the last pulse
        width. Nothing was wrong with this frame. `stop_diff.py` on 18650s
        stopped the car in all four cells, single frame included, which is what
        this now sends.

        Not waited on: this is the call that matters most in an emergency and
        it must not block behind a congested serial link, or on an
        acknowledgement command 1 may not send.
        """
        self.send(N.MOTOR_DIRECT, wait=False, D1=0, D2=0, D3=1)

    def stop(self, wait: bool = True) -> Optional[Reply]:
        """Stop the car: cut the motors, then clear the functional mode.

        The order matters. The motors come first because that is the part that
        prevents a collision; clearing the mode afterwards is housekeeping.
        """
        self.halt()
        return self.send(N.STOP, wait=wait)

    def servo(self, servo_id: int, angle: int) -> Reply:
        """Servo 1 pans the head left and right. 90 is nominally centre.

        There is no tilt on this unit: the head turns only, and the vertical
        aim is fixed by the bracket. The pin map lists a second servo channel
        on D11, and the firmware takes `D1=2`, but nothing is connected to it
        here. Fit a servo there and tilt becomes available with no firmware
        change, which is the cheapest route to an adjustable aim.
        """
        return self.send(N.SERVO, D1=servo_id, D2=angle)

    @property
    def aim(self) -> Tuple[Optional[int], Optional[int]]:
        """Where the head was last told to point, as (pan, tilt).

        Last commanded, not measured. Nothing in the protocol reads a servo
        back, and the servos have no feedback, so this is the best that exists.
        A None means nothing has commanded that axis this session, and the
        firmware's power up position is not known.
        """
        return self.last_pan, self.last_tilt

    def pan(self, angle: int) -> Reply:
        """Turn the head left and right. 90 is nominally centre."""
        clamped = max(PAN_MIN, min(PAN_MAX, angle))
        reply = self.servo(1, clamped)
        self.last_pan = clamped
        return reply

    def home(self) -> None:
        """Point the head straight ahead and level, and settle there.

        Straight is `PAN_CENTRE + PAN_TRIM`, level is `TILT_LEVEL + TILT_TRIM`.
        Adjust the trims if the head does not actually end up looking where it
        should; nothing can measure a servo's position, so this is calibrated
        by eye once rather than sensed.

        Called on connect by default, because servos hold position when the
        power goes off, so without it the first frame of a session is whatever
        the previous session happened to leave behind.
        """
        self.pan(PAN_CENTRE + PAN_TRIM)
        self.tilt(TILT_LEVEL + TILT_TRIM)
        time.sleep(0.6)      # let both servos actually arrive

    def tilt(self, angle: int) -> Reply:
        """Aim the head up and down, on the servo fitted to D11.

        Higher is further down. 120 is as low as the firmware will go, and
        that only gets the camera to about level: see TILT_DRIVING.

        **The angle is clamped to what the firmware will actually honour.**
        Not to protect the servo, which the firmware already does by refusing
        to drive it past 60 or 120, but so that `aim` does not claim a position
        the camera never reached.

        The range finder does not tilt with the camera, confirmed twice, so a
        distance reading stays valid at any tilt angle.
        """
        clamped = max(TILT_MIN, min(TILT_MAX, angle))
        reply = self.servo(2, clamped)
        self.last_tilt = clamped
        return reply

    def leds(self, sequence: int, r: int, g: int, b: int) -> Reply:
        """Useful for showing agent state without a screen."""
        return self.send(N.LEDS, D1=sequence, D2=r, D3=g, D4=b)

    def program_mode(self) -> Reply:
        """Clears the stock behaviours. Send early, and again after any
        reconnect, or obstacle avoidance competes for the motors."""
        return self.send(N.PROGRAM_MODE)

    def builtin_mode(self, mode: int) -> Reply:
        """1 trace, 2 avoid, 3 follow. Hands the car back to its own
        firmware."""
        return self.send(N.BUILTIN_MODE, D1=mode)

    # ---------- sensing ----------

    def distance(self) -> int:
        """Raw ultrasonic reading from wherever the servo is pointing.

        Not centimetres. See `distance_cm`. Returns the firmware's own number,
        which is what you want for logging, since it is lossless.
        """
        return self.send(N.ULTRASONIC, D1=2).as_int()

    def distance_cm(self) -> float:
        """Distance in centimetres, or `inf` if the reading is at the ceiling.

        Calibrated 2026-09-06 against a wall at 10, 30 and 100 cm. Residuals
        under one count, and the intercept is zero within the resolution, so
        this is a pure scale.

        A raw reading of RAW_CEILING is not a measurement. It means at least
        about 195 cm **or no echo came back at all**, and those two cannot be
        told apart. Returning `inf` is right for a `if d < threshold: stop`
        comparison, but it is the reason a safety layer must not treat a far
        reading as proof the way ahead is clear: a dead sensor reads exactly
        the same as an open corridor. Corroborate before trusting it.
        """
        raw = self.distance()
        return float("inf") if raw >= RAW_CEILING else raw * RAW_TO_CM

    def at_max_range(self) -> bool:
        """True when the reading is pinned at the ceiling, so it carries no
        distance information."""
        return self.distance() >= RAW_CEILING

    def obstacle_near(self) -> bool:
        """The firmware's own near check. What threshold it compares against
        is not known yet, so `distance` is the one to build the safety layer
        on."""
        return self.send(N.ULTRASONIC, D1=1).as_bool()

    def line_sensor(self, index: int) -> int:
        """Raw analogue reading, 0 to 1023, for one of the three channels.
        Index 0 and 1 confirmed, 2 inferred."""
        return self.send(N.LINE, D1=index).as_int()

    def line_sensors(self) -> Tuple[int, int, int]:
        return tuple(self.line_sensor(i) for i in range(3))

    def line_dark(self) -> Tuple[bool, bool, bool]:
        """Each channel thresholded at its own pale/dark midpoint.

        Channels 0 and 2 have only about 200 counts of contrast, so this is a
        coarse read. Do not use it as a cliff stop yet: what the sensors report
        with nothing underneath them at all has not been measured.
        """
        readings = self.line_sensors()
        return tuple(v > m for v, m in zip(readings, LINE_MIDPOINTS))

    def ground_check(self) -> bool:
        """Raw result of command 23. **Does not work. Do not use.**

        Tested 2026-09-06 in both positions, held in the air and flat on the
        floor, with no parameter and with D1 of 0, 1 and 2. All eight
        combinations returned false. Whatever this reports, it is not ground
        presence. Kept only so nobody has to rediscover that. Use `over_edge`.
        """
        return self.send(N.GROUND).as_bool()

    def over_edge(self) -> bool:
        """True when the front sensors see no floor. The cliff stop.

        Reads one channel, because it is the only one with usable margin. The
        sensor array sits across the front, and the whole array cleared the
        table in testing, so approaching an edge square is covered. An edge met
        at a sharp angle may trip only part of the array and is not covered by
        a single channel; if that matters, widen this to trip on any channel
        and accept more false stops.

        **Validate this against your actual floors before trusting it.** A dark
        enough surface reads close to no-surface: the measured margin is 218
        counts against the darkest thing tested, which is not much. Read
        `line_sensors()` over the darkest floor the car will meet and confirm
        channel 2 stays well under CLIFF_THRESHOLD.

        The failure directions are not symmetric. Tripping on a black rug is a
        nuisance. Missing a real edge puts the car on the floor, so if you are
        adjusting the threshold, adjust it towards stopping.
        """
        return self.line_sensor(CLIFF_CHANNEL) > CLIFF_THRESHOLD

    # ---------- vision ----------

    @property
    def stream_url(self) -> str:
        return f"http://{self.config.host}:{self.config.stream_port}/stream"

    def _http(self, path: str) -> bytes:
        url = f"http://{self.config.host}:{self.config.http_port}{path}"
        with urllib.request.urlopen(url, timeout=self.config.http_timeout) as r:
            return r.read()

    def capture(self, discard: int = CAPTURE_DISCARD) -> bytes:
        """One still frame as JPEG bytes. A single still per decision is all
        the model needs, which sidesteps stream frame rate entirely.

        **`discard` is not optional politeness, it is a correctness fix.** The
        camera firmware runs two frame buffers with `CAMERA_GRAB_WHEN_EMPTY`,
        which means `esp_camera_fb_get` returns the OLDEST queued frame rather
        than the newest. The driver refills a buffer as soon as one is free, so
        between captures a frame sits in the queue ageing, and the first
        `/capture` after the car has moved returns a picture taken **before**
        it moved.

        That is not a theoretical worry. On 2026-09-08 the agent scanned, saw
        nothing, turned 90 degrees, and then reported finding its target and
        drove at it. The target was where the car had been pointing one turn
        earlier: the model was shown a stale frame and acted on the past.

        Draining the queue costs about 50 ms a frame and is worth it. The real
        fix is `CAMERA_GRAB_LATEST` in the firmware, after which this can go
        back to zero; the firmware in this repository has it, but a car running
        an older flash does not.
        """
        for _ in range(max(0, discard)):
            self._http("/capture")
        return self._http("/capture")

    def capture_to(self, path: str) -> str:
        with open(path, "wb") as f:
            f.write(self.capture())
        return path

    def camera(self, var: str, val) -> bytes:
        """Camera settings: framesize, quality, brightness and around twenty
        others. Tunable at runtime from the agent loop."""
        query = urllib.parse.urlencode({"var": var, "val": val})
        return self._http(f"/control?{query}")

    def status(self) -> dict:
        return json.loads(self._http("/status").decode())
