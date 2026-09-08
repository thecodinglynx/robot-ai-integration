#!/usr/bin/env python3
"""
calibrate_motion.py - what speed actually moves this car, and how far.

Answers two of the oldest open questions in CLAUDE.md, both of which have been
guessed at until now:

  * the lowest speed value that reliably moves the car from a standstill
  * how far it goes, and how far it turns, per millisecond at a given speed

Neither can be measured by the robot. There are no wheel encoders, so the
distances come from you and a tape measure. That is not a shortcoming of this
script, it is the shape of the hardware.

    python calibrate_motion.py --host 192.168.1.211

Needs about two metres of clear floor and a tape measure. The car drives in
short bursts and stops between them; the safety layer is running throughout, so
it will refuse to drive into anything.

WHY THIS MATTERS

`demo_look_around.py` drove at speed 110 and appeared not to move at all, while
turning at 120 worked. The firmware clamps its straight line correction to a
minimum output of 10 and caps some modes around 180, so the nominal 0 to 255
range is not what it appears, and a value that turns may still not be enough to
overcome stiction going forward. Guessing at that has cost a demo already.
"""

import argparse
import sys
import time

from elegoo import Car, CarConfig, CarError, Direction
from safety import SafetyLoop, SafetyConfig


def ask(text: str) -> str:
    return input(f"  >> {text} ").strip().lower()


def yes(text: str) -> bool:
    return ask(f"{text} [y/n] ").startswith("y")


def number(text: str, unit: str) -> float:
    while True:
        raw = ask(f"{text} (in {unit}, or 'skip'): ")
        if raw.startswith("s"):
            return float("nan")
        try:
            return float(raw)
        except ValueError:
            print("     that is not a number")


def wait_until_clear(guard, need_cm=40.0, timeout=30.0):
    """Block until the range finder actually reports clear floor ahead.

    The hand held up to prove the sensor is alive is still the newest reading
    when that step finishes, so the first move gets refused for an obstacle
    that is no longer there. Pressing Enter is not evidence: the sensor is,
    and it is the thing the safety layer will consult.

    `need_cm` is deliberately above the safety layer's own limit. Starting a
    move at exactly the threshold means the first reading after it begins can
    fall below and cut the move short, which measures the safety layer rather
    than the car.
    """
    if not getattr(guard, "polls", True):
        # Nothing is reading the sensor, so there is nothing to wait for.
        # Saying so beats spinning here forever on a reading that will never
        # arrive, which is what the first version of --no-safety did.
        print("   (no sensor polling: you are the safety layer)")
        return True
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = guard.reading
        if r is not None and (r.at_ceiling or r.distance_cm >= need_cm):
            shown = "far" if r.at_ceiling else f"{r.distance_cm:.0f} cm"
            print(f"\r   ahead: {shown}, clear                    ")
            return True
        shown = "no reading" if r is None else f"{r.distance_cm:.0f} cm"
        print(f"\r   ahead: {shown}, waiting for {need_cm:.0f} cm or more...",
              end="", flush=True)
        time.sleep(0.2)
    print()
    return False


