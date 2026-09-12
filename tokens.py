#!/usr/bin/env python3
"""
tokens.py - what the runs cost, and whether that agrees with the bill.

    python tokens.py                        # every run, newest last
    python tokens.py --by-day               # totals per UTC day and model
    python tokens.py --last                 # the run you just did
    python tokens.py --run 20260908-215355  # one run, turn by turn
    python tokens.py --reconcile docs/claude_api_tokens_*.csv
    python tokens.py --reconcile docs/claude_api_cost_*.csv

WHY THIS EXISTS

A caching fault ran for three days and cost about four times what it should
have, and nothing in the project noticed. The logs recorded
`cache_read_input_tokens` and not `cache_creation_input_tokens`, so they only
ever showed the cheap half of the bill. An estimate built from them came out at
$0.46 against a real $2.09, and it looked healthy the whole time.

The lesson is not "log the other field", though that is done. It is that **a
number nothing checks against an outside source drifts without telling you.**
So this tool does two separate jobs, and the second is the one that matters:

1. Price the runs from `runs/*/turns.jsonl`, per run, per day and per turn.
2. **Reconcile that against Anthropic's own CSV export**, which is ground
   truth, and print the difference. A gap means the logs are wrong, or a run
   was not logged, or the price table is stale. All three have happened.

Export the CSV from the Anthropic console, Usage, and save it under docs/.
**There are two exports and they check different things**, so take both.
`--reconcile` tells them apart by their header:

- The **token** export is counts. It checks the LOGS: anything the runs failed
  to record shows up as a gap.
- The **cost** export is dollars, split by token type. It checks `PRICES`,
  which is the one input here that the logs cannot contradict, because it is
  typed in from a pricing page. Billed dollars against billed tokens pins every
  rate exactly. Both models and all four rates were confirmed to the cent on
  2026-09-12.

The cost export also shows the caching health in money, which is the form that
is hard to argue with: on 2026-09-09 it was $1.78 written against $0.07 read,
and on 2026-09-12, after the fix, $0.12 written against $0.30 read.

THREE TRAPS, ALL OF WHICH HAVE ALREADY CAUGHT US

- **The CSV is in UTC and the run directories are local time.** An evening run
  here appears on the next day on the bill. `runs/20260908-*` is Anthropic's
  2026-09-09, and pairing them by name attributes a whole day's spend to the
  wrong day. Runs logged since 2026-09-11 carry `started_utc`; older ones are
  converted from the directory name, which assumes this machine's current
  offset and is right for them.
- **Cache writes are most of the bill, and they cost more than fresh input.**
  A 5-minute write is 1.25x the base rate, a read is 0.1x. On 2026-09-09 the
  writes were 85% of the Sonnet bill. Anything reported without them is not an
  underestimate, it is a different number.
- **Missing is not zero.** Runs from before 2026-09-11 have no cache_write
  field at all. Those are marked INCOMPLETE and their cost is printed as a
  floor with a `>`, never as an estimate. Quietly treating a missing field as
  zero is exactly the mistake this file was written after.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import glob
import io
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# Dollars per million tokens. From Anthropic's published pricing, checked
# 2026-09-11.
#
# Cache write is the 5-minute TTL, which is the default and the only one this
# project uses; the 1-hour TTL is 2x base rather than 1.25x, and the CSV has a
# separate column for it, so it is priced separately rather than folded in.
#
# KEEP THIS TABLE HONEST. It is the one input here that cannot be derived from
# the logs, so it is the most likely thing to be silently wrong. --reconcile
# is what catches that: if the table drifts, the difference against the CSV
# stops being noise.
@dataclass(frozen=True)
class Price:
    inp: float
    out: float
    write_5m: float
    write_1h: float
    read: float


PRICES: Dict[str, Price] = {
    "claude-opus-5":    Price(5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-8":  Price(5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-sonnet-5":  Price(2.00, 10.00, 2.50,  4.00, 0.20),
    "claude-fable-5-1": Price(2.00, 10.00, 2.50,  4.00, 0.20),
    "claude-haiku-4-5-20251001": Price(1.00, 5.00, 1.25, 2.00, 0.10),
}
# Short names, so --reconcile still lines up when a run was logged as
# "claude-haiku-4-5" or the CSV shortens something.
PRICES["claude-haiku-4-5"] = PRICES["claude-haiku-4-5-20251001"]


def price_for(model: str) -> Optional[Price]:
    if model in PRICES:
        return PRICES[model]
    # A dated alias, claude-sonnet-5-20260114 and the like.
    for known, price in PRICES.items():
        if model.startswith(known):
            return price
    return None


@dataclass
class Usage:
    """Tokens, in the four categories the bill actually distinguishes."""
    inp: int = 0          # fresh input, neither read from nor written to cache
    out: int = 0
    write_5m: int = 0
    write_1h: int = 0
    read: int = 0
    turns: int = 0
    # False when any turn in this group predates cache_write logging. The cost
    # is then a floor rather than a figure.
    complete: bool = True

    def add(self, other: "Usage") -> None:
        self.inp += other.inp
        self.out += other.out
        self.write_5m += other.write_5m
        self.write_1h += other.write_1h
        self.read += other.read
        self.turns += other.turns
        self.complete = self.complete and other.complete

    @property
    def written(self) -> int:
        return self.write_5m + self.write_1h

    @property
    def hit_rate(self) -> Optional[float]:
        """Share of prompt tokens served from cache.

        The single number worth watching. A healthy run of any length sits
        high, because the system prompt, the tools and every earlier turn
        should be read rather than rewritten. Under about half means something
        is changing the prefix between turns, which is what broke here.

        None when nothing is known, rather than 0, because a run logged before
        cache_write existed cannot answer the question.
        """
        if not self.complete:
            return None
        total = self.read + self.written + self.inp
        return self.read / total if total else None

    def cost(self, model: str) -> Optional[float]:
        price = price_for(model)
        if price is None:
            return None
        return (self.inp * price.inp
                + self.out * price.out
                + self.write_5m * price.write_5m
                + self.write_1h * price.write_1h
                + self.read * price.read) / 1e6


@dataclass
class Run:
    name: str
    model: str
    task: str
    utc_date: str
    usage: Usage
    turn_usage: List[Tuple[int, Usage]] = field(default_factory=list)


def utc_date_of(run_dir: str, meta: Dict) -> str:
    """The UTC day Anthropic will have billed this run on.

    Prefers `started_utc`, written since 2026-09-11. Falls back to parsing the
    directory name as local time and converting, which is correct for the
    older runs as long as this machine's offset has not changed since.
    """
    stamp = meta.get("started_utc")
    if stamp:
        return stamp[:10]
    name = os.path.basename(run_dir.rstrip("/\\"))
    try:
        local = time.strptime(name, "%Y%m%d-%H%M%S")
    except ValueError:
        return "unknown"
    return time.strftime("%Y-%m-%d", time.gmtime(time.mktime(local)))


def read_run(run_dir: str) -> Optional[Run]:
    meta_path = os.path.join(run_dir, "run.json")
    turns_path = os.path.join(run_dir, "turns.jsonl")
    if not os.path.exists(meta_path):
        return None
    with io.open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    total = Usage()
    per_turn: List[Tuple[int, Usage]] = []
    # Turn numbers already counted in this run. A turn that called two tools
    # writes two records, and before 2026-09-11 both carried the full usage of
    # the one response. Counting the first and skipping the rest is correct for
    # those logs and for the current ones, which mark the repeats explicitly.
    # The run loop's turn counter only ever increases, so a repeat is always a
    # second tool call and never a second response.
    counted: set = set()
    if os.path.exists(turns_path):
        with io.open(turns_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue      # a run killed mid-write leaves a part line
                u = record.get("usage") or {}
                # A turn with several tool calls writes one record per call,
                # and only the first carries the usage. See agent.py: the
                # others would otherwise be counted again.
                if "same_as_turn" in u:
                    continue
                number = record.get("turn", len(per_turn) + 1)
                if number in counted:
                    continue
                counted.add(number)
                one = Usage(
                    inp=u.get("input", 0) or 0,
                    out=u.get("output", 0) or 0,
                    write_5m=u.get("cache_write", 0) or 0,
                    read=u.get("cache_read", 0) or 0,
                    turns=1,
                    # The distinction that matters: absent, not zero.
                    complete="cache_write" in u,
                )
                per_turn.append((number, one))
                total.add(one)

    return Run(name=os.path.basename(run_dir.rstrip("/\\")),
               model=meta.get("model", "unknown"),
               task=meta.get("task") or "(no task, listening)",
               utc_date=utc_date_of(run_dir, meta),
               usage=total, turn_usage=per_turn)


def load_runs(root: str) -> Tuple[List[Run], List[str]]:
    """Every run with usage, and the names of those that logged none.

    The second list matters as much as the first. A run that started, called
    the model and then died before writing a turn leaves a run.json and no
    turns.jsonl, and it was still billed. Three of those account for the whole
    remaining gap against the bill on 2026-09-09. Dropping them silently is
    how a reconciliation tool ends up agreeing with itself.
    """
    runs, empty = [], []
    for path in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(path):
            continue
        run = read_run(path)
        if run is None:
            continue
        if run.usage.turns:
            runs.append(run)
        else:
            empty.append(run.name)
    return runs, empty


def money(value: Optional[float], complete: bool = True) -> str:
    if value is None:
        return "   ?   "     # no price for this model
    return f"{'' if complete else '>'}${value:,.2f}"


def show_runs(runs: List[Run]) -> None:
    print(f"{'run':<17} {'model':<16} {'turns':>5} {'in':>7} {'read':>9} "
          f"{'written':>9} {'out':>7} {'hit':>5} {'cost':>9}")
    for run in runs:
        u = run.usage
        rate = u.hit_rate
        print(f"{run.name:<17} {run.model:<16} {u.turns:>5} {u.inp:>7,} "
              f"{u.read:>9,} {u.written:>9,} {u.out:>7,} "
              f"{('  -  ' if rate is None else f'{rate:>4.0%} ')}"
              f"{money(u.cost(run.model), u.complete):>9}")


def show_days(runs: List[Run]) -> None:
    """Grouped the way the bill is: by UTC day and model."""
    groups: Dict[Tuple[str, str], Usage] = defaultdict(Usage)
    counts: Dict[Tuple[str, str], int] = defaultdict(int)
    for run in runs:
        groups[(run.utc_date, run.model)].add(run.usage)
        counts[(run.utc_date, run.model)] += 1

    print(f"{'UTC day':<12} {'model':<16} {'runs':>4} {'turns':>5} {'in':>7} "
          f"{'read':>9} {'written':>9} {'out':>7} {'hit':>5} {'cost':>9}")
    grand = 0.0
    exact = True
    for key in sorted(groups):
        day, model = key
        u = groups[key]
        rate = u.hit_rate
        cost = u.cost(model)
        if cost is not None:
            grand += cost
        exact = exact and u.complete and cost is not None
        print(f"{day:<12} {model:<16} {counts[key]:>4} {u.turns:>5} "
              f"{u.inp:>7,} {u.read:>9,} {u.written:>9,} {u.out:>7,} "
              f"{('  -  ' if rate is None else f'{rate:>4.0%} ')}"
              f"{money(cost, u.complete):>9}")
    print(f"{'':<12} {'':<16} {'':>4} {'':>5} {'':>7} {'':>9} {'':>9} "
          f"{'':>7} {'':>5} {money(grand, exact):>9}")
    if not exact:
        print("\n> means a floor, not a figure: those runs were logged before\n"
              "  cache writes were recorded, and writes are most of the bill.")


def show_turns(run: Run) -> None:
    print(f"{run.name}  {run.model}\n{run.task}\n")
    print(f"{'turn':>4} {'in':>6} {'read':>9} {'written':>9} {'out':>6} "
          f"{'cost':>8}")
    for number, u in run.turn_usage:
        print(f"{number:>4} {u.inp:>6,} {u.read:>9,} {u.written:>9,} "
              f"{u.out:>6,} {money(u.cost(run.model), u.complete):>8}")
    u = run.usage
    print(f"{'all':>4} {u.inp:>6,} {u.read:>9,} {u.written:>9,} {u.out:>6,} "
          f"{money(u.cost(run.model), u.complete):>8}")
    if u.complete:
        # The shape to look for: reads that climb with the conversation. Reads
        # that go flat and then to zero are the caching fault of 2026-09-11.
        print("\nWatch the read column. It should climb with the conversation.\n"
              "Flat and then zero is the prefix being invalidated every turn.")


def read_csv(path: str) -> Dict[Tuple[str, str], Usage]:
    """Anthropic's usage export, keyed the same way as --by-day.

    Their column names are the authority on what the four categories are, so
    they are mapped straight across rather than reinterpreted.
    """
    out: Dict[Tuple[str, str], Usage] = defaultdict(Usage)
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            def num(name: str) -> int:
                value = (row.get(name) or "0").replace(",", "").strip()
                return int(value) if value else 0
            key = (row["usage_date_utc"].strip(),
                   row["model_version"].strip())
            out[key].add(Usage(
                inp=num("usage_input_tokens_no_cache"),
                out=num("usage_output_tokens"),
                write_5m=num("usage_input_tokens_cache_write_5m"),
                write_1h=num("usage_input_tokens_cache_write_1h"),
                read=num("usage_input_tokens_cache_read"),
                turns=0))
    return out


# The cost export writes display names. Map them onto the model ids the runs
# are logged under.
def normalise_model(name: str) -> str:
    return name.strip().lower().replace(" ", "-").replace(".", "-")


# Its token_type column onto the fields of Usage.
COST_FIELDS = {
    "input_no_cache": "inp",
    "output": "out",
    "input_cache_read": "read",
    "input_cache_write_5m": "write_5m",
    "input_cache_write_1h": "write_1h",
}


def is_cost_export(path: str) -> bool:
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        header = csv.DictReader(f).fieldnames or []
    return "cost_usd" in header


def read_cost_csv(path: str) -> Dict[Tuple[str, str], Dict[str, float]]:
    """The console's COST export: dollars, split by token type.

    This is a different and better check than the token export. The token
    counts can be verified against the logs, but PRICES cannot: it is typed in
    from a pricing page and is the one input here that nothing else can
    contradict. Billed dollars against billed tokens pins it exactly.
    """
    out: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(
        lambda: defaultdict(float))
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            field = COST_FIELDS.get((row.get("token_type") or "").strip())
            if field is None:
                continue            # a type we do not price, e.g. web search
            key = (row["usage_date_utc"].strip(),
                   normalise_model(row["model"]))
            value = (row.get("cost_usd") or "0").replace(",", "").strip()
            out[key][field] += float(value or 0)
    return out


def reconcile_cost(runs: List[Run], csv_path: str) -> int:
    """What the logs say each category cost, against what was billed for it.

    A disagreement here is one of two things and the report cannot tell them
    apart on its own: the token counts are wrong, or PRICES is stale. Run the
    token export through --reconcile as well; if the counts agree there and the
    dollars disagree here, it is the price table.
    """
    billed = read_cost_csv(csv_path)
    logged: Dict[Tuple[str, str], Usage] = defaultdict(Usage)
    for run in runs:
        logged[(run.utc_date, run.model)].add(run.usage)

    print(f"checking prices and costs against {csv_path}\n")
    worst = 0.0
    unknown = []
    for key in sorted(set(billed) | set(logged)):
        day, model = key
        b = billed.get(key, {})
        l = logged.get(key)
        price = price_for(model)
        print(f"--- {day}  {model}")
        if price is None:
            print(f"    no price known for this model, so nothing to check")
            unknown.append(model)
            continue
        rates = {"inp": price.inp, "out": price.out, "read": price.read,
                 "write_5m": price.write_5m, "write_1h": price.write_1h}
        # A day whose logs are missing or predate cache_write cannot say
        # anything about PRICES: its gap is a known hole in the logs, not a
        # wrong rate. Counting it would make the report cry wolf on every old
        # day for ever, which is how a check stops being read.
        judgeable = l is not None and l.complete
        print(f"    {'':<12}{'billed':>10}{'from logs':>11}{'diff':>10}")
        for field, rate in rates.items():
            bv = b.get(field, 0.0)
            lv = (getattr(l, field) * rate / 1e6) if l else 0.0
            if bv == 0 and lv == 0:
                continue
            mark = "" if abs(bv - lv) < 0.005 else "   <--"
            print(f"    {field:<12}{bv:>10.2f}{lv:>11.2f}{lv - bv:>+10.2f}{mark}")
            if judgeable:
                worst = max(worst, abs(bv - lv))
        total_b = sum(b.values())
        total_l = l.cost(model) if l else 0.0
        print(f"    {'TOTAL':<12}{total_b:>10.2f}{total_l:>11.2f}"
              f"{total_l - total_b:>+10.2f}")
        if l is None:
            print("    (nothing logged for this day, so only the billed "
                  "column is real)")
        elif not l.complete:
            print("    (some of these runs predate cache_write logging, so "
                  "the gap is a known\n     hole in the logs and says nothing "
                  "about the rates)")

        # The health signal, and the only one visible in dollars alone.
        read_cost, write_cost = b.get("read", 0.0), b.get("write_5m", 0.0)
        if read_cost or write_cost:
            if write_cost > read_cost:
                print(f"    CACHING POOR: ${write_cost:.2f} written against "
                      f"${read_cost:.2f} read.")
                print(f"    A healthy run reads back far more than it writes.")
            else:
                print(f"    caching healthy: ${read_cost:.2f} read against "
                      f"${write_cost:.2f} written.")

    grand = sum(sum(v.values()) for v in billed.values())
    print(f"\nbilled over the whole export: ${grand:,.2f}")
    if unknown:
        print(f"no price known for: {', '.join(sorted(set(unknown)))}")
        return 1
    if worst < 0.01:
        print("every rate in PRICES agrees with the bill to the cent, on every "
              "day whose\nlogs are complete enough to check.")
        return 0
    print(f"largest disagreement on a checkable day: ${worst:.2f}. Either the "
          f"token counts\nare wrong or PRICES is stale. Run the token export "
          f"through --reconcile: if the\ncounts agree there, it is the price "
          f"table.")
    return 1


def reconcile(runs: List[Run], csv_path: str, empty: List[str]) -> int:
    """Logs against the bill, side by side, per UTC day and model.

    This is the whole point of the file. Anything the logs miss shows up here
    as a gap: a run that was not logged, a turn that failed to write, a wrong
    price, or usage from something that is not this project at all.
    """
    # The console offers two exports and they answer different questions. The
    # token one checks the logs; the cost one checks PRICES. Dispatch on the
    # header rather than making the user remember which flag is which.
    if is_cost_export(csv_path):
        return reconcile_cost(runs, csv_path)

    billed = read_csv(csv_path)
    logged: Dict[Tuple[str, str], Usage] = defaultdict(Usage)
    for run in runs:
        logged[(run.utc_date, run.model)].add(run.usage)

    print(f"reconciling against {csv_path}\n")
    keys = sorted(set(billed) | set(logged))
    worst = 0.0
    for key in keys:
        day, model = key
        b, l = billed.get(key), logged.get(key)
        print(f"--- {day}  {model}")
        if b is None:
            print("    logged, but ABSENT FROM THE BILL. Either the export "
                  "predates the run,\n    or the run never reached the API.")
        if l is None:
            print("    billed, but NOTHING LOGGED. Usage from outside this "
                  "project, or a run\n    whose log was deleted.")
        fields = [("input", "inp"), ("cache read", "read"),
                  ("cache written", "written"), ("output", "out")]
        print(f"    {'':<14} {'billed':>12} {'logged':>12} {'diff':>12}")
        for label, attr in fields:
            bv = getattr(b, attr) if b else 0
            lv = getattr(l, attr) if l else 0
            mark = "" if bv == lv else "   <--"
            print(f"    {label:<14} {bv:>12,} {lv:>12,} {lv - bv:>+12,}{mark}")
        bc = b.cost(model) if b else 0.0
        lc = l.cost(model) if l else 0.0
        if bc is None or lc is None:
            print(f"    no price known for {model}, so no cost comparison")
            continue
        note = ""
        if l is not None and not l.complete:
            note = "   (logs predate cache_write; the gap IS the writes)"
        print(f"    {'cost':<14} {money(bc):>12} "
              f"{money(lc, l.complete if l else True):>12} "
              f"{lc - bc:>+12,.2f}{note}")
        worst = max(worst, abs(lc - bc))

    if empty:
        print(f"\n{len(empty)} run(s) logged NOTHING and were still billed for"
              f" whatever they sent\nbefore they died: {', '.join(empty)}."
              f"\nThey are not in the logged column above.")

    total_billed = sum(u.cost(m) or 0 for (d, m), u in billed.items())
    print(f"\nbilled over the whole export: ${total_billed:,.2f}")
    if worst < 0.01:
        print("logs and bill agree.")
    else:
        print(f"largest single-day gap: ${worst:,.2f}. Read the marked rows "
              f"above:\na gap in cache written is the expensive kind.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("WHY THIS EXISTS")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default="runs", help="the run log directory")
    ap.add_argument("--by-day", action="store_true",
                    help="totals per UTC day and model, the way the bill is "
                         "grouped")
    ap.add_argument("--run", help="one run, turn by turn. The timestamped "
                                  "directory name agent.py prints when it "
                                  "starts, e.g. 20260912-094512")
    ap.add_argument("--last", action="store_true",
                    help="the most recent run, turn by turn. What you want "
                         "straight after a run rather than copying its name")
    ap.add_argument("--reconcile", metavar="CSV",
                    help="compare the logs against Anthropic's usage export. "
                         "Console, Usage, export CSV")
    args = ap.parse_args()

    if args.reconcile and not os.path.exists(args.reconcile):
        matches = sorted(glob.glob(args.reconcile))
        if not matches:
            print(f"no such file: {args.reconcile}", file=sys.stderr)
            return 2
        args.reconcile = matches[-1]

    runs, empty = load_runs(args.runs)
    if not runs:
        print(f"no runs with usage in {args.runs}", file=sys.stderr)
        return 1

    if args.last:
        # Newest by name, which sorts correctly because the stamp is
        # year-month-day-hour-minute-second.
        show_turns(sorted(runs, key=lambda r: r.name)[-1])
        return 0
    if args.run:
        for run in runs:
            if run.name == args.run:
                show_turns(run)
                return 0
        print(f"no run named {args.run}. Runs are named for when they "
              f"started; the newest is {sorted(r.name for r in runs)[-1]}, "
              f"and --last picks it for you.", file=sys.stderr)
        return 1
    if args.reconcile:
        return reconcile(runs, args.reconcile, empty)
    if args.by_day:
        show_days(runs)
        return 0
    show_runs(runs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
