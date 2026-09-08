#!/usr/bin/env python3
"""
stop_test.py - prove the car can be stopped, before it is allowed on a floor.

The car ran away: it drove forward, never stopped, and hit something. The cause
was in the firmware, not the wiring. From Elegoo's UNO source:

    case 100:
      Functional_Mode = CMD_ClearAllFunctions_Standby_mode;
      Serial.print("{ok}");
      break;

Command 100 changes a mode and prints. It never touches the motors, and every
mode handler's "not my mode" branch only clears a flag. Whatever PWM was last
written keeps being written. So `car.stop()` has never stopped this car.

Command 2 hid that for months, because its own timer expires and calls
`stop_it`. The moment straight motion moved to command 1, which has no timer,
nothing was stopping the car at all.

The fix is command 1 with direction 0, which runs
`SmartRobotCarMotionControl(stop_it, 0)` and really does write zero to both
motors. `Car.halt()` sends exactly that, and `Car.stop()` now calls it first.

This script checks that claim with your eyes, because nothing on the robot can
check it. Four stop routes, each one: spin the wheels, stop, ask you what
happened.

    python stop_test.py --host 192.168.1.211

PUT THE CAR ON A BOX. The wheels must be off the ground and free to turn. Do
not run this on the floor; that is the thing being tested.
"""

import argparse
import sys
import time

from elegoo import Car, CarConfig, CarError, Direction, DRIVE_SPEED

SPIN_S = 1.2          # long enough to see and hear, short enough to be safe


def confirm(text: str) -> bool:
    return input(f"  >> {text} [y/n] ").strip().lower().startswith("y")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--speed", type=int, default=DRIVE_SPEED)
    args = ap.parse_args()

    cfg = CarConfig.from_env(**({"host": args.host} if args.host else {}))

    print(__doc__.split("    python")[0].strip())
    print()
    print("!!  Car on a box. Wheels clear of the ground. Not on the floor.  !!")
    input("Press Enter when the wheels are off the ground, or Ctrl+C to quit. ")

    results = []

    def trial(car, name: str, start, stop, note: str) -> None:
        print(f"\n{name}")
        print(f"   {note}")
        input("   Press Enter to spin the wheels. ")
        try:
            start()
            time.sleep(SPIN_S)
        finally:
            # Never let a failing stop route abort the run while the wheels are
            # turning, but do not paper over it either.
            try:
                stop()
            except CarError as exc:
                print(f"   the stop itself failed: {exc}")

        # The rescue halt goes AFTER the question, not before it. An earlier
        # version put it in the `finally` above, which meant every trial ended
        # with a working stop and trial 3 could not fail even in principle. It
        # duly reported that command 100 stops the car, contradicting the
        # firmware source, and the contradiction was this script.
        time.sleep(0.4)
        stopped = confirm("did the wheels come to a complete stop?")
        results.append((name, stopped))
        if not stopped:
            print("   Cutting power to the motors now.")
            try:
                car.halt()
            except CarError:
                pass

    try:
        with Car(cfg) as car:
            # `connect` already sent a halt, so if the wheels are turning now,
            # something is wrong before the first trial even starts.
            if confirm("are the wheels turning right now, before any test?"):
                print("\nThe car is already moving on connect. Stop here and")
                print("power cycle it; the tests below assume a still start.")
                return 1

            trial(car, "1. halt() on its own",
                  lambda: car.motor_direct(args.speed, forward=True),
                  car.halt,
                  "command 1 forward, then command 1 with direction 0. "
                  "This is the one that must work.")

            trial(car, "2. stop(), the everyday call",
                  lambda: car.motor_direct(args.speed, forward=True),
                  car.stop,
                  "halt first, then command 100 to clear the mode. "
                  "This is what every script and the safety layer call.")

            trial(car, "3. command 100 alone, the one that never worked",
                  lambda: car.motor_direct(args.speed, forward=True),
                  lambda: car.send(100, wait=False),
                  "EXPECT THIS TO FAIL. If the wheels keep turning, the "
                  "firmware reading was right. A pass here would mean the "
                  "diagnosis is wrong and worth rechecking.")
            # Whatever trial 3 did, leave nothing spinning.
            car.halt()

            trial(car, "4. drive_straight, the path that ran away",
                  lambda: None,
                  lambda: car.drive_straight(Direction.FORWARD,
                                             speed=args.speed,
                                             ms=int(SPIN_S * 1000)),
                  "the real driving primitive, timed on the host with the "
                  "halt in a `finally`. This is what agent.py issues.")

            print("\ncleaning up")
            car.stop()

    except KeyboardInterrupt:
        print("\nstopped by hand. Check the wheels are not still turning.")
        return 1
    except (OSError, CarError) as exc:
        print(f"\nfailed: {type(exc).__name__}: {exc}")
        print("If the wheels are still turning, switch the car off.")
        return 2

    print("\n" + "=" * 60)
    for name, ok in results:
        print(f"  {'stopped ' if ok else 'RAN ON '} {name}")

    expected_fail = {"3. command 100 alone, the one that never worked"}
    bad = [n for n, ok in results if not ok and n not in expected_fail]
    surprise = [n for n, ok in results if ok and n in expected_fail]

    print()
    if bad:
        print("NOT SAFE TO DRIVE. These did not stop the car:")
        for n in bad:
            print(f"  {n}")
        print("Keep the car on the box. Nothing on the floor until 1, 2 and 4")
        print("all stop it.")
        return 1
    if surprise:
        print("Trials 1, 2 and 4 stopped the car, so it is safe to drive.")
        print()
        print("Command 100 stopped it too. Elegoo's published source says it")
        print("should not, and that source has now been wrong about this car")
        print("three times, so the disagreement is expected rather than")
        print("alarming: the UNO here is running a different build. Keep")
        print("using halt(), whose mechanism is visible and does not depend")
        print("on which firmware is loaded, and treat 100 stopping the car as")
        print("a bonus rather than something to rely on.")
        return 0
    print("Every stop route worked and command 100 behaved as the firmware")
    print("says it does. Safe to put the car back on the floor.")
    print("Next: python calibrate_motion.py --host " + cfg.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
