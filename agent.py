#!/usr/bin/env python3
"""
agent.py - the model loop. Phase 05 of the plan in CLAUDE.md.

The slow loop. A vision model gets a still frame, the current sensor readings
and the outcome of its last action, and replies with one tool call from a small
vocabulary. The host executes it through the safety layer and hands back what
happened, along with a fresh frame.

    pip install anthropic
    set ANTHROPIC_API_KEY=sk-ant-...
    python agent.py --host 192.168.1.211 --task "find the red box and drive to it"

WHY A MANUAL LOOP RATHER THAN THE SDK'S TOOL RUNNER

The tool runner is the better default for most agents. This loop needs three
things it does not give us: pacing, so turns cannot run faster than the robot
can act; a turn budget and a host-owned stop, because this thing moves; and a
full record of every request, response, frame and outcome written as it goes,
so a run can be replayed afterwards. Phase 06 in the plan is that logging, and
it is folded in here rather than bolted on later, because the runs worth
learning from are the early ones where the prompt is worst.

WHAT THE MODEL CAN AND CANNOT DO

Every action is short and bounded, and re-observing after each one is what
absorbs the open loop drift, since there are no wheel encoders.

Note what model latency does and does not cost here. The car is stationary
while the model thinks: a move is issued, it completes, then the next turn
begins. So a slow model makes a run tedious to watch, it does not make the car
less safe. The research's warning about travelling 200 to 800 mm while the
model thinks applies to a design where the model steers continuously; this one
does not. Safety is the reflex layer's job, and it runs at 10 Hz regardless.

The safety layer holds a veto over every movement and can stop one in flight.
Refusals come back as ordinary tool results with the reason attached, because a
refusal is information the model should reason about, not an error.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from elegoo import (Car, CarConfig, CarError, Direction,
                    TILT_MIN, TILT_MAX, TILT_SCAN, TILT_DRIVING,
                    DRIVE_SPEED, TURN_SPEED, DRIVE_SPEED_MAX,
                    TURN_DEG_PER_MS, DRIVE_MM_PER_MS)
from safety import SafetyLoop, SafetyConfig
from voice import Voice

try:
    import anthropic
except ImportError:          # importable without the SDK, so the robot side
    anthropic = None         # can be tested without credentials

# Opus 5 by default. Not because the task demands it, but because a weaker
# model failing is hard to tell apart from a bad prompt, and the first job is
# to establish that the loop works at all. Cost is not the deciding factor at
# this scale: a twenty turn run is under a dollar on Opus and about a third of
# that on Sonnet. Step down once there is a baseline to compare against.
DEFAULT_MODEL = "claude-opus-5"

PAN_MIN, PAN_MAX, PAN_CENTRE = 62, 118, 90

SYSTEM = """You are driving a small four wheeled robot around a room, through a
camera mounted 16 cm off the floor on a head that can pan and tilt.

Your job each turn is to call exactly one tool. You will then be shown what
happened and a fresh photograph, and you decide again. Keep going until you have
finished the task, then call report.

WHAT YOU ARE WORKING WITH

The camera is your only way to identify anything. An ultrasonic range finder
points the same way as the camera and gives you a distance in centimetres to
whatever is in front of it. It is a single narrow ray, not a depth image: it
tells you how far away the thing you are looking at is, and nothing about
anything either side of it.

A range of "far" means the sensor got no echo back. That usually means nothing
is within about two metres, but it can also mean the surface is at an angle that
scattered the pulse, or soft enough to absorb it. Do not read "far" as proof
that the way ahead is clear.

MOVEMENT IS BLIND AND APPROXIMATE

There are no wheel encoders. You cannot ask how far you have travelled or which
way you are facing, and nothing remembers where you have been. Movements are
timed, not measured. Measured on hardwood with a charged battery:

  driving   0.55 mm per ms, so 400 ms is about 22 cm and 10 cm is about 180 ms
  turning   0.17 degrees per ms, so 400 ms is about 68 degrees and a right
            angle is about 530 ms

Both vary with battery charge and floor surface, and two identical commands
will not produce identical movements.

