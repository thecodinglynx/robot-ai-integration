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
           "best_sink", "shorten", "speakable"]

# One sentence, or about this many characters, whichever comes first. Was 180,
# which still ran on past the action it described: an utterance has to finish
# inside the couple of seconds a turn takes, or the robot is somewhere else by
# the time the sentence ends and the commentary is worse than useless.
MAX_SPOKEN_CHARS = 110

# Words per minute. Deliberately above the ~200 that both engines default to.
# The robot moves while it talks, so the words have to keep up; this is about
# a newsreader's clip, still clear but not leisurely. --voice-rate overrides.
DEFAULT_RATE_WPM = 275


# Symbols the model writes and a synthesiser cannot say. Two problems at once:
# a voice reads "14 deg left" better than "14° left" and says nothing at all
# for "≈", and on Windows the default cp1252 pipe encoding cannot carry them,
# which crashed a run mid-drive.
SPOKEN_SUBSTITUTIONS = {
    "°": " degrees",      # °
    "≈": " about ",       # ≈
    "±": " plus or minus ",
    "×": " by ",
    "→": " to ",          # →
    "–": ", ",            # en dash
    "—": ", ",            # em dash
    "…": "...",
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
}


def speakable(text: str) -> str:
    """Text a synthesiser can pronounce and a pipe can carry.

    Both halves matter. The model narrates in symbols, "14° left ≈ 80 ms",
    which reads well on screen and badly aloud. And on Windows the pipe to the
    speech engine is cp1252 by default, so a single "≈" raised a
    UnicodeEncodeError on a thread inside `subprocess` that this module cannot
    catch, printing a traceback in the middle of a run.

    Substitute what has a spoken form, drop what does not.
    """
    for symbol, spoken in SPOKEN_SUBSTITUTIONS.items():
        text = text.replace(symbol, spoken)
    text = text.encode("ascii", "ignore").decode("ascii")
    return " ".join(text.split())