class NoGuard:
    """A stand-in for SafetyLoop that vetoes nothing and polls nothing.

    Diagnostic only, and it exists because of a clean split in the evidence:
    with the safety layer polling at 8 Hz, command 2 circles at every speed
    from 140 to 180 and never acknowledges; with a quiet wire, the same
    command at 165 drove straight and acknowledged in 0.66 s. Neither command
    21 nor command 22 changes the functional mode in Elegoo's published
    source, so there is no mechanism to point at, but that source has already
    been wrong about this car three times over.

    So this removes the polling and nothing else. If the car then drives
    straight, the polling is implicated and the safety layer has to stop
    talking during a timed move. If it still circles, the polling is innocent
    and the fault is in the correction or the gyro.

    THIS TURNS OFF EVERY VETO. Nothing will refuse to drive into a wall or
    over an edge while it is in use. Two metres of clear floor, stay next to
    the car, and use it for this measurement and nothing else.
    """

    polls = False

    def __init__(self, car):
        self.car = car
        self.sensor_proven = True
        self.reading = None

    def describe(self):
        return "SAFETY LAYER OFF: no polling, no vetoes, nothing watching"

    def move(self, direction, speed, ms):
        try:
            self.car.move(direction, speed=speed, ms=ms)
            return _Verdict(True, "ok")
        except CarError as exc:
            return _Verdict(False, f"lost the car: {exc}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.car.halt()
        except CarError:
            pass
        return False


class _Verdict:
    interrupted = False
    reading = None

    def __init__(self, allowed, reason):
        self.allowed = allowed
        self.reason = reason


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--burst-ms", type=int, default=400,
                    help="how long each test burst runs, default 400")
    ap.add_argument("--direct", action="store_true",
                    help="drive with command 1 instead of command 2, which "
                         "bypasses Elegoo's yaw correction entirely. Use it "
                         "if the car circles at every speed: the correction "
                         "is then the suspect, and this measures the car "
                         "without it")
    ap.add_argument("--no-safety", action="store_true",
                    help="run with the safety layer off entirely: no polling, "
                         "no vetoes, nothing watching. Diagnostic, because the "
                         "polling is the one difference between the runs that "
                         "circle and the run that drove straight. Stay next "
                         "to the car")
    ap.add_argument("--start-speed", type=int, default=140,
                    help="where the sweep begins, default 140. Below about "
                         "140 this car pivots instead of driving and command "
                         "2 stops acknowledging, so the low end wastes time "
                         "and produces failures that are not about the car")
    args = ap.parse_args()

    overrides = {"straight_via_direct_motor": bool(args.direct)}
    if args.host:
        overrides["host"] = args.host
    cfg = CarConfig.from_env(**overrides)
    print(f"connecting to {cfg.host} ...")
    if args.direct:
        print("straight moves via command 1: no yaw correction in the path")

    print("\nYou need: about two metres of clear floor, and a tape measure.")
    print("The car drives in short bursts and stops between each one.")
    input("Press Enter when ready, or Ctrl+C to stop. ")

    findings = {}

    try:
        with Car(cfg) as car:
            guard_cm = (NoGuard(car) if args.no_safety
                        else SafetyLoop(car, SafetyConfig()))
            with guard_cm as guard:
                print(f"\n{guard.describe()}\n")

                # The safety layer will not allow forward motion until the range
                # finder has returned one real distance, as cheap proof it is
                # alive. This test wants two metres of clear floor in front, which
                # reads "far" forever, so that proof has to be arranged
                # deliberately. Without this the first burst is refused and the
                # refusal looks nothing like its cause.
                if not guard.sensor_proven:
                    print("   The range finder has not seen anything yet, and the")
                    print("   safety layer will not allow forward motion until it")
                    print("   has. Clear floor reads as 'far' forever, so:")
                    print()
                    for _ in range(60):
                        if guard.sensor_proven:
                            break
                        print("\r   >> hold a hand about 30 cm in front of the "
                              "sensor...", end="", flush=True)
                        time.sleep(0.5)
                    print()
                    if not guard.sensor_proven:
                        print("   Still nothing. Either the sensor is not working,")
                        print("   or the head is not pointing where you think.")
                        print("   Check with: python elegoo_probe.py --quiet, then")
                        print("   'd' for a distance reading.")
                        return 1
                    print("   Range finder proved alive. Take your hand away.")
                    input("   Press Enter when the floor ahead is clear again. ")
                    if not wait_until_clear(guard):
                        print("   The sensor still sees something close. Either")
                        print("   the floor is not clear or the head is pointing")
                        print("   somewhere unexpected. Nothing can be measured")
                        print("   until it reads clear.")
                        return 1

                # ---- 1. the threshold -------------------------------------------
                print("1. lowest speed that moves it at all")
                print("   Watch the wheels. Some of these will do nothing, and")
                print("   some will make a noise without the car actually moving.")
                print("   'Moved' means the car changed position, not that the")
                print("   motors made a sound.\n")

                # Starting at 140 by default. Elegoo's straight line correction
                # clamps the bottom at 10 and stops both motors every 10 ms to
                # read the gyro, so below that the car pivots on friction
                # differences rather than driving, and command 2 stops
                # acknowledging as well. Neither is informative about the speed
                # threshold this test is looking for. 180 is the ceiling the same
                # code imposes on straight motion, so there is no point above it.
                # Use --start-speed 100 to explore the low end deliberately.
                threshold = None
                spun = []
                no_ack = []
                for speed in range(args.start_speed, 181, 10):
                    # Re-check before every burst, not just at the start. The car
                    # is moving up the floor as this loop runs, so the space ahead
                    # shrinks, and a wall arriving mid-sweep should pause the run
                    # rather than end it.
                    if not wait_until_clear(guard):
                        print("   Not enough clear floor ahead to continue.")
                        print("   Move the car back, or turn it to face more")
                        print("   space, then run this again.")
                        return 1
                    verdict = guard.move(Direction.FORWARD, speed=speed,
                                         ms=args.burst_ms)
                    if not verdict.allowed and "lost the car" in verdict.reason:
                        # A missed reply is not a refusal about this speed. The
                        # UNO prints it from its main loop when the move timer
                        # expires, and on a busy serial line that can be late.
                        # Retry once before reading anything into it.
                        print(f"     speed {speed}: no reply in time, retrying")
                        time.sleep(1.0)
                        verdict = guard.move(Direction.FORWARD, speed=speed,
                                             ms=args.burst_ms)
                        if (not verdict.allowed
                                and "lost the car" in verdict.reason):
                            # Not a reason to stop. Below the threshold the motors
                            # stall rather than turn, a stalled motor draws several
                            # times its running current, and the rail sags far
                            # enough to lose the acknowledgement. The car is saying
                            # the speed is too low, in its own way. Record it and
                            # carry on up the sweep.
                            print(f"     speed {speed}: still no reply, very "
                                  f"likely stalling rather than driving")
                            no_ack.append(speed)
                            try:
                                car.halt()
                            except CarError:
                                pass
                            time.sleep(1.0)
                            continue
                    if not verdict.allowed:
                        # Say what to do about *this* refusal rather than guessing.
                        # An earlier version printed "clear the space in front" for
                        # every refusal and then concluded the battery was flat,
                        # which was wrong on both counts.
                        print(f"     speed {speed}: refused, {verdict.reason}")
                        if "too close" in verdict.reason:
                            print("     something is within 25 cm. Clear the floor "
                                  "ahead and rerun.")
                        elif "floor" in verdict.reason:
                            print("     the front sensors see no floor. Put the car "
                                  "back on the ground and rerun.")
                        elif "has not produced" in verdict.reason:
                            print("     the range finder still has not proved "
                                  "itself. Rerun and hold a hand in front when "
                                  "asked.")
                        else:
                            print("     that refusal is not about the speed value, "
                                  "so this test cannot continue.")
                        return 1
                    time.sleep(0.4)
                    # Three answers, not two. Below the working range the car has
                    # very little forward torque, because straight motion is
                    # chopped every 10 ms by the gyro read, and it pivots on
                    # friction differences instead of translating. That is a
                    # finding about the speed, not a failure of the run, and it
                    # needs recording rather than collapsing into "no".
                    answer = ask(f"speed {speed}: [d]rove forward, "
                                 f"[s]pun on the spot, [n]othing? ")[:1]
                    if answer == "d":
                        threshold = speed
                        break
                    if answer == "s":
                        print("     spun rather than drove: below the useful "
                              "range")
                        spun.append(speed)
                    # Put it back roughly where it started before the next try.
                    guard.move(Direction.BACKWARD, speed=speed, ms=args.burst_ms)
                    time.sleep(0.4)

                if no_ack:
                    joined = ", ".join(str(x) for x in no_ack)
                    findings["no_ack_at"] = joined
                    print(f"\n   No acknowledgement at: {joined}")
                    print("   Those speeds almost certainly stalled. Charge the")
                    print("   pack and they may come back; either way they are")
                    print("   below the useful range.")

                if threshold is None:
                    print("\n   Nothing drove it forward up to 180, which is")
                    print("   the ceiling the firmware puts on straight")
                    print("   motion, so there is nothing higher to try.")
                    if not args.direct:
                        print()
                        print("   Every speed circling, on a charged pack,")
                        print("   with the polling off, points at the yaw")
                        print("   correction rather than at the car. Its two")
                        print("   outputs are symmetric about the commanded")
                        print("   speed and go to motor A and motor B; if")
                        print("   those are not the sides this firmware")
                        print("   assumes, the correction is positive")
                        print("   feedback and the car spirals.")
                        print()
                        print("   Next: rerun with --direct. That drives with")
                        print("   command 1, which has no correction in the")
                        print("   path at all. If it goes straight, the")
                        print("   correction is the fault, and")
                        print("   straight_via_direct_motor should go back on")
                        print("   in elegoo.py.")
                    else:
                        print()
                        print("   It circles even with the correction")
                        print("   bypassed, so the correction is innocent.")
                        print("   That leaves the car: check that all four")
                        print("   wheels turn the same way when you command")
                        print("   forward, with the car held up. If one side")
                        print("   runs backwards, the motor wiring is crossed")
                        print("   and no software change will fix it.")
                    return 1

                findings["min_speed"] = threshold
                if spun:
                    findings["spun_below"] = max(spun)
                    joined = ", ".join(str(x) for x in spun)
                    print(f"\n   Spun rather than drove at: {joined}")
                working = min(180, threshold + 20)
                findings["working_speed"] = working
                print(f"\n   moves from about speed {threshold}.")
                print(f"   Using {working} for the rest, a little above the edge.")

                # ---- 2. distance per burst --------------------------------------
                print(f"\n2. how far it goes per {args.burst_ms} ms burst")
                print("   Five bursts, then measure the total. Five rather than")
                print("   one because a single short burst is dominated by")
                print("   starting and stopping, and the average is what you want.")
                input("   Mark where the front of the car is now, then press Enter. ")

                bursts = 0
                for i in range(5):
                    if not wait_until_clear(guard):
                        print(f"     stopping after {i} burst(s): not enough "
                              f"clear floor left")
                        break
                    verdict = guard.move(Direction.FORWARD, speed=working,
                                         ms=args.burst_ms)
                    if not verdict.allowed or verdict.interrupted:
                        print(f"     burst {i+1}: {verdict.reason}")
                        break
                    bursts += 1
                    time.sleep(0.5)
                print(f"   {bursts} burst(s) completed.")

                total = number(f"how far did it travel in total, over {bursts} "
                               f"bursts", "cm")
                if total == total and bursts:      # not NaN
                    per_burst = total / bursts
                    findings["cm_per_burst"] = per_burst
                    findings["mm_per_ms"] = per_burst * 10 / args.burst_ms
                    print(f"   {per_burst:.1f} cm per {args.burst_ms} ms burst, "
                          f"about {findings['mm_per_ms']:.2f} mm per ms")

                # ---- 3. turning --------------------------------------------------
                print("\n3. how far it turns per 300 ms")
                print("   Four turns of 300 ms. If they came to a right angle,")
                print("   that is 22.5 degrees each; a full circle is 90.")
                input("   Note which way the car is facing, then press Enter. ")

                turns = 0
                for i in range(4):
                    verdict = guard.move(Direction.LEFT, speed=working, ms=300)
                    if not verdict.allowed or verdict.interrupted:
                        print(f"     turn {i+1}: {verdict.reason}")
                        break
                    turns += 1
                    time.sleep(0.5)
                print(f"   {turns} turn(s) completed.")

                degrees = number(f"roughly how many degrees did it rotate in "
                                 f"total, over {turns} turns", "degrees")
                if degrees == degrees and turns:
                    per_turn = degrees / turns
                    findings["deg_per_300ms"] = per_turn
                    findings["deg_per_ms"] = per_turn / 300
                    print(f"   {per_turn:.0f} degrees per 300 ms, about "
                          f"{findings['deg_per_ms']:.3f} degrees per ms")

    except KeyboardInterrupt:
        print("\nstopped by hand")
        return 1
    except (OSError, CarError) as exc:
        print(f"\nfailed: {type(exc).__name__}: {exc}")
        return 2

    # ---- what to do with it ---------------------------------------------
    print("\n" + "=" * 60)
    print("findings")
    for k, v in findings.items():
        print(f"  {k:16s} {v:.2f}" if isinstance(v, float) else
              f"  {k:16s} {v}")

    print("\nPut these in CLAUDE.md, and use the working speed as the default")
    print("in demo_look_around.py and agent.py. The numbers drift with battery")
    print("charge and floor surface, so treat them as the right order of")
    print("magnitude rather than as constants, and re-measure on a different")
    print("floor or a low battery.")
    if "mm_per_ms" in findings:
        ms = findings["mm_per_ms"]
        print(f"\nAt {findings['working_speed']}, 10 cm takes about "
              f"{100 / ms:.0f} ms.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