**TURNING ALSO MOVES YOU.** This robot cannot rotate on the spot. It turns by
driving one side and holding the other still, so it swings around a stopped
wheel: a 400 ms turn also carries you something like 15 to 20 cm sideways and
forward through the arc. Never treat a turn as a way of looking around without
changing position. To look without moving, aim the head instead.

Work with that rather than against it. Move a little, look again, correct. Do
not plan several moves ahead and execute them blind, because the error compounds
and you have no way to detect it.

A SAFETY SYSTEM CAN OVERRULE YOU

A reflex layer polls the sensors ten times a second and will refuse a movement,
or cut one short, if it would put the robot into something or over an edge. You
will be told when this happens and why. It is not a malfunction and not
something to work around: it sees a great deal faster than you do. If it refuses
a move forward, the answer is to turn or back away, not to ask again.

START BY LOOKING AROUND

Your first action should almost always be `scan`. It sweeps the head across
five positions and returns what it sees at each, without moving the robot at
all. One scan tells you more about where you are than three drives do, and it
costs nothing but a turn.

Decide where to go from the scan. Do not start driving on the strength of the
single frame you happen to open with; that frame is one narrow view of a room
you have not looked at yet.

SEARCHING AND AIMING ARE DIFFERENT KINDS OF TURN

**Searching: turn big.** The head only pans 62 to 118, so one `scan` covers
about 56 degrees plus the width of the lens, call it a quarter of the way
round. If a scan does not find what you are looking for, there is nothing to be
learned by nudging: turn a **full 90 degrees, about 530 ms**, and scan the next
quarter. Three or four of those cover the whole room. Small turns while
searching just re-examine ground you have already seen.

**Aiming: turn small and computed.** Once you can see the target, the turn is
not a guess. Take the pan angle it was centred at, subtract 90, and turn by
that many degrees. See below.

Know which one you are doing. A 40 degree turn is too big for aiming and too
small for searching.

One caveat while searching: turning moves the car, so four search turns will
also walk you some way around the room rather than spinning you on the spot.
That is usually fine when looking for something, but do not expect to end up
where you started.

FACING SOMETHING IS A SEPARATE JOB FROM SEEING IT

The head turns independently of the car. Seeing the target does not mean you
are pointing at it, and a task like "drive to the ball" or "come to me" is not
finished until the CAR faces the target, not just the camera.

**The head's pan angle tells you exactly how far off the car is.** Pan 90 is
straight ahead. If the target is centred in frame while the head is at pan 118,
the car is 28 degrees off to the right; at pan 66, it is 24 degrees off to the
left. Divide by 0.17 to get the turn in milliseconds, so 28 degrees is about
165 ms. Every look and every scan tells you this offset.

So the loop for facing something is:

  1. find it, with `scan` or `look`
  2. note the pan angle it was centred at
  3. turn the body by that offset
  4. `look` at pan 90 and confirm it is now centred in the frame
  5. only then drive

If step 4 shows it still off to one side, turn again by the smaller remaining
offset. Two corrections is normal; the turn is open loop.

**Do not finish a run with the head off centre.** If you are reporting success
on a task about reaching or facing something, the last thing you do before
reporting should be a look at pan 90 that shows the target centred and close.

WHERE TO AIM THE HEAD

`tilt` runs 60 to 120, and **higher numbers look further down**:

  70 to 90     across the room. Use this to find things.
  100 to 115   the floor just ahead. Use this for the last approach, or to
               check what is immediately in front of you.

If your frames are full of floor and you cannot see anything useful, your tilt
is too high: come back to 80 and look again. If you are seeing mostly ceiling
or wall, it is too low.

HOW TO BEHAVE

Say briefly what you can see and what you intend before each tool call. Be
concrete: "a red box on the left, about a third of the way up the frame" beats
"I see the target".

Every action returns a fresh frame, so you do not need to call `look` after
moving just to see where you are. Use `look` and `scan` to aim somewhere new,
not to refresh the picture.

**Panning the head is not searching.** A previous run spent eight of its ten
turns panning the head one position at a time. `scan` covers the whole range in
a single turn and puts the head back at centre. Use it, then act on what it
found. If you have looked twice without acting, you are stalling.

If something is not visible, aim the head to look around before driving or
turning. Aiming the head is free and does not move the robot. Turning the body
does move it, so it is not a cheap way to search.

