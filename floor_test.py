#!/usr/bin/env python3
"""
floor_test.py - read the line sensors over the floors and edges this car will
actually meet, and say whether the cliff stop can tell one from the other.

    python floor_test.py --host 192.168.1.211

Phase 09 of the plan in CLAUDE.md, and the last untested part of phase 04.

WHY THIS IS NOT OPTIONAL

The cliff stop is one number on one channel. `CLIFF_THRESHOLD` is 900 on
channel 2, chosen because hardwood reads about 47 and no floor reads about
1020. That is a wide margin on hardwood and it is not a wide margin anywhere
else: **darker reads higher**.

On 2026-09-11 a run stopped a turn dead with "no floor under the front
sensors" and 122 cm of clear air in front of it, on a patterned rug. The dark
part of that pattern turned out to read 953, over the threshold, against a real
staircase at 1014. Sixty-one counts apart.

**The two failures are not symmetric, and that governs everything here.** A
false stop on a dark rug is an annoyance: the robot refuses to cross it and
says so. A missed edge puts the car down a staircase. So this script checks
both directions and names them differently, and it will not recommend simply
raising the threshold.

HOW TO RUN IT

Name each surface and say whether it is a **floor** the car should drive on or
a **drop** it must stop at. Floors go under the wheels, sitting normally. For a
drop, hold the car with its front overhanging.

**Measure a patterned surface in several places.** A rug read 732 on one spot
and 953 on a darker part of the same pattern, and only the second one explained
the stop that prompted this script. One reading per surface is one reading, not
a survey.

**Measure a REAL edge, not just the car in the air.** Empty space returns
nothing; a staircase has a tread 20 cm down and a table has a floor below, both
of which send something back. A rule calibrated only against thin air is
calibrated against the easy case.

**And measure the same edge more than once.** Different angles and approach
positions, because an edge met at a sharp angle may only reach part of the
sensor array.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from elegoo import Car, CarConfig, CarError, CLIFF_CHANNEL, CLIFF_THRESHOLD

SAMPLES = 12

# How many counts a floor and a drop must differ by before a threshold between
# them counts as real. The existing rule has about 120 counts above it, so
# anything under 150 is thinner than what is already considered marginal here.
# Not a law, a line drawn somewhere defensible: the point is that "a threshold
# exists" and "a threshold is safe" are different claims, and only the second
# one matters when the failure is the car on the floor below.
MIN_SEPARATION = 150

# Printed whenever something fails. It deliberately does not recommend raising
# the threshold, and is careful about the multi-channel idea, because the
# 2026-09-11 survey showed both are worse than they look. See CLAUDE.md phase 09.
ADVICE = '''Do not simply raise the threshold. The two failure directions are
not symmetric: a false stop is an annoyance you notice at once, a missed edge
puts the car on the floor below. Raising the threshold buys the first at the
cost of the second.

Adding a second channel, so a stop needs two of them to agree, carries the same
risk in a subtler form: any AND rule can only ever make the stop LESS likely to
fire. The analysis above says whether one would have worked on the surfaces
measured, which is not the same as saying it is safe.

The reliable answer meanwhile is to keep the car off the surfaces that trip
it.'''


def sample(car, samples: int = SAMPLES) -> Dict[int, List[int]]:
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


@dataclass
class Surface:
    """One surface, and whether the car is meant to be able to cross it.

    The kind is the whole point. An earlier version of this script assumed
    every surface named was a floor, so when a real staircase was finally
    measured it reported the correct answer as a "FALSE STOP". A checker that
    does not know what the right answer is cannot tell you whether you got it.
    """
    name: str
    kind: str                       # "floor" to drive on, "drop" to stop at
    readings: Dict[int, List[int]]

    def worst(self, channel: int) -> int:
        """The highest single sample. The cliff stop fires on one poll, so one
        high reading is a stop and the median is not what decides."""
        return max(self.readings[channel]) if self.readings[channel] else -1

    def lowest(self, channel: int) -> int:
        """The lowest single sample. On a drop this is the weakest the real
        edge signal ever gets, which is the case that still has to work."""
        return min(self.readings[channel]) if self.readings[channel] else -1

    @property
    def is_drop(self) -> bool:
        return self.kind == "drop"


def describe(surface: Surface) -> None:
    cells = []
    for channel in (0, 1, 2):
        values = surface.readings[channel]
        if not values:
            cells.append("   --      ")
            continue
        cells.append(f"{statistics.median(values):>5.0f} "
                     f"({min(values)}-{max(values)})".ljust(12))
    label = f"{surface.name} [{surface.kind}]"
    print(f"{label:<24}{''.join(cells)}")


def verdict(surfaces: List[Surface]) -> int:
    """Whether the cliff rule survives contact with these surfaces.

    Both directions are checked, because they are different failures. A floor
    that reads high is a false stop, which is a nuisance. A drop that reads low
    is a missed edge, which puts the car on the floor below.
    """
    floors = [s for s in surfaces if not s.is_drop]
    drops = [s for s in surfaces if s.is_drop]

    print(f"\nCliff stop: channel {CLIFF_CHANNEL} above {CLIFF_THRESHOLD} "
          f"means no floor.\n")
    bad = False
    for s in surfaces:
        if s.is_drop:
            low = s.lowest(CLIFF_CHANNEL)
            if low <= CLIFF_THRESHOLD:
                print(f"  MISSED EDGE  {s.name}: dipped to {low}, at or under "
                      f"{CLIFF_THRESHOLD}.")
                print(f"               THE CAR WOULD DRIVE OFF THIS.")
                bad = True
            else:
                print(f"  stops        {s.name}: never below {low}, "
                      f"{low - CLIFF_THRESHOLD} above the threshold.")
        else:
            high = s.worst(CLIFF_CHANNEL)
            if high > CLIFF_THRESHOLD:
                print(f"  FALSE STOP   {s.name}: reached {high}, over "
                      f"{CLIFF_THRESHOLD}.")
                print(f"               The car will refuse to cross this.")
                bad = True
            elif high > CLIFF_THRESHOLD - 150:
                print(f"  THIN         {s.name}: reached {high}, only "
                      f"{CLIFF_THRESHOLD - high} below the threshold.")
                bad = True
            else:
                print(f"  ok           {s.name}: reached {high}, "
                      f"{CLIFF_THRESHOLD - high} of margin.")

    if not drops:
        print("\nNo drop measured, so the top of the scale is unknown and "
              "nothing above it\ncan be judged. Re-run and measure a real "
              "edge, or at least the car held\nin the air.")
        return 1
    if not floors:
        print("\nNo floor measured, so there is nothing to tell the drops "
              "apart from.")
        return 1

    top_floor = max((s.worst(CLIFF_CHANNEL), s.name) for s in floors)
    low_drop = min((s.lowest(CLIFF_CHANNEL), s.name) for s in drops)
    gap = low_drop[0] - top_floor[0]
    print(f"\n  highest floor: {top_floor[1]} at {top_floor[0]}")
    print(f"  lowest drop  : {low_drop[1]} at {low_drop[0]}")
    print(f"  separation   : {gap} counts")

    if gap >= MIN_SEPARATION:
        middle = (top_floor[0] + low_drop[0]) // 2
        print(f"\n  A single channel still works here: a threshold of "
              f"{middle} would give\n  {middle - top_floor[0]} counts either "
              f"way on the surfaces measured.")
    elif gap > 0:
        # Caught by a test, which is the only reason this branch exists. The
        # first version called any positive gap a pass, and the real data has a
        # gap of 61 counts: arithmetically separable, nowhere near trustworthy.
        # Readings move with battery, wear and approach angle, and "a threshold
        # exists" is not the same claim as "a threshold is safe".
        print(f"\n  TOO THIN TO TRUST. A threshold could be squeezed into "
              f"those {gap} counts, but\n  that is {gap // 2} either way, and "
              f"readings move with battery, wear and the\n  angle the edge is "
              f"met at. Treat this as no separation.")
        bad = True
        second_channel(surfaces)
    else:
        print("\n  NO THRESHOLD ON THIS CHANNEL CAN SEPARATE THEM. A floor "
              "reads as high as a\n  drop, so the rule cannot be repaired by "
              "moving the number.")
        bad = True
        second_channel(surfaces)

    if bad:
        print("\n" + ADVICE)
        return 1
    print("\nThe threshold holds on every surface tested.")
    return 0


def second_channel(surfaces: List[Surface]) -> None:
    """Whether another channel can break the tie the cliff channel cannot.

    Only the surfaces the cliff channel already calls high are considered. A
    second channel here is a veto on those, not a detector in its own right,
    and that distinction matters: judging a channel on surfaces where channel 2
    was never in doubt is how an earlier reading of this data got the answer
    backwards.
    """
    confused = [s for s in surfaces
                if s.worst(CLIFF_CHANNEL) > CLIFF_THRESHOLD]
    fooled = [s for s in confused if not s.is_drop]
    real = [s for s in confused if s.is_drop]
    if not fooled or not real:
        return

    for channel in (1, 0):
        highest_fooled = max(s.worst(channel) for s in fooled)
        lowest_real = min(s.lowest(channel) for s in real)
        print(f"\n  Channel {channel}, on only those surfaces:")
        for s in confused:
            print(f"    {s.name:<26}{s.worst(channel):>5}  ({s.kind})")
        if lowest_real - highest_fooled <= 0:
            print(f"    -> channel {channel} cannot break the tie either.")
            continue

        middle = (highest_fooled + lowest_real) // 2
        # Every floor, not just the confusing ones. A rule that only narrowly
        # clears the surfaces that happened to get measured is not a rule worth
        # trusting with a staircase.
        worst_floor = max((s.worst(channel)
                           for s in surfaces if not s.is_drop), default=-1)
        print(f"    -> a rule of 'channel {CLIFF_CHANNEL} high AND channel "
              f"{channel} above {middle}' would hold,")
        print(f"       with {lowest_real - middle} counts of margin on a real "
              f"drop and {middle - highest_fooled} on the")
        print(f"       floor that fooled channel {CLIFF_CHANNEL}.")
        if worst_floor >= middle:
            print(f"       BUT another floor reads {worst_floor} on channel "
                  f"{channel}, at or over that line.")
            print(f"       The rule is only safe while such a floor never also "
                  f"fools channel {CLIFF_CHANNEL}.")
        elif middle - worst_floor < 80:
            print(f"       Note the worst floor anywhere reads {worst_floor} "
                  f"here, only {middle - worst_floor}")
            print(f"       below that line.")
        print(f"\n    This rests on the drop measurements above. Measure the "
              f"same edge at\n    several angles and approach positions before "
              f"trusting it: an edge met\n    at a sharp angle may only reach "
              f"part of the sensor array.")
        return


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read the line sensors over real floors and real edges, "
                    "and check the cliff stop against both.")
    ap.add_argument("--host", help="the car's address. Defaults to whatever "
                                   "CarConfig.from_env resolves, as the other "
                                   "scripts do")
    ap.add_argument("--samples", type=int, default=SAMPLES)
    args = ap.parse_args()

    print(__doc__.split("HOW TO RUN IT")[1].strip())
    print("\nName each surface as you go. Empty name to finish.\n")

    surfaces: List[Surface] = []

    cfg = CarConfig.from_env(**({"host": args.host} if args.host else {}))
    print(f"connecting to {cfg.host} ...")

    # No safety layer and no motors: this only reads sensors, so there is
    # nothing to stop and nothing to veto.
    with Car(cfg) as car:
        print(f"{'surface':<24}{'ch0':<12}{'ch1':<12}{'ch2 (cliff)':<12}")
        while True:
            try:
                name = input("\nsurface name (or Enter to stop): ").strip()
                if not name:
                    break
                kind = "drop" if input(
                    "  a [f]loor to drive on, or a [d]rop to stop at? "
                ).strip().lower().startswith("d") else "floor"
                where = ("hold the car with its front overhanging"
                         if kind == "drop" else "put the car on")
                input(f"  {where} {name}, then press Enter: ")
            except (EOFError, KeyboardInterrupt):
                break
            surface = Surface(name, kind, sample(car, args.samples))
            describe(surface)
            surfaces.append(surface)

        try:
            if input("\nhold the car in the air and press Enter "
                     "(or s to skip): ").strip().lower() != "s":
                surface = Surface("held in the air", "drop",
                                  sample(car, args.samples))
                describe(surface)
                surfaces.append(surface)
        except (EOFError, KeyboardInterrupt):
            pass

    if not surfaces:
        print("\nNothing measured.")
        return 1
    return verdict(surfaces)


if __name__ == "__main__":
    sys.exit(main())
