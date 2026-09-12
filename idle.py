#!/usr/bin/env python3
"""
idle.py - the robot glances around while it is thinking.

A model turn takes two to four seconds, and for all of it the robot sits
perfectly still with its head locked forward. That reads as broken rather than
as thinking. This moves the head a little during that window, so there is
something alive to watch.

    with IdleHead(car):
        response = client.messages.create(...)

THE RULE THIS MUST NOT BREAK

**The model's view of the world cannot change.** The head's pan angle is how
the model works out which way the car is facing: it finds a target, reads the
pan angle it was centred at, and turns the body by that offset. A head that
wandered would silently corrupt that, and the failure would look like the model
being bad at aiming rather than like a feature nobody asked to interfere.

So this is bounded by construction:

- It only runs while the model is thinking, which is the one window where
  nothing is being observed or acted on.
- It **restores the exact commanded aim** before returning, and the next frame
  is captured after that. `Car.aim` reports the last commanded position, so the
  restore is to a known number rather than to an assumption.
- It is purely cosmetic. It never moves the wheels and it never touches tilt
  beyond a couple of degrees.

WHY IT IS CHEAP

It costs no model turns and no tokens, which is the whole appeal: liveliness
without spending the budget the robot needs for actually driving. A run that
achieves the same thing by asking the model to call `look` more often pays a
turn and about 100 image tokens every time, and that turn is one it did not
spend moving.

On the serial link it is two or three servo commands spread over a couple of
seconds, about 30 bytes each, against a 960 byte per second budget that the
safety poll already uses 55% of. Negligible, but it is shared, so the glance
sleeps between steps rather than streaming.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Optional

# How far the head may wander, in servo degrees. Small on purpose: this is a
# glance, not a scan, and a big sweep would be both slower to undo and more
# likely to still be moving when the restore lands.
PAN_SWING = 12
TILT_SWING = 4

# Leave the head still for a moment first, so a fast turn does not twitch.
SETTLE_S = 0.45
# Between steps of a glance. Also the resolution at which a finished model call
# can interrupt one.
STEP_S = 0.35


class IdleHead:
    """Moves the head while the model thinks, and puts it back.

    Use as a context manager around the model call. Exiting restores the aim
    and waits for the servo to arrive, so the caller can capture immediately
    afterwards without seeing a half-finished movement.

    Every failure here is swallowed. A cosmetic flourish must never be able to
    end a run: the link is shared with the safety poll and the command socket,
    and a servo command that times out under load is a normal event on this car,
    not a reason to stop driving.

    **The one case this cannot fully protect**: if the link fails during a
    glance, the restore fails too, and the head is left up to PAN_SWING degrees
    off while `Car.aim` still reports where it was commanded. `Car.pan` only
    records a position after the command succeeds, so nothing lies about it;
    the two just disagree until the next `look`, `scan` or `home` sets both.
    The exposure is twelve degrees and a link that has already failed, which is
    a run about to end anyway, so it is accepted rather than solved.
    """

    def __init__(self, car, enabled: bool = True,
                 rng: Optional[random.Random] = None):
        self.car = car
        self.enabled = enabled
        self.rng = rng or random.Random()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._home: Optional[tuple] = None
        self.moved = False          # for tests, and for anyone wondering

    def __enter__(self) -> "IdleHead":
        if not self.enabled:
            return self
        pan, tilt = self.car.aim
        if pan is None or tilt is None:
            # Nothing has commanded the head this session, so there is no known
            # position to return to. Doing nothing is correct: guessing would
            # leave the camera somewhere the model did not ask for.
            return self
        self._home = (pan, tilt)
        self._stop.clear()
        self._thread = threading.Thread(target=self._glance, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.restore()
        return False                # never swallow the caller's exception

    def _glance(self) -> None:
        if self._stop.wait(SETTLE_S) or self._home is None:
            return
        pan, tilt = self._home
        # One way, then a smaller drift back past centre, which reads more like
        # looking at something than like a servo test.
        first = self.rng.choice((-1, 1)) * self.rng.randint(PAN_SWING // 2,
                                                            PAN_SWING)
        steps = ((pan + first, tilt + self.rng.randint(-TILT_SWING,
                                                       TILT_SWING)),
                 (pan - first // 2, tilt))
        for target_pan, target_tilt in steps:
            if self._stop.is_set():
                return
            try:
                self.car.pan(target_pan)
                self.car.tilt(target_tilt)
                self.moved = True
            except Exception:
                return              # see the class docstring: never fatal
            if self._stop.wait(STEP_S):
                return

    def restore(self) -> None:
        """Put the head back exactly where the model last commanded it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._home is None or not self.moved:
            return
        pan, tilt = self._home
        try:
            self.car.pan(pan)
            self.car.tilt(tilt)
            # Let the servo actually arrive. Without this the next capture can
            # catch the head mid-sweep, which would hand the model a frame that
            # does not match the pan angle it is told about.
            time.sleep(0.35)
        except Exception:
            pass
        finally:
            self.moved = False
