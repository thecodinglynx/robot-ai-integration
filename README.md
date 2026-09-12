# AI-controlled Elegoo Smart Robot Car V4.0

Drive an Elegoo Smart Robot Car Kit V4.0 with a cloud vision language model.
The model runs off-board on a laptop, receives camera frames and sensor
readings, and issues high-level driving commands over the network.

Claude gets a photograph, the range finder and line sensor readings, and the
outcome of its last action. It replies with one tool call from a small
vocabulary. The host executes it through a reflex safety layer and hands back
what happened, along with a fresh photograph.

**[`CLAUDE.md`](CLAUDE.md) is the working brief** and the real documentation:
the protocol, the pin maps, the calibration numbers, and a long record of what
turned out to be true about this specific unit. Read it before changing
anything. This README is the short version.

## Hardware

| | |
| --- | --- |
| Controller | Arduino UNO R3 on a `SmartCar-Shield-V1.1` |
| Motor driver | TB6612FNG, four wheels, skid steer, no encoders |
| Camera | ESP32-WROVER (`ESP32-WROVER Camera-V1.5`) with an OV2640 |
| Sensors | HC-SR04 ultrasonic, three IR line sensors, MPU6050 |
| Head | Pan servo on D10, tilt servo on D11 (tilt is an addition, not stock) |
| Battery | **Two 18650 cells.** See the warning below |

The camera module runs replacement firmware, `firmware/elegoo_cam_station/`,
which joins your home network in station mode while keeping its own access
point as a fallback. It serves `/capture`, `/stream`, `/control` and `/status`
over HTTP, and bridges a JSON command socket on TCP port 100 through to the
UNO at 9600 baud.

## Safety

This robot moves under its own power and has driven into things. Two rules
earned the hard way, both explained at length in `CLAUDE.md`:

- **Anything that starts the motors owns the stop, and the stop belongs in a
  `finally`.** The firmware's own move timer cannot be relied on here.
- **Put the car on a box, wheels clear, whenever you are testing motion.** Use
  the floor only once the stop is proven. `stop_test.py` is that proof.

**Do not power this car from a 9 V PP3 battery.** It cannot supply four motors,
the rail collapses when they start, the radio browns out before anything else,
and the car keeps driving on the last pulse width with nothing able to reach
it. A day was lost to sophisticated software explanations for that. Measure
the pack **under load**, not at rest.

## Getting started

Flash the camera firmware, then:

```bash
cp firmware/elegoo_cam_station/secrets.h.example \
   firmware/elegoo_cam_station/secrets.h
```

Fill in your network. It must not isolate clients from each other; test by
pinging one device on it from another first.

Prove the car can be stopped, with it up on a box and the wheels clear:

```bash
python stop_test.py --host 192.168.1.211
```

Prove the safety layer refuses a wall and a table edge:

```bash
python safety_test.py --host 192.168.1.211 --move
```

Measure your own floor, because the constants below were taken on hardwood
with a charged pack and they drift:

```bash
python calibrate_motion.py --host 192.168.1.211
```

Then run the model loop:

```bash
pip install anthropic
set ANTHROPIC_API_KEY=sk-ant-...
python agent.py --host 192.168.1.211 --task "find the white ball and drive up to it"
```

On a shell that is not Windows `cmd`, use `export` rather than `set`.

Every run writes `runs/<timestamp>/` with the task, the system prompt, one
JSON line per turn including token usage, and every frame the model was shown.
Those directories are git-ignored: they are a photographic record of whatever
room the car was in.

## Layout

**The robot**

- `elegoo.py` — client library. Standard library only. Socket wrapper with the
  heartbeat responder on its own thread, tagged commands with reply matching,
  typed methods for the whole command set, and the still frame grabber. Holds
  no policy and makes no decisions.
- `safety.py` — the reflex layer. Polls the sensors on its own thread, holds
  veto power over every action, shortens a move to the clear space in front of
  it, and stops the car itself. No intelligence, no network.
- `agent.py` — the model loop, with logging built in.

**Firmware**

- `firmware/elegoo_cam_station/` — replacement ESP32 camera firmware. Station
  mode with the access point kept as a fallback, plus `/capture`, `/stream`,
  `/control`, `/status` and the port 100 command bridge.

**Verification and calibration**

- `stop_test.py` — proves the car can be stopped, before it is allowed on a
  floor. Run it after any change to the motion or stop path.
- `safety_test.py` — proves the safety layer refuses to drive into a wall or
  over a table edge.
- `calibrate_motion.py` — the lowest speed that moves the car, centimetres per
  burst, degrees per turn. Re-run it on a new floor or a low battery; the
  distances come from you and a tape measure, since there are no encoders.
- `demo_look_around.py` — the robot looks around, drives a little, and
  retraces its way back. Exercises every subsystem in one run.

## The robot's voice