def shorten(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """Trim narration to something that can be said in the time available.

    Sentence boundaries where possible, because a clause cut mid-phrase sounds
    like a fault rather than a summary.
    """
    text = speakable(text)
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)

    # Keep as much as fits from the front, which is usually what was seen.
    out = ""
    for sentence in sentences:
        if out and len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()

    # But if that dropped the ending, prefer the ending. Narration is almost
    # always "what I can see, then what I am about to do", and the second half
    # is the half worth hearing: whoever is listening can see the room for
    # themselves, and cannot see what the robot has decided. The first version
    # kept the front and cut the intent, so a long line was spoken as "I can
    # see a person lying on the floor" with the "turning to face them" lost.
    if out and len(out) <= limit and out.strip() != text.strip():
        tail = sentences[-1].strip()
        if tail and tail not in out and len(tail) <= limit:
            return tail

    if out and len(out) <= limit:
        return out
    # A single sentence longer than the limit falls through everything above,
    # so cut it on a word boundary: a word chopped in half sounds like a fault.
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

    # The voice name comes in through the environment rather than being pasted
    # into the script, so a name with a quote in it cannot break or extend the
    # command. SelectVoice throws on an unknown name, so it is matched loosely
    # against the installed list and skipped if nothing matches: a wrong
    # --voice should mean the default voice, not a silent run.
    PREAMBLE = (
        "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new(); "
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = [int]$env:VOICE_RATE; "
        "if ($env:VOICE_NAME) { "
        "  $m = $s.GetInstalledVoices() | "
        "       ? { $_.VoiceInfo.Name -like \"*$env:VOICE_NAME*\" } | "
        "       select -First 1; "
        "  if ($m) { $s.SelectVoice($m.VoiceInfo.Name) } } "
    )

    # Out of the laptop's speakers.
    SCRIPT = PREAMBLE + "$s.Speak([Console]::In.ReadToEnd())"

    # Into a WAV file instead, in the one format the robot's /say accepts:
    # 16 kHz, 16 bit, mono. Dispose is what finalises the header, so it must
    # run before the file is read.
    RENDER = PREAMBLE + (
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
        "[System.Speech.AudioFormat.AudioChannel]::Mono); "
        "$s.SetOutputToWaveFile($env:VOICE_OUT, $f); "
        "$s.Speak([Console]::In.ReadToEnd()); "
        "$s.Dispose()"
    )

    LIST = (
        "Add-Type -AssemblyName System.Speech; "
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
        ".GetInstalledVoices() | "
        "% { $_.VoiceInfo.Name + '  (' + $_.VoiceInfo.Culture + ', ' "
        "    + $_.VoiceInfo.Gender + ')' }"
    )

    def __init__(self, rate: Optional[int] = None,
                 voice: Optional[str] = None):
        self.exe = shutil.which("powershell") or shutil.which("pwsh")
        if not self.exe:
            raise RuntimeError("no powershell on PATH")
        # System.Speech rates run -10 to 10, unlike pyttsx3's words per minute.
        self.rate = 0 if rate is None else max(-10, min(10, (rate - 200) // 20))
        self.voice = voice or ""

    def installed(self):
        out = subprocess.run([self.exe, "-NoProfile", "-NonInteractive",
                              "-Command", self.LIST],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
        return [l.strip() for l in out.stdout.splitlines() if l.strip()]

    def speak(self, text: str) -> None:
        import os
        env = dict(os.environ, VOICE_RATE=str(self.rate),
                   VOICE_NAME=self.voice)
        # utf-8 with errors="replace" rather than the console default, which
        # is cp1252 on Windows and cannot carry a degree sign. `shorten` has
        # already stripped anything unpronounceable; this is the second line
        # of defence, because the failure happens on a thread inside
        # subprocess where it cannot be caught.
        subprocess.run([self.exe, "-NoProfile", "-NonInteractive",
                        "-Command", self.SCRIPT],
                       input=text, text=True, env=env,
                       encoding="utf-8", errors="replace",
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)

    def render_wav(self, text: str) -> bytes:
        """The same voice, rendered to 16 kHz mono WAV bytes rather than played.

        Goes through a temporary file because System.Speech writes WAV headers
        properly only to a file it can seek in.
        """
        import os
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="robot_say_")
        os.close(fd)
        try:
            env = dict(os.environ, VOICE_RATE=str(self.rate),
                       VOICE_NAME=self.voice, VOICE_OUT=path)
            subprocess.run([self.exe, "-NoProfile", "-NonInteractive",
                            "-Command", self.RENDER],
                           input=text, text=True, env=env,
                           encoding="utf-8", errors="replace",
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60, check=True)
            with open(path, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


class RobotSink(Sink):
    """Speech out of the robot itself, through its own speaker.

    Synthesis stays here on the host, because the ESP32 has neither the room
    nor the quality for it. Each line is rendered to a 16 kHz mono WAV with the
    same Windows voice the laptop uses, and POSTed to `/say` on the camera
    module, which buffers it in PSRAM and clocks it out over I2S1 to a
    MAX98357A amplifier.

    `/say` answers as soon as the clip is queued, so this returns in well under
    a second and the worker is free for the next line. The robot plays each
    sentence to the end and keeps at most one waiting behind it, replacing a
    waiting one with anything newer: the same policy as the laptop path, in the
    same place it matters, next to the speaker.

    Wiring and pins are in the firmware, `elegoo_cam_station.ino`, under
    "audio". They are not the ones first proposed: GPIO 2 and 14 turned out not
    to be broken out on this board, and its 3.3 V regulator is too small to
    share with an amplifier.
    """

    name = "robot"

    def __init__(self, host: str, rate: Optional[int] = None,
                 voice: Optional[str] = None, volume: int = 70,
                 port: int = 80, timeout: float = 5.0):
        self.renderer = PowerShellSink(rate=rate, voice=voice)
        volume = max(0, min(100, int(volume)))
        self.url = f"http://{host}:{port}/say?vol={volume}"
        self.timeout = timeout

    def speak(self, text: str) -> None:
        import urllib.error
        import urllib.request
        wav = self.renderer.render_wav(text)
        request = urllib.request.Request(
            self.url, data=wav, method="POST",
            headers={"Content-Type": "audio/wav"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                r.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Say what to do, not just what went wrong: this is the one
                # error every first attempt will hit.
                raise RuntimeError(
                    "the camera firmware has no /say endpoint. Flash the "
                    "build with AUDIO_ENABLED 1") from None
            detail = exc.read().decode("utf-8", "replace").strip()
            raise RuntimeError(f"/say answered {exc.code}: {detail}") from None


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
        return PowerShellSink(rate=rate, voice=voice)
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
        rate = DEFAULT_RATE_WPM if rate is None else rate
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


def list_voices():
    """Print what the speech engine can actually reach.

    "Actually reach" is the important part. Voices installed through Settings
    or through Narrator's natural-voice list often register only under
    Speech_OneCore, and System.Speech reads the older Speech key, so a voice
    can be installed, work perfectly in Narrator, and be invisible here. If
    something you have just installed is missing from this list, that is why;
    see the note in the README.
    """
    sink = best_sink()
    if sink is None:
        print("no speech engine found")
        return
    print(f"engine: {sink.name}")
    names = []
    if hasattr(sink, "installed"):
        names = sink.installed()
    else:
        try:
            import pyttsx3
            e = pyttsx3.init()
            names = [f"{v.name}  ({v.id})" for v in e.getProperty("voices")]
            e.stop()
        except Exception as exc:
            print(f"could not list voices: {exc}")
    for n in names:
        print(f"  {n}")
    if not names:
        print("  (none reported)")


if __name__ == "__main__":
    # A quick listen, so the voice can be judged before it is wired in.
    if "--list" in sys.argv:
        list_voices()
        raise SystemExit(0)
    def take(flag):
        if flag in sys.argv:
            i = sys.argv.index(flag)
            value = sys.argv[i + 1]
            del sys.argv[i:i + 2]
            return value
        return None

    voice_name = take("--voice")
    # --robot HOST sends the lines to the robot's speaker instead of playing
    # them here. The bring-up test for the amplifier, before involving the
    # agent at all: if this does not produce sound, nothing else will.
    robot = take("--robot")
    volume = int(take("--vol") or 70)
    lines = sys.argv[1:] or [
        "Scanning the room.",
        "A white ball by the couch, about thirty degrees to the right. "
        "Turning to face it.",
        "Close now. Stopping here.",
    ]
    sink = (RobotSink(robot, rate=DEFAULT_RATE_WPM, voice=voice_name,
                      volume=volume) if robot else None)
    with Voice(sink=sink, voice=voice_name) as v:
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
