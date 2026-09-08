#!/usr/bin/env python3
"""
safety_test.py - prove the phase 04 gate.

The brief's condition for moving on is blunt: the safety layer has to
demonstrably refuse to drive into a wall. This runs that demonstration, plus
the other refusals the layer is supposed to make, and prints a pass or fail for
each so the result is not a matter of opinion.

    export ELEGOO_HOST=192.168.1.211
    python3 safety_test.py                 # everything except motion
    python3 safety_test.py --move          # includes the wall test

Without --move nothing drives, so it is safe to run at a desk. With --move the
car is commanded forward at a wall, deliberately, and the whole point is that
it should refuse or cut the move short.

WHAT YOU NEED
  * The car on the floor, not on a box. This test is about the floor.
  * A wall, a box or a book to put in front of it.
  * A table edge for the cliff test, and a hand ready to catch it.
"""

import argparse
import sys
import time

from elegoo import Car, CarConfig, CarError, Direction
from safety import SafetyLoop, SafetyConfig, Reason

passes, failures = [], []


def check(label, condition, detail=""):
    (passes if condition else failures).append(label)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    return condition


def prompt(text):
    input(f"\n  >> {text}, then press Enter ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--move", action="store_true",
                    help="include the tests that actually drive the motors")
    args = ap.parse_args()

    cfg = CarConfig.from_env(**({"host": args.host} if args.host else {}))
    print(f"connecting to {cfg.host} ...")

    vetoes = []
    scfg = SafetyConfig(on_veto=lambda r, why: vetoes.append(why))

    try:
        with Car(cfg) as car, SafetyLoop(car, scfg) as guard:
            print(f"connected. {guard.describe()}\n")

            # ---- 1. the loop is actually running ----------------------------
            print("1. the loop itself")
            r1 = guard.reading
            check("has a reading", r1 is not None)
            time.sleep(0.5)
            r2 = guard.reading
            check("readings are being refreshed",
                  r2 is not None and r1 is not None and r2.at > r1.at,
                  f"age {time.monotonic() - r2.at:.2f}s" if r2 else "")
            check("range finder proved alive", guard.sensor_proven,
                  "" if guard.sensor_proven else
                  "point it at something within 2 m and rerun")

            # ---- 2. bounds on any single action -----------------------------
            print("\n2. limits on what one action may ask for")
            v = guard.check(Direction.FORWARD, speed=250, ms=400)
            check("refuses excessive speed", not v.allowed, v.reason)
            v = guard.check(Direction.FORWARD, speed=120, ms=5000)
            check("refuses excessive duration", not v.allowed, v.reason)
            v = guard.check(Direction.BACKWARD, speed=120, ms=5000)
            check("refuses excessive reverse duration", not v.allowed, v.reason)

            # ---- 3. the wall ------------------------------------------------
            print("\n3. the wall, which is the gate")
            prompt("put a wall, box or book right in front of the car, "
                   "closer than 25 cm")
            time.sleep(0.5)
            r = guard.reading
            print(f"     reading now: {r}")
            near = r is not None and not r.at_ceiling and r.distance_cm < 30
            if not check("sees the obstacle", near,
                         "" if near else "nothing close detected, "
                                         "move it nearer and rerun"):
                print("     skipping the refusal test: nothing to refuse")
            else:
                v = guard.check(Direction.FORWARD, speed=120, ms=400)
                check("REFUSES to drive forward into it", not v.allowed,
                      v.reason)
                v = guard.check(Direction.BACKWARD, speed=120, ms=300)
                check("still allows reversing away", v.allowed, v.reason)

                if args.move:
                    print("\n     now actually commanding it forward...")
                    v = guard.move(Direction.FORWARD, speed=120, ms=400)
                    check("the move was refused or cut short",
                          (not v.allowed) or v.interrupted, str(v))

            prompt("take the obstacle away")
            time.sleep(0.5)
            v = guard.check(Direction.FORWARD, speed=120, ms=400)
            check("allows forward again once clear", v.allowed, v.reason)

            # ---- 4. the edge ------------------------------------------------
            print("\n4. the edge")
            prompt("hold the car so the front overhangs a table edge, "
                   "keeping hold of it")
            time.sleep(0.5)
            r = guard.reading
            print(f"     reading now: {r}")
            if check("detects no floor", r is not None and r.over_edge,
                     "" if r and r.over_edge else
                     "channel 2 did not cross the threshold, "
                     "see the cliff notes in CLAUDE.md"):
                v = guard.check(Direction.FORWARD, speed=120, ms=400)
                check("REFUSES to drive forward over it", not v.allowed,
                      v.reason)
                v = guard.check(Direction.BACKWARD, speed=120, ms=300)
                check("still allows reversing away", v.allowed, v.reason)

            prompt("put the car back on the floor")
            time.sleep(0.5)

            # ---- 5. staleness ------------------------------------------------
            print("\n5. what happens when the sensors go quiet")
            guard.pause_polling()
            time.sleep(scfg.stale_after_s + 0.4)
            v = guard.check(Direction.FORWARD, speed=120, ms=400)
            # The loop is still running, so a refusal here is genuinely about
            # the age of the reading rather than the loop being shut down.
            check("refuses to move on stale readings", not v.allowed, v.reason)
            check("and refuses for the right reason",
                  Reason.SENSOR_STALE in v.reason, v.reason)
            guard.resume_polling()
            time.sleep(0.3)
            v = guard.check(Direction.FORWARD, speed=120, ms=400)
            check("recovers once readings resume", v.allowed, v.reason)

            print(f"\nvetoes raised by the loop itself: {vetoes or 'none'}")

    except (OSError, CarError) as exc:
        print(f"\nFAILED to run: {type(exc).__name__}: {exc}")
        return 2

    print("\n" + "=" * 60)
    print(f"{len(passes)} passed, {len(failures)} failed")
    if failures:
        for f in failures:
            print(f"  failed: {f}")
        print("\nPhase 04 is NOT signed off. Do not start the model loop.")
        return 1
    print("\nThe safety layer refuses to drive into a wall and refuses to drive")
    print("over an edge. Phase 04's gate is met.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
