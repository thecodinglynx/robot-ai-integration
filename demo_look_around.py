#!/usr/bin/env python3
"""
demo_look_around.py - the robot has a look around, then puts itself back.

Exercises everything built so far in one go: the client library, the sensors,
both camera servos, timed motion, and the safety layer holding veto power over
all of it. Mostly it is a demonstration, but it is also a useful smoke test,
because it touches every subsystem in a single run.

    python demo_look_around.py --host 192.168.1.211
    python demo_look_around.py --host 192.168.1.211 --no-drive   # camera only
    python demo_look_around.py --host 192.168.1.211 --captures   # save frames

ABOUT "ENDS UP WHERE IT STARTED"

There are no wheel encoders, so the car cannot know where it is. The only
honest way to return to a starting point is to make every movement in a pair:
do a thing, then later do exactly the opposite thing for exactly as long. That
is what the undo stack below does, replaying the run backwards with each
direction inverted.

It will not be exact. Timed moves drift with battery voltage, floor surface and
which way the wheels happened to be pointing. Expect to end up near where you
started, facing roughly the right way, not on the same square centimetre. That
drift is the whole reason the architecture re-observes after every short action
rather than trusting a plan.

If the safety layer cuts a move short, its inverse would overshoot, so this
records the shortfall and says so rather than pretending.
"""

import argparse
import os
import random
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

from elegoo import (Car, CarConfig, CarError, Direction,
                    TILT_SCAN, TILT_DRIVING, TILT_MIN, TILT_MAX,
                    DRIVE_SPEED, TURN_SPEED)
from safety import SafetyLoop, SafetyConfig

# Both servos are clamped by the UNO firmware to roughly 90 +/- 30, so there is
# no point asking for more than this and the head simply stops moving if we do.
PAN_CENTRE = 90
PAN_LEFT = 62
PAN_RIGHT = 118

OPPOSITE = {
    Direction.FORWARD: Direction.BACKWARD,
    Direction.BACKWARD: Direction.FORWARD,
    Direction.LEFT: Direction.RIGHT,
    Direction.RIGHT: Direction.LEFT,
    Direction.FORWARD_LEFT: Direction.BACKWARD_RIGHT,
    Direction.FORWARD_RIGHT: Direction.BACKWARD_LEFT,
    Direction.BACKWARD_LEFT: Direction.FORWARD_RIGHT,
    Direction.BACKWARD_RIGHT: Direction.FORWARD_LEFT,
}


@dataclass
class Done:
    direction: Direction
    speed: int
    ms: int
    complete: bool          # False if the safety layer cut it short


