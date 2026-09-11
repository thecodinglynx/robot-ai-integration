#!/usr/bin/env python3
"""
ears.py - let a person talk to the robot.

Push to talk on the laptop: press Enter, speak, press Enter again. What you said
is transcribed locally with faster-whisper and handed to the agent, which reads
it on its next turn as "The person just said: ...".

    pip install sounddevice faster-whisper
    python ears.py                 # a quick test: speak once, see the transcript
    python agent.py --host 192.168.1.211 --listen

WHY THE MICROPHONE IS ON THE LAPTOP AND NOT THE ROBOT

Three reasons, any one of which would settle it:

  * A Bluetooth speaker's microphone only works through the phone-call
    profile, and while that is active Windows drops the OUTPUT to phone-call
    quality too. The robot's voice would sound like a bad line whenever it
    could hear you.
  * A microphone on the robot sits beside four motors and a gearbox, while you
    are across the room. It would mostly hear the robot.
  * The robot's own voice would come out of a speaker a few centimetres away
    and be transcribed as an instruction.

So the voice goes out through the speaker on the robot and instructions come in
through a microphone near you, on separate paths.

WHY "STOP" NEVER GOES TO THE MODEL

Every other utterance waits for the model's next turn, which is a couple of
seconds away. A spoken stop does not: it halts the car the moment it is
transcribed, through the safety layer, and then the robot HOLDS, refusing to
drive or turn until you say something else. The model is told afterwards. A
stop that had to wait for the model to read it and agree would not be one.

It is still not an emergency stop. Transcription takes about a second after you
finish speaking, on top of however long you held the key. The car's power
switch, and Ctrl+C in the agent's terminal, remain the real ones.

WHY PUSH TO TALK

Always-on listening needs voice activity detection, a wake word, and some way
of not hearing the robot's own voice. Push to talk needs a key, is unambiguous
about when you are addressing the robot, and while it is held the robot's voice
is muted so it cannot be picked up.
"""

from __future__ import annotations

import queue
import re
import sys
import threading
import time
from typing import Callable, List, Optional

__all__ = ["Ears", "ListenUnavailable", "is_stop", "STOP_WORDS"]

SAMPLE_RATE = 16000              # what whisper wants; no resampling needed
MAX_RECORD_S = 15.0              # a held key should not record for ever
MIN_RECORD_S = 0.3               # shorter than this is a stray keypress

# Any of these anywhere in an utterance halts the car. Deliberately broad, and
# deliberately matched as words rather than phrases, because the two ways of
# getting it wrong are not symmetric: a false halt costs a sentence to restart,
# a missed one costs whatever the car hits. "Don't stop" halts. That is fine.
STOP_WORDS = frozenset({"stop", "halt", "freeze"})

# Model size trades accuracy for speed. base.en transcribes a short sentence in
# about a second on a laptop CPU and is a ~150 MB download on first use.
# tiny.en is faster and noticeably worse; small.en is better and slower.
DEFAULT_MODEL = "base.en"


class ListenUnavailable(RuntimeError):
    """The microphone or the transcriber could not be set up."""


def is_stop(text: str) -> bool:
    """Whether an utterance asks the robot to stop.

    Whisper punctuates and capitalises ("Stop!", "Stop, stop."), so compare
    bare lowercase words.
    """
    words = re.findall(r"[a-z']+", text.lower())
    return any(w in STOP_WORDS for w in words)