When you have achieved the task, or concluded you cannot, call report and stop.
Do not keep going indefinitely."""


def tool_definitions() -> List[Dict[str, Any]]:
    """The whole vocabulary. Deliberately small.

    Durations rather than distances and angles, because that is what the robot
    actually accepts. Presenting a `distance_cm` parameter would be a fiction:
    nothing here can measure distance travelled.
    """
    return [
        {
            "name": "drive",
            "description": (
                "Drive straight for a short time. About 22 cm per 400 ms at "
                "speed 120, varying with battery and floor. The safety layer "
                "may refuse this or cut it short."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string",
                                  "enum": ["forward", "backward"]},
                    "ms": {"type": "integer", "minimum": 100, "maximum": 600,
                           "description": "how long to drive, milliseconds"},
                    "speed": {"type": "integer", "minimum": 120,
                              "maximum": DRIVE_SPEED_MAX,
                              "description": "leave this alone unless "
                                             "you have a reason"},
                },
                "required": ["direction", "ms"],
                "additionalProperties": False,
            },
        },
        {
            "name": "turn",
            "description": (
                "Change which way you are facing. About 68 degrees per "
                "400 ms. NOT a rotation on the spot: the robot swings around "
                "a stopped wheel, so a turn moves you 15 to 20 cm as well as "
                "turning you. Turning is how you face somewhere you cannot "
                "reach by panning the head; it is not a way to look around "
                "cheaply. Searching: use about 530 ms, a 90 degree step, to "
                "bring a fresh quarter of the room into view. Aiming at "
                "something you can already see: compute the milliseconds from "
                "the head's pan offset instead of guessing."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["left", "right"]},
                    "ms": {"type": "integer", "minimum": 100, "maximum": 600},
                    # No speed knob. Turning is a tank turn at a calibrated
                    # 170, and the degrees-per-ms figure above only holds at
                    # that speed. The first run had the model choosing 100,
                    # which is not a speed this car turns reliably at and
                    # which silently invalidates its own arithmetic.
                },
                "required": ["direction", "ms"],
                "additionalProperties": False,
            },
        },
        {
            "name": "look",
            "description": (
                f"Aim the head and take a photograph. Pan {PAN_MIN} to "
                f"{PAN_MAX}, where {PAN_CENTRE} is straight ahead, lower is "
                f"left and higher is right. Tilt {TILT_MIN} to {TILT_MAX}, "
                f"where higher looks further down. Free and instant compared "
                f"with driving, so look before you move."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pan": {"type": "integer",
                            "minimum": PAN_MIN, "maximum": PAN_MAX},
                    "tilt": {"type": "integer",
                             "minimum": TILT_MIN, "maximum": TILT_MAX},
                },
                "required": ["pan"],
                "additionalProperties": False,
            },
        },
        {
            "name": "scan",
            "description": (
                "Sweep the head across its full range, taking a distance "
                "reading at each stop, and photograph the centre. Use this to "
                "get the shape of what is around you in one turn instead of "
                "several looks."),
            "input_schema": {"type": "object", "properties": {},
                             "additionalProperties": False},
        },
        {
            "name": "stop",
            "description": "Stop the wheels immediately.",
            "input_schema": {"type": "object", "properties": {},
                             "additionalProperties": False},
        },
        {
            "name": "report",
            "description": (
                "Finish the run and say what happened. Call this when the task "
                "is done, or when you are confident it cannot be done."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "outcome": {"type": "string",
                                "enum": ["success", "failed", "gave_up"]},
                    "summary": {"type": "string",
                                "description": "what happened, in a sentence "
                                               "or two"},
                },
                "required": ["outcome", "summary"],
                "additionalProperties": False,
            },
        },
    ]


class RunLog:
    """Everything needed to replay a run afterwards.

    Without this, prompt tuning is guesswork: no two physical runs are the
    same, so a change that looks like an improvement may just be a different
    room, a different battery, or a different starting angle.
    """

    def __init__(self, root: str, task: str, model: str):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.dir = os.path.join(root, stamp)
        os.makedirs(os.path.join(self.dir, "frames"), exist_ok=True)
        self.turns_path = os.path.join(self.dir, "turns.jsonl")
        self.started = time.time()
        with open(os.path.join(self.dir, "run.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"task": task, "model": model, "system": SYSTEM,
                       "tools": tool_definitions(),
                       "started": stamp}, f, indent=2)
        print(f"logging to {self.dir}")

    def frame(self, turn: int, jpeg: bytes, tag: str = "") -> str:
        name = f"{turn:03d}{'_' + tag if tag else ''}.jpg"
        path = os.path.join(self.dir, "frames", name)
        with open(path, "wb") as f:
            f.write(jpeg)
        return name

    def turn(self, record: Dict[str, Any]) -> None:
        record["t"] = round(time.time() - self.started, 3)
        with open(self.turns_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


@dataclass
class Outcome:
    """What a tool call did, in a form the model can reason about."""
    text: str
    frame: Optional[bytes] = None
    is_error: bool = False
    # Several labelled photographs, for `scan`. A scan that returns only the
    # view straight ahead is nearly useless for finding things: on 2026-09-08
    # the head was pointed directly at the person the model was hunting for,
    # the model was handed a distance instead of a picture, concluded the room
    # was empty and turned away from it.
    frames: Optional[List[Tuple[str, bytes]]] = None


def bearing_note(pan: int) -> str:
    """What a pan angle means for where the CAR is pointing.

    Pan 90 is straight ahead, so the offset from 90 is the angle the body is
    off by, and TURN_DEG_PER_MS converts that into a turn duration. This is
    exact rather than estimated: the pan angle is commanded, not inferred.

    It exists because the first runs had the model find its target by panning
    the head and then declare success from a sideways glance, with the car
    still facing somewhere else entirely. Nothing had told it that pan offset
    is bearing error.
    """
    off = pan - PAN_CENTRE
    if abs(off) <= 3:
        return ("The head is centred, so whatever is in the middle of this "
                "frame is straight in front of the car.")
    side = "right" if off > 0 else "left"
    ms = int(round(abs(off) / TURN_DEG_PER_MS))
    return (f"The head is {abs(off)} degrees {side} of straight ahead, so "
            f"whatever is centred in this frame is {abs(off)} degrees {side} "
            f"of where the CAR is pointing. To face it, turn {side} for about "
            f"{ms} ms, then look at pan 90 to check.")



class Pilot:
    def __init__(self, car: Car, guard: SafetyLoop, log: RunLog,
                 survey_tilt: int = TILT_SCAN):
        self.car = car
        self.guard = guard
        self.log = log
        self.survey_tilt = survey_tilt
        self.finished: Optional[Dict[str, Any]] = None

    # ---------- observation ----------

    def sensors(self) -> str:
        r = self.guard.reading
        if r is None:
            return "sensors: no reading"
        distance = "far, no echo" if r.at_ceiling else f"{r.distance_cm:.0f} cm"
        edge = ", NO FLOOR under the front sensors" if r.over_edge else ""
        pan, tilt = self.car.aim
        head = ("head position not yet set" if pan is None and tilt is None
                else f"head at pan {pan}, tilt {tilt}")
        return f"range {distance}, {head}{edge}"

    def frame(self) -> Optional[bytes]:
        try:
            return self.car.capture()
        except OSError as exc:
            print(f"  (capture failed: {exc})")
            return None

    # ---------- actions ----------

    def execute(self, name: str, args: Dict[str, Any]) -> Outcome:
        try:
            handler = getattr(self, f"_do_{name}")
        except AttributeError:
            return Outcome(f"no such tool: {name}", is_error=True)
        try:
            outcome = handler(args)
        except CarError as exc:
            return Outcome(f"the robot stopped responding: {exc}",
                           is_error=True)

        # Every action comes back with a fresh view, which is what the design
        # says and what the first run showed it was not doing. Only look and
        # scan attached a frame, so the model had to spend a whole turn on
        # look after every drive just to see what had happened, and half the
        # budget went on that. Nothing here is worth deciding from a stale
        # picture, so the picture comes with the answer.
        if outcome.frame is None and not outcome.is_error and name != "report":
            outcome.frame = self.frame()
        return outcome

    def _do_drive(self, args) -> Outcome:
        direction = (Direction.FORWARD if args["direction"] == "forward"
                     else Direction.BACKWARD)
        ms = int(args["ms"])
        speed = int(args.get("speed", DRIVE_SPEED))
        verdict = self.guard.move(direction, speed=speed, ms=ms)
        if not verdict.allowed:
            return Outcome(f"REFUSED by the safety layer: {verdict.reason}. "
                           f"You did not move.")
        if verdict.interrupted:
            return Outcome(f"Started moving but the safety layer stopped you "
                           f"early: {verdict.reason}. You moved less than "
                           f"asked, by an unknown amount.")
        # The safety layer may shorten a move to the clear space ahead. Report
        # what actually happened rather than what was asked for, or the model's
        # own dead reckoning goes wrong without it ever knowing.
        actual = verdict.ms or ms
        cm = round(actual * DRIVE_MM_PER_MS / 10)
        note = ("" if actual == ms
                else f" ({verdict.reason})")
        return Outcome(f"Drove {args['direction']} for {actual} ms, roughly "
                       f"{cm} cm.{note}")

    def _do_turn(self, args) -> Outcome:
        direction = (Direction.LEFT if args["direction"] == "left"
                     else Direction.RIGHT)
        ms = int(args["ms"])
        # Always the calibrated turn speed; the tool exposes no knob for it.
        verdict = self.guard.move(direction, speed=TURN_SPEED, ms=ms)
        if not verdict.allowed:
            return Outcome(f"REFUSED by the safety layer: {verdict.reason}.")
        if verdict.interrupted:
            return Outcome(f"Turn stopped early: {verdict.reason}.")
        degrees = round(ms * TURN_DEG_PER_MS)
        return Outcome(f"Turned {args['direction']} for {ms} ms, roughly "
                       f"{degrees} degrees. You also moved through the arc.")

    def _do_look(self, args) -> Outcome:
        pan = max(PAN_MIN, min(PAN_MAX, int(args["pan"])))
        self.car.pan(pan)
        tilt = args.get("tilt")
        if tilt is not None:
            self.car.tilt(int(tilt))
        time.sleep(0.5)         # servo travel plus a frame for the exposure
        return Outcome(f"Looking at pan {pan}"
                       + (f", tilt {tilt}" if tilt is not None else "") + ".",
                       frame=self.frame())

    def _do_scan(self, args) -> Outcome:
        """Sweep the head and bring back what it saw, in pictures.

        The first version returned distances at five head positions and one
        photograph, straight ahead. That is a range finder sweep, not a look
        around, and it cost a run: the head was pointed at the target, the
        model got a number rather than an image, and it turned away.

        Three photographs rather than five to keep the cost down. The
        in-between positions still report their distance, and the frames are
        pruned out of the conversation a few turns later like any other.
        """
        lines = []
        shots: List[Tuple[str, bytes]] = []
        # Survey at the survey tilt, whatever the last look left the head at.
        # A scan taken while the head is still aimed at the floor from an
        # approach is a scan of the floor.
        self.car.tilt(self.survey_tilt)
        photograph_at = (PAN_MIN, PAN_CENTRE, PAN_MAX)
        for pan in (PAN_MIN, 76, PAN_CENTRE, 104, PAN_MAX):
            self.car.pan(pan)
            time.sleep(0.45)
            r = self.guard.reading
            off = pan - PAN_CENTRE
            where = ("straight ahead" if off == 0
                     else f"{abs(off)} deg {'right' if off > 0 else 'left'}")
            if r is None:
                lines.append(f"  pan {pan} ({where}): no reading")
            else:
                d = "far" if r.at_ceiling else f"{r.distance_cm:.0f} cm"
                lines.append(f"  pan {pan} ({where}): {d}")
            if pan in photograph_at:
                jpeg = self.frame()
                if jpeg:
                    shots.append((f"pan {pan}, looking {where}", jpeg))
        self.car.pan(PAN_CENTRE)
        time.sleep(0.3)
        text = ("Swept the head across its range:\n"
                + "\n".join(lines)
                + "\nThe photographs below are left, centre and "
                  "right in that order, and the head is back at centre "
                  "now.\nAnything in the left or right picture is "
                  "off to that side of the CAR, not just of the camera, "
                  "by the degrees in its caption.")
        return Outcome(text, frames=shots)

    def _do_stop(self, args) -> Outcome:
        self.car.stop()
        return Outcome("Stopped.")

    def _do_report(self, args) -> Outcome:
        self.finished = {"outcome": args.get("outcome", "gave_up"),
                         "summary": args.get("summary", "")}
        return Outcome("Run ended.")


# esp32-camera framesize values. Anthropic bills an image at roughly
# width * height / 750 tokens, so the resolution choice is a direct multiplier
# on the cost of every turn.
FRAMESIZES = {
    "qqvga": (1, "160x120", 26),
    "hqvga": (3, "240x176", 56),
    "qvga":  (5, "320x240", 102),
    "cif":   (6, "400x296", 158),
    "hvga":  (7, "480x320", 205),
    "vga":   (8, "640x480", 410),
    "svga":  (9, "800x600", 640),
}


def prune_images(messages: List[Dict[str, Any]], keep: int) -> int:
    """Replace all but the newest `keep` images with a one-line placeholder.

    This is the single biggest cost lever in the loop. Every frame stays in
    the conversation for the rest of the run, so turn 10 re-sends all ten
    pictures and a 20 turn run pays for 210 image-copies to look at 20
    pictures. Cost grows with the square of the run length.

    Older frames are worth very little anyway: the robot has moved since, so
    they describe somewhere it no longer is. What the model needs from them is
    the fact that it looked and roughly what it saw, and the text of each
    outcome already carries that.

    Returns how many were dropped. The placeholder is stable once written, so
    the cacheable prefix keeps growing rather than being invalidated wholesale
    each turn.
    """
    seen = 0
    dropped = 0
    for message in reversed(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            # tool_result blocks carry their own content list
            inner = (block.get("content")
                     if isinstance(block, dict) and
                     isinstance(block.get("content"), list) else None)
            targets = inner if inner is not None else [block]
            for i in range(len(targets) - 1, -1, -1):
                item = targets[i]
                if not (isinstance(item, dict) and item.get("type") == "image"):
                    continue
                seen += 1
                if seen > keep:
                    targets[i] = {
                        "type": "text",
                        "text": "[an earlier photograph was here; it has been "
                                "dropped to save cost. You are past it now.]"}
                    dropped += 1
    return dropped


def tool_result_content(outcome, sensors_text, log, turn):
    """Assemble what goes back to the model after one action.

    Pulled out of the loop so it can be tested. It was inline, and a change
    that taught it about multi-frame outcomes went in while the matching field
    on `Outcome` did not: every unit test passed, because they all call the
    tool handlers directly and none of them exercised the message assembly.
    The run died on the first `scan`.

    Returns (content blocks, a name for the log).
    """
    content = [{"type": "text",
                "text": f"{outcome.text}\n\n{sensors_text}"}]
    if getattr(outcome, "frames", None):
        names = []
        for i, (label, jpeg) in enumerate(outcome.frames):
            names.append(log.frame(turn, jpeg, f"scan{i}"))
            content.append({"type": "text", "text": f"[{label}]"})
            content.append(image_block(jpeg))
        return content, ", ".join(names)
    if outcome.frame:
        content.append(image_block(outcome.frame))
        return content, log.frame(turn, outcome.frame)
    return content, None


def image_block(jpeg: bytes) -> Dict[str, Any]:
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.standard_b64encode(jpeg).decode()}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--task", required=True,
                    help='what to attempt, e.g. "find the red box and drive '
                         'to it"')
    ap.add_argument("--no-voice", action="store_true",
                    help="do not speak the narration aloud")
    ap.add_argument("--voice-rate", type=int, default=None,
                    help="speaking speed in words per minute, around 200 is "
                         "normal. Faster keeps the speech in step with a "
                         "robot that is still moving")
    ap.add_argument("--keep-frames", type=int, default=3,
                    help="how many recent photographs stay in the "
                         "conversation, default %(default)s. Every frame is "
                         "re-sent on every later turn, so this is the biggest "
                         "single lever on cost. Older frames describe "
                         "somewhere the robot no longer is")
    ap.add_argument("--framesize", default="qvga",
                    choices=sorted(FRAMESIZES),
                    help="camera resolution, default %(default)s. An image "
                         "costs about width*height/750 tokens: qvga is ~102, "
                         "vga ~410, so this is a four times difference on "
                         "every frame")
    ap.add_argument("--tilt", type=int, default=None,
                    help="head tilt to start at and to survey with, 60 to "
                         "120, LOWER looks further up. Default is the "
                         "TILT_SCAN in elegoo.py. The mapping shifts when the "
                         "servo horn slips, so if the frames are full of floor "
                         "try a smaller number")
    ap.add_argument("--stop-cm", type=float, default=None,
                    help="how close the safety layer lets the car get to "
                         "something in front, default 15. Lower makes the "
                         "robot bolder and leaves less margin: reaction "
                         "distance is about 10 cm at the default drive speed")
    ap.add_argument("--turns", type=int, default=20,
                    help="hard cap on model turns, default 20")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"which model drives. Default {DEFAULT_MODEL}. "
                         f"claude-sonnet-5 is cheaper and quicker and is "
                         f"probably enough for a brightly coloured target; "
                         f"compare turns-to-completion against a logged Opus "
                         f"run before trusting it")
    ap.add_argument("--effort", default="low",
                    choices=["low", "medium", "high", "xhigh", "max"],
                    help="how hard the model thinks per turn. Low keeps the "
                         "loop quick, which matters more here than depth: the "
                         "reflex layer handles anything urgent, and every "
                         "action is short and re-observed. Default low")
    ap.add_argument("--logs", default="runs", help="where to write run logs")
    ap.add_argument("--min-turn-s", type=float, default=0.0,
                    help="floor on how fast turns may run, seconds")
    args = ap.parse_args()

    if anthropic is None:
        print("the anthropic SDK is missing. Install it with:")
        print("    pip install anthropic")
        return 2

    client = anthropic.Anthropic()
    tools = tool_definitions()
    log = RunLog(args.logs, args.task, args.model)

    cfg = CarConfig.from_env(**({"host": args.host} if args.host else {}))
    # Where the head sits at the start of the run, and what `scan` surveys
    # with. Overridable because the tilt-to-aim mapping moves whenever the
    # servo horn slips, and nothing on the robot can measure it.
    survey_tilt = args.tilt if args.tilt is not None else TILT_SCAN
    print(f"connecting to {cfg.host} ...")

    try:
        safety_cfg = SafetyConfig()
        if args.stop_cm is not None:
            safety_cfg.stop_cm = args.stop_cm
        with Car(cfg) as car, SafetyLoop(car, safety_cfg) as guard:
            pilot = Pilot(car, guard, log, survey_tilt=survey_tilt)
            # Aim the head before the opening frame, not after. The first
            # picture of a run is the one the whole plan is built on.
            car.tilt(survey_tilt)
            time.sleep(0.5)
            car.home()          # straight ahead and level, every run

            # Resolution is a straight multiplier on the cost of every turn.
            code, size, tokens = FRAMESIZES[args.framesize]
            try:
                car.camera("framesize", code)
                time.sleep(0.4)
                pilot.frame()   # discard one: the sensor restarts on a resize
            except CarError as exc:
                print(f"could not set the camera resolution: {exc}")
            print(f"camera: {args.framesize} ({size}), about {tokens} tokens "
                  f"a frame, keeping {args.keep_frames} in the conversation")

            voice = Voice(enabled=not args.no_voice, rate=args.voice_rate)
            print(voice.describe)
            voice.say(f"Starting. {args.task}.")

            print(f"task: {args.task}")
            print(f"model: {args.model}, effort {args.effort}")
            print(f"{guard.describe()}\n")

            first_frame = pilot.frame()
            opening: List[Dict[str, Any]] = [
                {"type": "text",
                 "text": f"Task: {args.task}\n\n{pilot.sensors()}\n\n"
                         f"This is what you can see now."}]
            if first_frame:
                opening.append(image_block(first_frame))
                log.frame(0, first_frame, "opening")

            messages: List[Dict[str, Any]] = [
                {"role": "user", "content": opening}]
            totals = {"input": 0, "output": 0, "cache_read": 0}

            for turn in range(1, args.turns + 1):
                started = time.time()
                response = client.messages.create(
                    model=args.model,
                    max_tokens=8000,
                    system=SYSTEM,
                    tools=tools,
                    # Auto caching: the system prompt and tool list never
                    # change, and the history only grows at the end, so the
                    # prefix is reusable every turn.
                    cache_control={"type": "ephemeral"},
                    thinking={"type": "adaptive"},
                    output_config={"effort": args.effort},
                    messages=messages,
                )
                latency = time.time() - started
                totals["input"] += response.usage.input_tokens
                totals["output"] += response.usage.output_tokens
                totals["cache_read"] += getattr(
                    response.usage, "cache_read_input_tokens", 0) or 0

                said = " ".join(b.text.strip() for b in response.content
                                if b.type == "text").strip()
                if said:
                    print(f"[{turn}] {said}")
                    # The narration is already written for a person to follow,
                    # which is exactly what makes it worth saying out loud.
                    # Fire and forget: `say` returns at once and drops anything
                    # the robot has already moved past.
                    voice.say(said)

                calls = [b for b in response.content if b.type == "tool_use"]
                if not calls:
                    print(f"[{turn}] no tool call, stopping "
                          f"({response.stop_reason})")
                    log.turn({"turn": turn, "said": said, "tool": None,
                              "stop_reason": response.stop_reason,
                              "latency_s": round(latency, 2)})
                    break

                messages.append({"role": "assistant",
                                 "content": response.content})

                results = []
                for call in calls:
                    print(f"      -> {call.name}({json.dumps(call.input)})")
                    outcome = pilot.execute(call.name, call.input)
                    print(f"         {outcome.text.splitlines()[0]}")

                    content, frame_name = tool_result_content(
                        outcome, pilot.sensors(), log, turn)

                    results.append({"type": "tool_result",
                                    "tool_use_id": call.id,
                                    "content": content,
                                    **({"is_error": True}
                                       if outcome.is_error else {})})

                    log.turn({
                        "turn": turn, "said": said,
                        "tool": call.name, "input": call.input,
                        "result": outcome.text, "frame": frame_name,
                        "is_error": outcome.is_error,
                        "sensors": pilot.sensors(),
                        "latency_s": round(latency, 2),
                        "usage": {
                            "input": response.usage.input_tokens,
                            "output": response.usage.output_tokens,
                            "cache_read": getattr(
                                response.usage, "cache_read_input_tokens", 0),
                        },
                    })

                messages.append({"role": "user", "content": results})
                prune_images(messages, args.keep_frames)

                if pilot.finished:
                    break

                spare = args.min_turn_s - (time.time() - started)
                if spare > 0:
                    time.sleep(spare)

            if pilot.finished:
                voice.say(pilot.finished.get("summary") or "Finished.")
            voice.close(wait=6.0)

            print()
            if totals["input"]:
                print(f"tokens: {totals['input']:,} in "
                      f"({totals['cache_read']:,} of them cached), "
                      f"{totals['output']:,} out")
            if pilot.finished:
                print(f"the robot reports: {pilot.finished['outcome']}")
                print(f"  {pilot.finished['summary']}")
            else:
                print(f"stopped after {args.turns} turns without a report")

            vetoes = guard.vetoes
            if vetoes:
                print(f"\nthe safety layer intervened {len(vetoes)} time(s):")
                for _, why in vetoes:
                    print(f"  {why}")
            print(f"\nrun logged to {log.dir}")
        return 0

    except KeyboardInterrupt:
        print("\nstopped by hand")
        return 1
    except anthropic.APIStatusError as exc:
        print(f"\nthe model API returned {exc.status_code}: {exc.message}")
        return 2
    except anthropic.APIConnectionError:
        print("\ncould not reach the model API. Is this machine online?")
        return 2
    except (OSError, CarError) as exc:
        print(f"\nfailed: {type(exc).__name__}: {exc}")
        return 2
    finally:
        # Let a finished sentence finish, but never leave a voice talking
        # about a robot that has stopped.
        voice.close(wait=6.0 if voice.spoken else 0.5)


if __name__ == "__main__":
    sys.exit(main())
