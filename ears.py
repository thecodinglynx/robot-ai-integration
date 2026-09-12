#!/usr/bin/env python3
"""
ears.py - let a person talk to the robot.

Say "robot" and then what you want. The microphone stays open, speech is
segmented out of the room, transcribed locally with faster-whisper, and handed
to the agent, which reads it on its next turn as "The person just said: ...".

    pip install sounddevice faster-whisper
    python ears.py                          # speak, see what it heard
    python agent.py --host 192.168.1.211 --listen

Anything without the wake word in it is transcribed and thrown away. "Stop" is
the exception: it is always obeyed, because nobody shouting at a car heading
for the stairs says its name first.

`--wake none` goes back to push to talk, where Enter starts and stops each
recording. That is worth having when the room is noisy enough that the wake
word keeps being missed, or when you would rather the microphone were not open.

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

HOW ALWAYS-ON LISTENING AVOIDS HEARING ITSELF

The robot's voice comes out of a speaker in the same room as the microphone, so
without care it transcribes its own narration and obeys it, which is a loop
that feeds itself. Two things prevent that. The agent tells these ears when the
robot is talking, and audio is thrown away rather than segmented for as long as
it is; and whatever was part-heard when it started talking is discarded too,
rather than being stitched either side of the interruption.

The threshold that separates speech from silence is measured from the room
itself at startup, not fixed. A fan, a fridge or a laptop's own cooling move
the noise floor by more than a voice does, so a number that works in one room
is wrong in the next.
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

# ------------------------------------------------------------- wake word
#
# With a wake word the microphone is always open and there is no key to press.
# Everything spoken nearby is segmented and transcribed, and an utterance is
# only passed on if the wake word is in it; the command is whatever follows.
#
# Transcribing everything sounds wasteful and is not: segmenting first means
# whisper only runs when somebody actually speaks, which in a quiet room is
# rarely. It does mean anything said near the laptop gets transcribed locally
# and thrown away, so it is opt-in per run, not a default that creeps up on
# anyone.
DEFAULT_WAKE = "robot"

# What whisper tends to write when it hears the wake word. It has no idea the
# word matters, so it punctuates and mishears it like any other.
WAKE_ALIASES = {
    "robot": ("robot", "robots", "roboto", "robo", "robort"),
}

# A stop is always obeyed, wake word or not. Shouting "robot, stop" at a car
# heading for the stairs is not what anyone does.
# ------------------------------------------------------------------------

# Segmenting speech from silence, in blocks of this many samples. 512 at
# 16 kHz is 32 ms, short enough to catch the start of a word and long enough
# that the arithmetic per block is nothing.
BLOCK = 512
BLOCKS_PER_S = SAMPLE_RATE / BLOCK
START_BLOCKS = 3                  # ~100 ms above the threshold starts it
END_BLOCKS = 25                   # ~800 ms below it ends it
PREROLL_BLOCKS = 12               # ~380 ms kept from before it started
CALIBRATE_S = 1.0                 # ambient noise, measured at startup
# How much of an utterance has to be ABOVE the threshold for it to be worth
# transcribing. Counting the whole segment instead lets a cough through: with
# the pre-roll in front and the silence that ends it behind, 130 ms of door
# arrives as a 1.3 second utterance. Kept under 200 ms so that "stop", which is
# a short word said in a hurry, always survives.
MIN_VOICED_S = 0.18
THRESHOLD_OVER_AMBIENT = 4.0      # how far above the room counts as speech
THRESHOLD_FLOOR = 0.004           # a silent room must not arm on nothing


class ListenUnavailable(RuntimeError):
    """The microphone or the transcriber could not be set up."""


def is_stop(text: str) -> bool:
    """Whether an utterance asks the robot to stop.

    Whisper punctuates and capitalises ("Stop!", "Stop, stop."), so compare
    bare lowercase words.
    """
    words = re.findall(r"[a-z']+", text.lower())
    return any(w in STOP_WORDS for w in words)


OPENERS = ("ok", "okay", "hey", "hi", "yo", "so", "right", "now")


def wake_command(text: str, wake: str) -> Optional[str]:
    """The instruction inside an utterance, or None if it was not for us.

    People address a robot at the front, "robot, go left", sometimes behind a
    filler, "OK robot, go left", and sometimes at the back, "go left, robot".
    All three work. Anywhere else does not, which is the point: "the robot is
    quite slow", said to somebody else in the room, is a remark and not an
    instruction, and matching the word wherever it fell turned it into one.
    """
    if not wake:
        return text.strip() or None
    words = re.findall(r"[a-z']+", text.lower())
    aliases = WAKE_ALIASES.get(wake.lower(), (wake.lower(),))
    if not words:
        return None

    # At the front, possibly after a filler word.
    for i in (0, 1):
        if i < len(words) and words[i] in aliases:
            if i == 1 and words[0] not in OPENERS:
                break
            rest = " ".join(words[i + 1:]).strip()
            return rest or None

    # Or tacked on the end.
    if len(words) > 1 and words[-1] in aliases:
        return " ".join(words[:-1]).strip() or None
    return None


class Segmenter:
    """Turns a stream of audio blocks into utterances.

    A plain energy gate: speech starts when the room gets louder than its own
    noise floor for a moment, and ends when it goes quiet again for most of a
    second. It keeps a little audio from before the start, because the first
    consonant of a word arrives before the level has risen enough to notice.

    Deliberately not a neural detector. This only has to decide when to bother
    whisper, and whisper has a proper voice detector of its own for what
    reaches it. A wrong call here costs a transcription, not an action.
    """

    def __init__(self, threshold: float):
        self.threshold = threshold
        self.preroll: List = []
        self.blocks: List = []
        self.loud = 0
        self.quiet = 0
        self.voiced = 0
        self.speaking = False

    def feed(self, block, level: float) -> Optional[List]:
        """One block in; a finished utterance out, when there is one."""
        if not self.speaking:
            self.preroll.append(block)
            if len(self.preroll) > PREROLL_BLOCKS:
                self.preroll.pop(0)
            self.loud = self.loud + 1 if level > self.threshold else 0
            if self.loud >= START_BLOCKS:
                self.speaking = True
                self.blocks = list(self.preroll)
                self.preroll = []
                self.quiet = 0
                self.voiced = START_BLOCKS
            return None

        self.blocks.append(block)
        if level > self.threshold:
            self.quiet = 0
            self.voiced += 1
        else:
            self.quiet += 1
        too_long = len(self.blocks) > MAX_RECORD_S * BLOCKS_PER_S
        if self.quiet >= END_BLOCKS or too_long:
            return self.finish()
        return None

    def finish(self) -> Optional[List]:
        """End the utterance now, whatever it is doing."""
        if not self.speaking:
            return None
        self.speaking = False
        blocks, self.blocks = self.blocks, []
        voiced, self.voiced = self.voiced, 0
        self.loud = self.quiet = 0
        # Judged on the speech in it, not its length. A door or a cough is
        # loud for a moment and silent either side of it.
        if voiced < MIN_VOICED_S * BLOCKS_PER_S:
            return None
        return blocks

    def discard(self) -> None:
        """Throw away whatever is part-heard, for instance when the robot
        starts talking and everything after it is the robot."""
        self.speaking = False
        self.blocks = []
        self.preroll = []
        self.loud = self.quiet = self.voiced = 0


def microphones(sounddevice=None):
    """Every input device, as (index, name) pairs."""
    if sounddevice is None:
        import sounddevice
    return [(i, d["name"]) for i, d in enumerate(sounddevice.query_devices())
            if d["max_input_channels"] > 0]


def find_mic(sounddevice, wanted: str, on_note=print) -> Optional[int]:
    """An input device index by loose name match, or None for the default."""
    for index, name in microphones(sounddevice):
        if wanted.lower() in name.lower():
            on_note(f"microphone: {name}")
            return index
    on_note(f'no microphone matching "{wanted}", using the default. '
            f"`python ears.py --devices` lists them")
    return None


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
                 model: str = DEFAULT_MODEL, mic: Optional[str] = None,
                 wake: Optional[str] = None,
                 busy: Optional[Callable[[], bool]] = None,
                 on_note=print):
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
        # Which microphone, matched loosely against the device name.
        #
        # Worth pinning rather than taking the default. If the default input is
        # a Bluetooth speaker's own microphone, Windows has to switch that
        # device to the phone-call profile to open it, and the robot's voice
        # drops to phone quality for as long as it is open. Naming the laptop's
        # own microphone avoids the whole thing.
        self.mic = find_mic(sounddevice, mic, on_note) if mic else None
        self.on_stop = on_stop
        self.on_recording = on_recording

        on_note(f"loading speech model {model} (downloads once, ~150 MB)...")
        started = time.time()
        # int8 on the CPU: fast enough for a sentence, and no GPU assumed.
        self._model = WhisperModel(model, device="cpu", compute_type="int8")
        on_note(f"speech model ready in {time.time() - started:.1f} s")

        # With a wake word the microphone stays open and there is no key.
        # Without one, Enter starts and stops each recording.
        self.wake = (wake or "").strip().lower() or None
        # Asked before any audio is kept: true while the robot is talking, so
        # its own voice is never segmented and handed back as an instruction.
        self.busy = busy

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

    @property
    def prompt(self) -> str:
        """How to talk to it, in the current mode."""
        if self.wake:
            return f'say "{self.wake}" and then your instruction'
        return "press Enter, speak, press Enter again"

    def start(self) -> "Ears":
        """Begin listening, by whichever route was configured."""
        self._running.set()
        if self.wake:
            self._key_thread = threading.Thread(target=self._listen,
                                                daemon=True, name="listening")
        else:
            self._key_thread = threading.Thread(target=self._keys, daemon=True,
                                                name="push-to-talk")
        self._key_thread.start()
        self._note(self.prompt)
        return self

    def _listen(self) -> None:
        """Always-on: segment speech out of the room and transcribe it."""
        np = self._np
        try:
            stream = self._sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=BLOCK, device=self.mic)
            stream.start()
        except Exception as exc:
            self._note(f"could not open the microphone: {exc}")
            return

        with stream:
            # Measure the room before deciding what counts as speech. A fixed
            # threshold works in one room and not the next; a fan, a fridge or
            # a laptop fan moves the floor by more than a voice does.
            floor = 0.0
            samples = 0
            while self._running.is_set() and samples < CALIBRATE_S * SAMPLE_RATE:
                block, _ = stream.read(BLOCK)
                floor = max(floor, float(np.sqrt(np.mean(block ** 2))))
                samples += BLOCK
            threshold = max(THRESHOLD_FLOOR, floor * THRESHOLD_OVER_AMBIENT)
            self._note(f"  (room noise {floor:.4f}, speaking above "
                       f"{threshold:.4f})")

            segmenter = Segmenter(threshold)
            talking = False
            while self._running.is_set():
                try:
                    block, _ = stream.read(BLOCK)
                except Exception as exc:
                    self._note(f"  (microphone stopped: {exc})")
                    return
                # While the robot is speaking, hear nothing. Its voice would
                # otherwise be segmented, transcribed and handed back as an
                # instruction, which is a loop that feeds itself.
                if self.busy is not None and self.busy():
                    if not talking:
                        segmenter.discard()
                        talking = True
                    continue
                if talking:
                    talking = False
                    segmenter.discard()      # drop the tail of its last word
                utterance = segmenter.feed(block,
                                           float(np.sqrt(np.mean(block ** 2))))
                if utterance:
                    audio = np.concatenate(utterance).flatten()
                    threading.Thread(target=self._transcribe, args=(audio,),
                                     daemon=True, name="transcribe").start()

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
        """Wait for the person to say something. None if the timeout ran out.

        **Always pass a timeout and loop.** A wait with no timeout cannot be
        interrupted on Windows, so Ctrl+C does nothing until the next time
        somebody speaks, which is the opposite of what Ctrl+C is for.
        """
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
                    device=self.mic, callback=self._on_audio)
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
        if self.wake and not is_stop(text):
            # Not addressed to the robot: heard, transcribed, discarded. A
            # stop skips this check on purpose, because someone shouting at a
            # car heading for the stairs will not say its name first.
            command = wake_command(text, self.wake)
            if command is None:
                self._note(f'  (not for me: "{text}")')
                return
            text = command
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
    if "--devices" in sys.argv:
        try:
            for index, name in microphones():
                print(f"  {index:2d}  {name}")
        except ImportError:
            print("pip install sounddevice faster-whisper")
        raise SystemExit(0)

    mic = None
    if "--mic" in sys.argv:
        i = sys.argv.index("--mic")
        mic = sys.argv[i + 1]
    wake = DEFAULT_WAKE
    if "--wake" in sys.argv:
        i = sys.argv.index("--wake")
        wake = None if sys.argv[i + 1].lower() == "none" else sys.argv[i + 1]
    try:
        ears = Ears(mic=mic, wake=wake,
                    on_stop=lambda t: print("  [the car would halt here]"))
    except ListenUnavailable as exc:
        print(exc)
        raise SystemExit(1)

    with ears:
        print("Say something. Ctrl+C to quit.")
        try:
            while True:
                # Polled, not blocked: see Ears.wait. Blocking outright here
                # made Ctrl+C do nothing at all until someone spoke.
                said = ears.wait(timeout=0.3)
                if said is not None:
                    print(f"  stop word: {is_stop(said)}, hold: "
                          f"{ears.hold.is_set()}")
        except KeyboardInterrupt:
            print()
