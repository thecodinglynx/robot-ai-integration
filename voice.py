#!/usr/bin/env python3
"""
voice.py - speak the model's narration out loud.

The agent already says what it is doing before every action, because that
narration is what makes a run legible: "Turning right to face it" followed by a
left turn diagnosed a mirrored motor mapping in one line. This module puts that
same text through a speech synthesiser, so the robot can be followed by ear
instead of by watching a terminal.

    from voice import Voice

    voice = Voice()
    voice.say("A red box on the left. Turning towards it.")
    ...
    voice.close()

WHY IT IS BUILT THE WAY IT IS

**Speaking must never delay driving.** Synthesis takes a second or two and the
control loop is the thing holding a moving robot's stop. So `say` hands the
text to a worker thread and returns immediately, and the queue is deliberately
tiny: if the robot gets ahead of the speech, the OLDEST pending utterance is
dropped rather than played late. Narration that describes a decision two turns
ago is worse than silence, because it is actively misleading about where the
robot is now.

**The sink is swappable on purpose.** Today the laptop speaks. The intended
end state is the robot speaking, through a MAX98357A amplifier on the ESP32's
I2S pins, with the host doing the synthesis and posting audio to the camera
module. That is a different `Sink` and nothing else changes: `agent.py` never
learns where the sound comes out.

**It degrades to silence.** No speech engine, a broken one, a synthesiser that
throws halfway through: all of it is a warning on stderr once, and then the run
carries on. Nothing about the audio is allowed to stop the robot.
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import sys
import threading
from typing import Optional

__all__ = ["Voice", "Sink", "Pyttsx3Sink", "PowerShellSink", "RobotSink",
           "best_sink"]

# Two sentences, or about this many characters, whichever comes first. The
# model's narration can run long, and a turn takes a couple of seconds: speech
# that outlasts the action it describes falls behind and stays behind.
MAX_SPOKEN_CHARS = 180


def shorten(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """Trim narration to something that can be said in the time available.

    Sentence boundaries where possible, because a clause cut mid-phrase sounds
    like a fault rather than a summary.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for sentence in sentences:
        if out and len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()
    # A single sentence longer than the limit falls straight through the loop
    # above, so the length has to be checked again rather than assumed. Cut on
    # a word boundary in that case: a word chopped in half sounds like a fault.
    if out and len(out) <= limit:
        return out
    return text[:limit].rsplit(" ", 1)[0] + "..."


