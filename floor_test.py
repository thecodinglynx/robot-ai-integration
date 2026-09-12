#!/usr/bin/env python3
"""
floor_test.py - read the line sensors over the floors this car will actually
drive on, and say whether the cliff stop can tell them from a real edge.

    python floor_test.py --host 192.168.1.211

Phase 09 of the plan in CLAUDE.md, and the last untested part of phase 04.

WHY THIS IS NOT OPTIONAL

The cliff stop is one number on one channel. `CLIFF_THRESHOLD` is 900 on
channel 2, chosen because hardwood reads about 47 and no floor reads about
1020. That is a wide margin on hardwood and it is not a wide margin anywhere
else: **darker reads higher**, and the darkest surface measured when the
threshold was set already read 802. Ninety-eight counts of margin.

On 2026-09-11 a run stopped a turn dead with "no floor under the front
sensors" and 122 cm of clear air in front of it, on a patterned rug. Nothing
recorded how close to 900 that rug was, because only the verdict was logged and
not the reading. Both are logged now, and this script is how you get the number
without having to wait for the robot to refuse something.

**The two failures are not symmetric, and that governs how to read the output.**
A false stop on a dark rug is an annoyance: the robot refuses to cross it and
says so. A missed edge puts the car down a staircase. So a surface that reads
anywhere near the threshold is a reason to act, and the action is not
automatically "raise the threshold": raising it eats the margin at the top,
which is the end that breaks things.

HOW TO RUN IT

Put the car on each surface in turn, wheels down, sitting normally. Then hold
it in the air for the no-floor reading, which is the one everything is measured
against. It reads all three channels, unlike the hot path, which reads only
channel 2 to stay inside the serial link's budget.

**Measure a patterned surface in several places.** On 2026-09-11 a rug read 732
on one spot and 953 on a darker part of the same pattern, and only the second
one explains the stop that prompted this script. One reading per surface is one
reading, not a survey.

**And measure a REAL edge, not just the car in the air.** Empty space returns
nothing; a staircase has a tread 20 cm down and a table has a floor below, both
of which send something back. Every no-floor figure in CLAUDE.md is the car
held up, which may well be the easy case. Hold the car with its front
overhanging the actual drop and name that as a surface.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from typing import Dict, List, Optional, Tuple

from elegoo import Car, CarConfig, CarError, CLIFF_CHANNEL, CLIFF_THRESHOLD

SAMPLES = 12

# Printed whenever a surface fails. It deliberately does not recommend raising
# the threshold, and is careful about the multi-channel idea, because the
# 2026-09-11 survey showed both are worse than they look. See CLAUDE.md phase 09.
ADVICE = '''Do not simply raise the threshold. The two failure directions are
not symmetric: a false stop is an annoyance you notice at once, a missed edge
puts the car on the floor below. Raising the threshold buys the first at the
cost of the second.

Adding a second channel, so a stop needs two of them to agree, is the
better-looking fix and carries the same risk in a subtler form: any AND rule
can only ever make the stop LESS likely to fire. Before trusting one, measure a
real edge on all three channels, because every no-floor number this project has
is the car held in the air, and empty space is the easy case.

The reliable answer meanwhile is to keep the car off the surfaces that trip
it.'''


def sample(car: Car, samples: int = SAMPLES) -> Dict[int, List[int]]:
    """All three channels, several times, because one reading is not a finding.

    The firmware appears to cache readings on its own clock, so repeats can be
    identical without the sensor being stuck. Spread matters as much as level:
    a channel that never moves at all on a surface is worth a second look.
    """
    out: Dict[int, List[int]] = {0: [], 1: [], 2: []}
    for _ in range(samples):
        for channel in (0, 1, 2):
            try:
                out[channel].append(car.line_sensor(channel))
            except (CarError, OSError) as exc:
                print(f"  read failed on channel {channel}: {exc}")
    return out


def describe(name: str, readings: Dict[int, List[int]]) -> Tuple[str, int, int]:
    """One line per surface, plus the numbers the verdict needs."""
    cells = []
    for channel in (0, 1, 2):
        values = readings[channel]
        if not values:
            cells.append("   --      ")
            continue
        cells.append(f"{statistics.median(values):>5.0f} "
                     f"({min(values)}-{max(values)})".ljust(12))
    two = readings[CLIFF_CHANNEL]
    median = int(statistics.median(two)) if two else -1
    worst = max(two) if two else -1
    print(f"{name:<22}{''.join(cells)}")
    return name, median, worst


def verdict(rows: List[Tuple[str, int, int]], air: Optional[int]) -> int:
    """Whether the threshold survives contact with these floors."""
    print(f"\nCliff stop: channel {CLIFF_CHANNEL} above {CLIFF_THRESHOLD} means "
          f"no floor.\n")
    bad = False
    for name, median, worst in rows:
        # The worst single sample decides it, not the median. The cliff stop
        # fires on one poll, so one high reading is a stop.
        if worst > CLIFF_THRESHOLD:
            print(f"  FALSE STOP   {name}: reached {worst}, over {CLIFF_THRESHOLD}. "
                  f"The car will refuse to cross this.")
            bad = True
        elif worst > CLIFF_THRESHOLD - 150:
            print(f"  THIN         {name}: reached {worst}, only "
                  f"{CLIFF_THRESHOLD - worst} below the threshold.")
            bad = True
        else:
            print(f"  ok           {name}: reached {worst}, "
                  f"{CLIFF_THRESHOLD - worst} of margin.")

    if air is None:
        print("\nNo air reading taken, so the top of the scale is unmeasured "
              "and\nnothing above can be judged against it. Re-run and hold "
              "the car up.")
        return 1

    print(f"\n  no floor at all: {air}. That is the number a real edge has to "
          f"beat.")
    headroom = air - CLIFF_THRESHOLD
    print(f"  margin above the threshold: {headroom}")
    if headroom < 100:
        print("  THIN AT THE TOP. Raising the threshold to clear a dark floor "
              "would\n  start eating the margin that stops the car at a real "
              "edge.")
        bad = True

    if bad:
        print("\n" + ADVICE)
        return 1
    print("\nThe threshold holds on every surface tested.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read the line sensors over real floors and check the "
                    "cliff stop against them.")
    ap.add_argument("--host", help="the car's address. Defaults to whatever "
                                   "CarConfig.from_env resolves, as the other "
                                   "scripts do")
    ap.add_argument("--samples", type=int, default=SAMPLES)
    args = ap.parse_args()

    print(__doc__.split("HOW TO RUN IT")[1].strip())
    print("\nName each surface as you go. Empty name to finish.\n")

    rows: List[Tuple[str, int, int]] = []
    air: Optional[int] = None

    cfg = CarConfig.from_env(**({"host": args.host} if args.host else {}))
    print(f"connecting to {cfg.host} ...")

    # No safety layer and no motors: this only reads sensors, so there is
    # nothing to stop and nothing to veto.
    with Car(cfg) as car:
        print(f"{'surface':<22}{'ch0':<12}{'ch1':<12}{'ch2 (cliff)':<12}")
        while True:
            try:
                name = input("\nsurface name (or Enter to stop): ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not name:
                break
            input(f"  put the car on {name}, wheels down, then press Enter: ")
            rows.append(describe(name, sample(car, args.samples)))

        try:
            if input("\nhold the car in the air and press Enter "
                     "(or s to skip): ").strip().lower() != "s":
                readings = sample(car, args.samples)
                describe("held in the air", readings)
                # The minimum, not the median: the lowest the air reading ever
                # dips to is the weakest the real-edge signal ever gets.
                air = min(readings[CLIFF_CHANNEL]) if readings[CLIFF_CHANNEL] \
                    else None
        except (EOFError, KeyboardInterrupt):
            pass

    if not rows:
        print("\nNo surfaces measured.")
        return 1
    return verdict(rows, air)


if __name__ == "__main__":
    sys.exit(main())