class Ears:
    """Push to talk, transcription, and the stop that bypasses the model.

    Everything runs on background threads, so the agent loop never waits on a
    microphone. It collects what was said with `drain()`, or blocks for the next
    instruction with `wait()` when it has nothing else to do.

    `hold` is set by a spoken stop and cleared by the next thing said that is
    not a stop. While it is set, the agent refuses to drive or turn.
    """

    def __init__(self, on_stop: Optional[Callable[[str], None]] = None,
                 on_recording: Optional[Callable[[bool], None]] = None,
                 model: str = DEFAULT_MODEL, on_note=print):
        try:
            import numpy
            import sounddevice
        except ImportError as exc:
            raise ListenUnavailable(
                f"{exc.name} is not installed. "
                f"pip install sounddevice faster-whisper") from None
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise ListenUnavailable(
                "faster-whisper is not installed. "
                "pip install sounddevice faster-whisper") from None

        self._np = numpy
        self._sd = sounddevice
        self._note = on_note
        self.on_stop = on_stop
        self.on_recording = on_recording

        on_note(f"loading speech model {model} (downloads once, ~150 MB)...")
        started = time.time()
        # int8 on the CPU: fast enough for a sentence, and no GPU assumed.
        self._model = WhisperModel(model, device="cpu", compute_type="int8")
        on_note(f"speech model ready in {time.time() - started:.1f} s")

        self.heard: "queue.Queue[str]" = queue.Queue()
        self.hold = threading.Event()
        self._recording = threading.Event()
        self._chunks: List = []
        self._stream = None
        self._started_at = 0.0
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._key_thread: Optional[threading.Thread] = None

    # ---------- lifecycle ----------

    def start(self) -> "Ears":
        """Start watching the keyboard. Enter toggles recording."""
        self._running.set()
        self._key_thread = threading.Thread(target=self._keys, daemon=True,
                                            name="push-to-talk")
        self._key_thread.start()
        self._note("push to talk: press Enter, speak, press Enter again")
        return self

    def close(self) -> None:
        self._running.clear()
        with self._lock:
            self._close_stream()

    def __enter__(self) -> "Ears":
        return self.start()

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    # ---------- what the agent calls ----------

    def drain(self) -> List[str]:
        """Everything said since the last call, oldest first. Never blocks."""
        out = []
        while True:
            try:
                out.append(self.heard.get_nowait())
            except queue.Empty:
                return out

    def wait(self, timeout: Optional[float] = None) -> Optional[str]:
        """Block until the person says something. None on timeout."""
        try:
            return self.heard.get(timeout=timeout)
        except queue.Empty:
            return None

    # ---------- push to talk ----------

    def _keys(self) -> None:
        # A plain blocking read on stdin, on its own thread. No keyboard hook,
        # no admin rights, and it works in cmd, PowerShell and WSL alike.
        while self._running.is_set():
            try:
                sys.stdin.readline()
            except (EOFError, OSError, ValueError):
                return
            if not self._running.is_set():
                return
            if self._recording.is_set():
                self._finish()
            else:
                self._begin()

    def _begin(self) -> None:
        with self._lock:
            self._chunks = []
            try:
                self._stream = self._sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                    callback=self._on_audio)
                self._stream.start()
            except Exception as exc:
                self._stream = None
                self._note(f"could not open the microphone: {exc}")
                return
            self._started_at = time.time()
            self._recording.set()
        if self.on_recording:
            self.on_recording(True)
        self._note("  (listening... press Enter when you have finished)")
        threading.Thread(target=self._timeout_guard, daemon=True).start()

    def _timeout_guard(self) -> None:
        started = self._started_at
        while self._recording.is_set() and self._started_at == started:
            if time.time() - started > MAX_RECORD_S:
                self._note("  (that is as long as one instruction can be)")
                self._finish()
                return
            time.sleep(0.2)

    def _on_audio(self, indata, frames, when, status) -> None:
        # Called on the audio driver's thread. Copy, because the driver reuses
        # the buffer as soon as this returns.
        self._chunks.append(indata.copy())

    def _finish(self) -> None:
        with self._lock:
            if not self._recording.is_set():
                return
            self._recording.clear()
            duration = time.time() - self._started_at
            self._close_stream()
            chunks, self._chunks = self._chunks, []
        if self.on_recording:
            self.on_recording(False)
        if duration < MIN_RECORD_S or not chunks:
            return
        audio = self._np.concatenate(chunks).flatten()
        # Transcribe off this thread, so the keyboard stays responsive.
        threading.Thread(target=self._transcribe, args=(audio,),
                         daemon=True, name="transcribe").start()

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # ---------- transcription ----------

    def _transcribe(self, audio) -> None:
        try:
            # The VAD filter matters more than it looks. On silence, whisper is
            # prone to hallucinating a polite "Thank you." out of nothing, which
            # would reach the model as an instruction.
            segments, _ = self._model.transcribe(
                audio, language="en", beam_size=1, vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            self._note(f"  (could not transcribe that: {exc})")
            return
        if not text:
            self._note("  (heard nothing)")
            return
        self.handle(text)

    def handle(self, text: str) -> None:
        """Act on a transcript. Public so a test can feed it words directly."""
        if is_stop(text):
            # Halt first, then tell anyone. The order is the whole point.
            self.hold.set()
            if self.on_stop:
                try:
                    self.on_stop(text)
                except Exception as exc:
                    self._note(f"  (the stop raised: {exc})")
            self._note(f'  heard: "{text}"  -> HALTED, holding until you '
                       f"speak again")
        else:
            self.hold.clear()
            self._note(f'  heard: "{text}"')
        self.heard.put(text)


if __name__ == "__main__":
    # The microphone and the transcriber, on their own, before the robot is
    # involved at all: if this cannot hear you, nothing downstream will.
    try:
        ears = Ears(on_stop=lambda t: print("  [the car would halt here]"))
    except ListenUnavailable as exc:
        print(exc)
        raise SystemExit(1)
    with ears:
        print("Say something. Ctrl+C to quit.")
        try:
            while True:
                said = ears.wait()
                if said is not None:
                    print(f"  stop word: {is_stop(said)}, hold: "
                          f"{ears.hold.is_set()}")
        except KeyboardInterrupt:
            print()