class Sink:
    """Somewhere speech comes out. Blocking; called only on the worker."""

    name = "none"

    def speak(self, text: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class Pyttsx3Sink(Sink):
    """The laptop's own synthesiser, via pyttsx3.

    A fresh engine per utterance, which is slower than reusing one and far more
    reliable: pyttsx3's `runAndWait` is known to hang on a second call against
    a reused SAPI engine, and a hung speech thread on a driving robot is not a
    trade worth making for a few hundred milliseconds.
    """

    name = "pyttsx3"

    def __init__(self, rate: Optional[int] = None, voice: Optional[str] = None):
        import pyttsx3            # raises if not installed; caller handles it
        self._pyttsx3 = pyttsx3
        self.rate = rate
        self.voice = voice
        self._pyttsx3.init().stop()      # fail loudly here rather than mid-run

    def speak(self, text: str) -> None:
        engine = self._pyttsx3.init()
        try:
            if self.rate is not None:
                engine.setProperty("rate", self.rate)
            if self.voice:
                for v in engine.getProperty("voices"):
                    if self.voice.lower() in (v.name or "").lower():
                        engine.setProperty("voice", v.id)
                        break
            engine.say(text)
            engine.runAndWait()
        finally:
            try:
                engine.stop()
            except Exception:
                pass


class PowerShellSink(Sink):
    """Windows' built-in synthesiser, with nothing to install.

    Slower to start than pyttsx3, about half a second of PowerShell, but it is
    always present on Windows and it cannot be broken by a Python package.
    The text is passed on stdin rather than interpolated into the command, so
    quotes and apostrophes in the narration cannot break the script or inject
    anything into it.
    """

    name = "powershell"

    SCRIPT = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = $env:VOICE_RATE; "
        "$s.Speak([Console]::In.ReadToEnd())"
    )

    def __init__(self, rate: Optional[int] = None):
        self.exe = shutil.which("powershell") or shutil.which("pwsh")
        if not self.exe:
            raise RuntimeError("no powershell on PATH")
        # System.Speech rates run -10 to 10, unlike pyttsx3's words per minute.
        self.rate = 0 if rate is None else max(-10, min(10, (rate - 200) // 20))

    def speak(self, text: str) -> None:
        import os
        env = dict(os.environ, VOICE_RATE=str(self.rate))
        subprocess.run([self.exe, "-NoProfile", "-NonInteractive",
                        "-Command", self.SCRIPT],
                       input=text, text=True, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)


class RobotSink(Sink):
    """Speech out of the robot itself. Not built yet.

    The plan, so the shape of it is on record: synthesis stays here on the
    host, because the ESP32 has neither the room nor the quality for it. The
    host renders 16 kHz mono PCM and POSTs it to a `/say` endpoint on the
    camera module, which buffers it in PSRAM and clocks it out over I2S to a
    MAX98357A amplifier driving the speaker.

    The pins are the awkward part and they are already worked out: GPIO 14 for
    BCLK, 13 for LRC and 2 for DIN. Almost everything else is taken by the
    camera, the UNO link and the debug UART, and 16 and 17 are the WROVER's
    PSRAM despite every pinout guide listing them as free.
    """

    name = "robot"

    def __init__(self, car):
        raise NotImplementedError(
            "speech on the robot needs the MAX98357A fitted and a /say "
            "endpoint in the camera firmware. Until then the laptop speaks.")


def best_sink(rate: Optional[int] = None,
              voice: Optional[str] = None) -> Optional[Sink]:
    """The best speech route available, or None if there is not one.

    pyttsx3 first because it starts faster, PowerShell second because it needs
    nothing installed. Failure here is not an error: the run continues in
    silence.
    """
    try:
        return Pyttsx3Sink(rate=rate, voice=voice)
    except Exception:
        pass
    try:
        return PowerShellSink(rate=rate)
    except Exception:
        return None


class Voice:
    """Speak text without ever making the caller wait.

    `say` returns immediately. One worker thread does the synthesis, and the
    queue holds `depth` utterances; past that the oldest is dropped, so what
    you hear stays in step with what the robot is doing.
    """

    def __init__(self, sink: Optional[Sink] = None, enabled: bool = True,
                 rate: Optional[int] = None, voice: Optional[str] = None,
                 depth: int = 2, on_note=print):
        self.enabled = enabled
        self.sink = sink if sink is not None else (
            best_sink(rate=rate, voice=voice) if enabled else None)
        self.dropped = 0
        self.spoken = 0
        self._note = on_note
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=depth)
        self._thread: Optional[threading.Thread] = None
        self._warned = False

        if self.enabled and self.sink is None:
            self._note("no speech engine found, running silently. "
                       "`pip install pyttsx3` for one.")
            self.enabled = False
        if self.enabled:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="voice")
            self._thread.start()

    @property
    def describe(self) -> str:
        if not self.enabled or self.sink is None:
            return "voice: off"
        return f"voice: on, via {self.sink.name}"

    def say(self, text: str) -> None:
        """Queue something to be said. Never blocks, never raises."""
        if not self.enabled or not text:
            return
        text = shorten(text)
        if not text:
            return
        while True:
            try:
                self._queue.put_nowait(text)
                return
            except queue.Full:
                # Drop the oldest rather than the newest: what the robot is
                # doing now matters more than what it was doing two turns ago,
                # and narration that lags is worse than none.
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    return

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            try:
                self.sink.speak(text)
                self.spoken += 1
            except Exception as exc:
                # One warning, then silence. Nothing about the audio is
                # allowed to interfere with a robot that is moving.
                if not self._warned:
                    self._warned = True
                    self._note(f"speech failed, continuing silently: "
                               f"{type(exc).__name__}: {exc}")

    def close(self, wait: float = 2.0) -> None:
        """Stop speaking and let the worker finish."""
        if not self.enabled or self._thread is None:
            return
        self.enabled = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=wait)
        try:
            self.sink.close()
        except Exception:
            pass

    def __enter__(self) -> "Voice":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


if __name__ == "__main__":
    # A quick listen, so the voice can be judged before it is wired in.
    lines = sys.argv[1:] or [
        "Scanning the room.",
        "A white ball by the couch, about thirty degrees to the right. "
        "Turning to face it.",
        "Close now. Stopping here.",
    ]
    with Voice() as v:
        print(v.describe)
        for line in lines:
            print(f"  {shorten(line)}")
            v.say(line)
        import time
        time.sleep(1)
        while not v._queue.empty():
            time.sleep(0.2)
        time.sleep(3)
    print(f"spoken {v.spoken}, dropped {v.dropped}")