The agent speaks its narration aloud as it drives. That text already exists and
is already written to be followed by a person, so it costs no extra tool call,
no extra turn and no extra tokens.

```bash
python voice.py --list          # what your machine can actually speak with
python voice.py                 # hear it say a few sample lines
python voice.py --voice Hazel "Turning left to face the door."
```

In a run: `--voice-name Zira`, `--voice-rate 240`, `--no-voice`.

**Personalities.** `--persona` changes how it talks and nothing else:

```bash
python agent.py --host 192.168.1.211 --persona sarcastic --task "find the ball"
```

`plain` (default), `funny`, `serious`, `annoyed`, `sarcastic`. They all speak
at the same rate, since what carries a character is the words rather than the
tempo; `--voice-rate` changes it. `python personas.py` prints them all with
examples.

The separation is deliberate and enforced in the prompt. A persona is a
speaking style, never a driving style: every one of them carries the same
prohibitions on inventing an observation to suit a line, on changing which tool
it calls or how long it keeps trying, and on arguing with the safety layer. A
sarcastic robot that stretches what it sees would be a worse robot, and that
failure would not announce itself, because the narration would still sound
fine. Each run's `run.json` records the persona and the exact prompt used, so
two runs can be compared.

**Adding better voices on Windows.** Two places, and they are not the same:

- *Settings, Time & language, Speech, Manage voices* installs the classic
  SAPI5 desktop voices that come with language packs.
- *Settings, Accessibility, Narrator, Add natural voices* installs the modern
  neural voices, which sound far better and still run offline.

**The trap: a newly installed voice may not appear in `--list`.** Voices added
through either of those often register only under
`HKLM\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens`, while `System.Speech`
reads `HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens`. So a voice can be
installed, work perfectly in Narrator, and be invisible here. Copying the token
key from the OneCore branch to the other one makes it visible; export the key,
edit the path in the `.reg` file, and import it.

If the built-in voices are not good enough, the better answer is a cloud
synthesiser rather than fighting the registry.

## Talking to the robot

```bash
pip install sounddevice faster-whisper
python ears.py                                        # check the microphone
python agent.py --host 192.168.1.211 --listen         # then talk to it
```

**Say "robot" and then what you want.** The microphone stays open, speech is
segmented out of the room and transcribed on the laptop, and what you said
reaches the robot on its next turn. `--task` becomes optional: it waits for
you, does what you say, reports back, and waits again, until Ctrl+C. Say
something new mid-task and it changes course.

Anything without the wake word in it is transcribed and thrown away, so
conversation in the room is not taken as instructions. `--wake none` goes back
to pressing Enter before and after each instruction, which is worth having in
a noisy room or when you would rather the microphone were not open.

**Saying "stop", "halt" or "freeze" halts the car straight away**, without
waiting for the model, and it then refuses to move until you speak again. It
still takes about a second to transcribe, so the power switch and Ctrl+C remain
the real emergency stops.

The microphone is the laptop's on purpose, not one on the robot: a Bluetooth
speaker's microphone drops its sound to phone quality while open, a microphone
on the robot would mostly hear the motors, and it would hear the robot's own
voice. For that last reason the ears go deaf while the robot is talking, so it
cannot transcribe itself and obey it.

The speech model downloads once, about 150 MB. `--stt-model tiny.en` is
quicker, `small.en` more accurate.

**To make the voice come from the robot**, pair a small Bluetooth speaker with
the laptop, make it the default output, and fix it to the chassis behind the
head. No firmware is involved. If it answers phone calls, Windows shows it as
two devices: pick the **Stereo** one, because the Hands-Free one is phone-call
quality. Untick *Handsfree Telephony* in its properties if Windows keeps
switching back.

## What is not in this repository

- **Elegoo's official download bundle**, 262 MB, their copyright, and a free
  download from elegoo.com. Get your own copy: the camera source in it is
  ground truth for the pin map, and every online source for this board has it
  wrong. See the References section of `CLAUDE.md`.
- **A backup of the stock camera firmware.** Read your own off your own chip
  before you flash anything. Do not restore someone else's.
- **Run logs and build photographs**, for the privacy reason above.
- **The one-off diagnostics.** A long day of chasing a car that would not drive
  straight, would not stop, and then would not turn produced a pile of
  single-purpose probe scripts. What each of them found is written up in
  `CLAUDE.md`, which is the part worth keeping. The scripts themselves were
  disposable, and several were built on theories that turned out to be wrong.

## Status

Phases 00 to 06 of the plan in `CLAUDE.md` are done: the platform is
characterised, the client library and safety layer are built and tested
against the car, and the model loop runs with full logging. Calibration on
hardwood with a charged pack gives 0.55 mm/ms driving and 0.17 deg/ms turning.

Known and unresolved: the car will no longer pivot in place, and turns are
arcs. Both `elegoo.py` and the agent's prompt account for it. The cause is not
understood and is written up honestly rather than guessed at.