class Robot:
    """Thin wrapper that narrates, keeps an undo stack, and never moves
    without asking the safety layer first."""

    def __init__(self, car: Car, guard: SafetyLoop, captures: Optional[str]):
        self.car = car
        self.guard = guard
        self.captures = captures
        self.history: List[Done] = []
        self.frame_no = 0

    # ---------- narration ----------

    def say(self, text: str) -> None:
        print(f"  {text}")

    def beat(self, seconds: float = 0.6) -> None:
        """A pause. Half the point of this demo is that it looks like it is
        thinking rather than twitching."""
        time.sleep(seconds)

    # ---------- looking ----------

    def look(self, pan: int, tilt: Optional[int] = None,
             label: str = "") -> Optional[int]:
        self.car.pan(pan)
        if tilt is not None:
            self.car.tilt(tilt)
        self.beat(0.5)          # let the servo arrive and the sensor settle
        reading = self.guard.reading
        raw = reading.distance_raw if reading else None
        where = "far" if reading and reading.at_ceiling else (
            f"{reading.distance_cm:.0f} cm" if reading else "?")
        aim = f"pan {pan}" + (f", tilt {tilt}" if tilt is not None else "")
        self.say(f"{label or 'looks'} ({aim}): {where}")
        self.snap(f"pan{pan}" + (f"_tilt{tilt}" if tilt is not None else ""))
        return raw

    def snap(self, tag: str) -> None:
        if not self.captures:
            return
        self.frame_no += 1
        name = os.path.join(self.captures, f"{self.frame_no:02d}_{tag}.jpg")
        try:
            self.car.capture_to(name)
        except OSError as exc:
            self.say(f"(capture failed: {exc})")

    def scan(self, label: str = "scanning") -> None:
        """Sweep the head across the room, pausing at each stop."""
        self.say(f"{label}...")
        nearest, nearest_pan = None, None
        for pan in (PAN_CENTRE, PAN_LEFT, PAN_CENTRE, PAN_RIGHT, PAN_CENTRE):
            raw = self.look(pan, label="  ")
            if raw is not None and raw < 150:
                if nearest is None or raw < nearest:
                    nearest, nearest_pan = raw, pan
        if nearest is not None:
            self.say(f"  closest thing is at pan {nearest_pan}, "
                     f"{nearest * 1.296:.0f} cm away")
        else:
            self.say("  nothing within range in any direction")

    # ---------- moving ----------

    def move(self, direction: Direction, speed: int, ms: int,
             why: str = "") -> bool:
        verdict = self.guard.move(direction, speed=speed, ms=ms)
        name = direction.name.lower().replace("_", " ")
        if not verdict.allowed:
            self.say(f"wanted to go {name}, refused: {verdict.reason}")
            return False
        if verdict.interrupted:
            self.say(f"went {name} but was stopped: {verdict.reason}")
            self.history.append(Done(direction, speed, ms, complete=False))
            return True
        self.say(f"goes {name} for {ms} ms{f' ({why})' if why else ''}")
        self.history.append(Done(direction, speed, ms, complete=True))
        return True

    def undo(self) -> None:
        """Replay the run backwards, each move inverted."""
        if not self.history:
            self.say("nothing to undo, it never moved")
            return
        partial = sum(1 for d in self.history if not d.complete)
        self.say(f"retracing {len(self.history)} move(s), in reverse")
        if partial:
            self.say(f"warning: {partial} of them were cut short, so their "
                     f"inverses will overshoot")
        for done in reversed(self.history):
            back = OPPOSITE[done.direction]
            verdict = self.guard.move(back, speed=done.speed, ms=done.ms)
            state = "ok" if verdict.allowed and not verdict.interrupted else \
                    f"PROBLEM: {verdict.reason}"
            self.say(f"  back {back.name.lower().replace('_', ' ')} "
                     f"{done.ms} ms: {state}")
            self.beat(0.3)
        self.history.clear()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None,
                    help="command socket port, for testing "
                         "against something other than the car")
    ap.add_argument("--no-drive", action="store_true",
                    help="camera and sensors only, wheels never turn")
    ap.add_argument("--captures", nargs="?", const="demo_frames", default=None,
                    metavar="DIR", help="save a frame at each look")
    args = ap.parse_args()

    if args.captures:
        os.makedirs(args.captures, exist_ok=True)

    overrides = {}
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["command_port"] = args.port
    cfg = CarConfig.from_env(**overrides)

    if not args.no_drive:
        print("\nThis drives the car. It needs about a metre of clear floor in")
        print("front of it, and it will try to return to where it started.")
        input("Press Enter when the space is clear, or Ctrl+C to stop. ")

    try:
        with Car(cfg) as car, SafetyLoop(car, SafetyConfig()) as guard:
            bot = Robot(car, guard, args.captures)
            print()

            # ---- waking up ----
            bot.say("waking up")
            car.home()          # straight ahead and level, every time
            bot.beat(0.4)
            bot.say(guard.describe())
            print()

            # ---- first look around ----
            bot.scan("having a look around")
            print()

            # ---- checking the floor, then the horizon ----
            bot.say("checking the ground in front")
            bot.look(PAN_CENTRE, TILT_DRIVING, label="  looks down")
            bot.beat()
            bot.say("and back up")
            bot.look(PAN_CENTRE, TILT_SCAN, label="  looks up")
            print()

            # ---- a double take, which reads as noticing something ----
            side = random.choice([PAN_LEFT, PAN_RIGHT])
            bot.say("thinks it saw something")
            bot.look(side, label="  snaps round")
            bot.beat(1.0)
            bot.look(PAN_CENTRE, label="  looks back")
            bot.beat(0.5)
            bot.say("looks again, more carefully")
            bot.look(side, TILT_SCAN, label="  peers")
            bot.beat(1.2)
            bot.look(PAN_CENTRE, TILT_SCAN, label="  gives up on it")
            print()

            # ---- going to have a closer look ----
            if not args.no_drive:
                bot.say("going for a closer look")
                bot.move(Direction.FORWARD, speed=DRIVE_SPEED, ms=400,
                         why="edging forward")
                bot.beat()
                bot.scan("checking again from here")
                print()

                bot.say("trying a different angle")
                bot.move(Direction.LEFT, speed=TURN_SPEED, ms=300, why="turning")
                bot.beat()
                bot.look(PAN_CENTRE, label="  looks")
                bot.beat()
                bot.move(Direction.FORWARD, speed=DRIVE_SPEED, ms=350,
                         why="a bit further")
                bot.beat()
                bot.scan("one more look")
                print()

                # ---- putting itself back ----
                bot.say("gives up and goes home")
                bot.undo()
                print()
            else:
                bot.say("(driving skipped)")
                print()

            # ---- settling down ----
            bot.say("settling down")
            car.home()
            bot.beat(0.4)
            bot.say(guard.describe())

            vetoes = guard.vetoes
            if vetoes:
                print(f"\n  the safety layer intervened {len(vetoes)} time(s):")
                for _, why in vetoes:
                    print(f"    {why}")

        print("\ndone. It is near where it started, not exactly where it")
        print("started: timed moves drift, and nothing on this robot can")
        print("measure how far it actually went.")
        return 0

    except KeyboardInterrupt:
        print("\nstopped by hand")
        return 1
    except (OSError, CarError) as exc:
        print(f"\nfailed: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
