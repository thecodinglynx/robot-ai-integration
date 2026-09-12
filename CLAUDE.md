# AI-controlled Elegoo Smart Robot Car V4.0

Drive an Elegoo Smart Robot Car Kit V4.0 (with camera) using a cloud vision
language model. The model runs off-board on a laptop, receives camera frames,
and issues high-level driving commands over the network.

`feasibility-report.html` in this folder is the background research. This file
is the working brief. Where the two disagree, this file wins.

## Confirmed on this specific unit

Verified physically or by direct observation. Trust these.

- Shield is silkscreened `SmartCar-Shield-V1.1`, which is the V4.0 board.
- **Camera module is an ESP32-D0WDQ6 with 4 MB of flash.** That is the
  original ESP32, the WROVER class part, **not** the S3. Read directly off the
  chip with esptool on 2026-09-06:

      Chip type:  ESP32-D0WDQ6 (revision v1.0)
      MAC:        94:b5:55:14:b1:bc
      flash:      0x400000

  An earlier entry here claimed the S3, inferred from the module having a USB
  Type-C port. That inference was wrong. Confirmed from the board itself:

  | | |
  | --- | --- |
  | Board silkscreen | `ESP32-WROVER Camera-V1.5` |
  | Module | Espressif ESP32-WROVER, FCC ID 2ACJ72-ESP32WROVER |
  | USB bridge | **CH340C**, between the Type-C socket and the module |
  | Camera | OV2640 on a `Y-OV2640-12.7-V1.0` ribbon |
  | To the UNO | 4 pin header marked RX TX GND VCC |
  | Buttons | one, on the front |

  The Type-C socket goes to the CH340C, not to the chip, so it was never
  evidence of native USB. **A connector shape says nothing about the silicon
  behind it.** Flashing needs no external adapter anyway, contrary to what the
  research predicted for a WROVER: the on-board bridge does the job, with
  esptool auto resetting via DTR and RTS.
- **The stock firmware, read out of the backup image.** Built against ESP-IDF
  `v3.2.3-14-gd3e562907` via esp32-arduino-lib-builder, bootloader dated
  **15 July 2019**. That is the arduino-esp32 **1.0.x** era, so match it when
  rebuilding rather than reaching for a current 2.x or 3.x core.
- **Partition layout, read off the chip.** Byte for byte Arduino's
  `huge_app.csv`, so the IDE setting is **Huge APP, 3 MB no OTA, 1 MB SPIFFS**:

      nvs       20 KB  @ 0x009000
      otadata    8 KB  @ 0x00e000
      app0    3072 KB  @ 0x010000
      spiffs   960 KB  @ 0x310000

- **The access point name is derived from the MAC.** `ELEGOO-BCB11455B594` is
  the chip MAC `94:b5:55:14:b1:bc` with the bytes reversed.
- **There are protocol tokens the command table does not mention.** Found as
  strings in the firmware: `{Factory}`, `{BT_detection}`, `{BT_OK}`,
  `{WA_detection}`, `{WA_OK}`, `{WA_NO}`, alongside the familiar `{Heartbeat}`
  and `{"N":100}`, plus `[Client connected]` and `[Client disconnected]`.
  Face recognition code from Espressif's CameraWebServer example is also
  compiled in. None of this is investigated.
- Car comes up as its own access point. Its address is `192.168.4.1`.
- `http://192.168.4.1/capture` returns a still frame. Working.
- Raw command socket on TCP port 100 works. Verified 2026-09-06.
- **The socket needs traffic at least every ~3.5 s.** Sending nothing dropped
  the connection at 3.6 s, which matches the documented three missed beats.
  Echoing `{Heartbeat}` back held it, and so did sending unprompted pings, so
  the firmware appears to want activity rather than a specific reply. Echo is
  the strategy to use: it is clocked by the car, needs no timer of its own, and
  gives the dead man switch for free.
- **Replies are not JSON.** See the reply format below.
- **The camera is on a mast, not down at bumper height.** Photographs of the
  build, 2026-09-06. The camera board stands upright on a bracket well above
  the ultrasonic sensor. **Lens height measured at 16 cm**, not the 60 mm the
  feasibility report assumed, so that report's worry about a lens scraping the
  floor does not apply to this build.
- **The camera and the range finder share the pan bracket**, so they turn
  together and always look the same way. This is what the architecture assumes
  when it pairs a frame with a distance in the same prompt, and it is now
  confirmed rather than hoped for.
- **The camera board leans backwards on its mount**, which is why the frames
  are full of ceiling. Visible in the side on photograph. The fix is at the
  bracket, and the target is **10 to 15 degrees below horizontal**, not level.
  See the aiming note below.
- **A tilt servo has since been fitted to D11**, 2026-09-06, and works with no
  firmware change: command 5 with `D1=2` aims it. Higher is further down. The
  original build had pan only, and the earlier note to that effect is now
  historical.
- **The camera was remounted on 2026-09-06 and the travel re-measured.**
  Horizon position down the frame: 87% at 60 and below, 67% at 90, 58% at 100,
  49% at 120 and beyond. `elegoo.py` clamps to 68 to 118, provisionally.
- **Both ends are a firmware clamp, not the mechanism.** Sweeping the whole
  servo range produced **no grinding and no load at either end**, and the
  camera simply stopped responding past 60 and past 120, while the mechanism
  swings much further by hand. A clamp of 90 plus or minus 30 in the UNO
  firmware fits every observation. Earlier notes here called these mechanical
  stops and worried about stalling the SG90; that was wrong. The firmware never
  lets the servo reach a stop.
  - The community protocol reference documents no range for command 5, and the
    official firmware repository shows a code path with no `D1` servo selection
    and the angle divided by ten, neither of which matches this car. That
    repository is not the firmware on this unit. Trust the measurements.
  - **To reach further down**, either re-seat the servo horn a few teeth so the
    60 to 120 window maps onto a lower part of the arc, which needs no
    software, or widen the clamp in the UNO firmware, which is worth bundling
    with the battery voltage command that is already outstanding.
- **There is at least one command we do not have documented.** The community
  reference mentions `N=106`, preset servo positions, valid for 1 to 5. Not
  investigated.
- **The angle to aim mapping is not stable.** The same commanded 70 produced a
  visibly different aim in the two runs on 2026-09-06, the second one lower by
  something like 20 degrees of servo travel. The likely cause is the mount or
  the servo horn slipping while the first run drove it into the stops at 50 and
  130. Consequences: check the horn screw, treat tilt angles as relative rather
  than as a calibration, and re-check by eye after any knock. This is also why
  the clamp matters.
- **Useful tilt angles on the current mount.** 100 puts the horizon a little
  above the middle and suits spotting something across the room. 120 is the
  lowest the firmware allows and suits close work, though it only reaches about
  optically level. Both improve if the horn is re-seated.
- **The range finder does not tilt with the camera.** Confirmed twice, before
  and after the remount. Eleven readings across the first sweep returned raw 47
  every single time, zero spread; thirteen across the wider sweep after the
  remount returned 46 in all but one. So the camera can look wherever it likes without blinding the safety
  layer, which is the good outcome. That 47 also converts to 61 cm against a
  measured 60, confirming the scale factor a third time.
- **Do not trust frame size as a proxy for whether the servo moved.** The
  automatic check in `--gimbal` reported no movement across a 20 degree step
  that the pictures clearly showed. Compressed size tracks scene content more
  than view angle. Compare frames by eye and watch the horizon.
- **Tilting down fixed the exposure as a side effect.** With the ceiling lights
  out of frame the metering stops chasing them and the floor is properly
  exposed. Worth still setting `/control` exposure explicitly, since a window
  will reintroduce the problem, but it is no longer urgent.
- **Two servos plus four motors did not brown out the 5 V rail** at speed 150,
  across two runs. Retest on a low battery, which is when a marginal supply
  actually fails.
- **The heartbeat echo holds a connection indefinitely.** The second gimbal run
  sat idle for six and a half minutes between steps, with nothing but echoed
  beats, and the socket was still live afterwards. The head turns left and right and that is the whole range
  of motion, so the vertical aim is fixed and can only be changed mechanically.
  This contradicts the feasibility report, which described two servo channels
  aiming the camera. The pin map still lists a Y axis on D11; whether that
  channel is unpopulated, unused, or drives something else here is not known,
  but nothing on the camera responds to it.

## Established from documentation, not yet verified here

Treat as likely but confirm before depending on it.

- Motion controller is an Arduino UNO R3 at 9600 baud.
- Motor driver is TB6612FNG. Inferred from the pin map: pulse width modulation
  plus a direction pin per side plus a standby pin on D3. The L298 used on the
  V3.0 generation has no standby line.
- Camera module reaches the UNO over the shield's P8 header, which carries
  transmit, receive, ground and 5 V and does the 3.3 V to 5 V level shifting.
- The shield has an **Upload / Cam switch**. Upload to reflash the UNO, Cam for
  normal operation. If commands stop landing, check this first.
- MJPEG video stream at `http://192.168.4.1:81/stream`.
- Camera settings at `http://192.168.4.1/control?var=<name>&val=<value>`,
  covering framesize, quality, brightness, contrast and around twenty others.
- Raw command socket on **TCP port 100**. JSON frames sent there are forwarded
  to the UNO.
- ESP32 sends `{Heartbeat}` roughly every second and drops the client after
  three missed responses, then issues a stop itself. This is a useful dead man
  switch: keep it rather than working around it.

### UNO pin map

| Function | Pin |
| --- | --- |
| Right motor PWM / direction | D5 / D7 |
| Left motor PWM / direction | D6 / D8 |
| Motor standby | D3 |
| Ultrasonic trigger / echo | D13 / D12 |
| Servo Z axis / Y axis | D10 / D11 |
| Line sensors left / middle / right | A2 / A1 / A0 |
| Battery voltage divider | A3 |
| RGB light emitting diodes | D4 |
| Infrared receiver | D9 |
| Button | D2 |
| MPU6050 inertial measurement unit | I2C |

### Camera module pin map

For the `ESP32-WROVER Camera-V1.5` board.

**From Elegoo's own source**, `elegoo-docs/02 .../04 Code of Carmer (ESP32)/`,
obtained 2026-09-07. The camera is `CAMERA_MODEL_M5STACK_WIDE`. Every online
source for this board says `WROVER_KIT`, and every one of them is wrong;
Elegoo's own `camera_pins.h` contains the WROVER_KIT block, commented out,
which is presumably how the error spread.

A cross check that confirms it: Elegoo's sketch uses GPIO 4 for Serial2 TX.
Under WROVER_KIT that pin is the Y2 data line and would collide. Under
M5STACK_WIDE it is unused.

**The master clock is 10 MHz**, not the usual 20. Their source changed it down
and left the old value in a comment.

Notes:

- **The camera is powered through the 4 pin cable from the car**, not from the
  module's USB. Probing with that cable detached scans a dead sensor and finds
  nothing, which looks identical to a wrong pin map. Always probe with the
  module attached and the car switched on.
- The data pins, VSYNC, HREF and PCLK are still **unverified**. If a capture
  comes back corrupt rather than failing outright, that is where to look.

| Signal | GPIO | Signal | GPIO |
| --- | --- | --- | --- |
| PWDN | -1 | Y6 | 39 |
| RESET | **15** | Y5 | 5 |
| XCLK | **27** | Y4 | 34 |
| SIOD (SDA) | **22** | Y3 | 35 |
| SIOC (SCL) | **23** | Y2 | 32 |
| Y9 | 19 | VSYNC | 25 |
| Y8 | 36 | HREF | 26 |
| Y7 | 18 | PCLK | 21 |

**The UNO is on Serial2, GPIO 33 for receive and GPIO 4 for transmit**, at
9600. From Elegoo's own source. UART0 goes only to the CH340C USB bridge and is
free for debug.

An earlier note here said UART0 was shared three ways, on the evidence of
seeing `[Client connected]`, `{"N":100}` and `{ok}` on the USB port at 9600.
That was wrong: Elegoo's sketch echoes the Serial2 traffic to UART0 with
`Serial.print` for debugging, so what looked like the link itself was a copy of
it. The mistake also produced firmware that relayed commands to the USB bridge
rather than to the Arduino.

**Debug output over USB is therefore free**, which is worth using: the earlier
belief that anything printed would reach the UNO's parser is not true.

### Command protocol

Frames are JSON objects. `N` is the command number, `H` an optional tag echoed
back in the reply, `D1` to `D4` parameters, `T` a duration in milliseconds.

| N | Function | Parameters |
| --- | --- | --- |
| 2 | Timed movement | D1 direction, D2 speed, T milliseconds |
| 3 | Continuous movement | D1 direction, D2 speed |
| 4 | Direct left and right speeds. **D1 is the RIGHT motor**, see below | D1, D2 |
| 5 | Servo angle | D1 servo id, D2 angle |
| 8 | RGB light emitting diodes | D1 sequence, D2 to D4 colour |
| 21 | Ultrasonic query | D1 mode |
| 22 | Line sensor query | D1 mode |
| 23 | Ground loss check | none, returns true or false |
| 100 | Clear mode. **Does not stop the motors.** | none |
| 101 | Built in mode | D1: 1 trace, 2 avoid, 3 follow |
| 110 | Programming mode, clears stock behaviours | none |

Direction codes: 1 forward, 2 backward, 3 left, 4 right, 5 forward-left,
6 backward-left, 7 forward-right, 8 backward-right, 9 stop.

### Reply format

Confirmed 2026-09-06. Replies are **not** JSON, despite commands being JSON.
Each is a brace delimited `{<H>_<value>}` token, where `<H>` is the tag sent
with the command and `<value>` is `ok`, `true`, `false`, or a bare number.
Commands sent without an `H` get an untagged `{ok}` with no underscore, so send
a tag and match replies on it. The exception is command 100, which answers
untagged whether you tagged it or not.

| Sent | Received |
| --- | --- |
| `{"N":100}` | `{ok}`, untagged even when a tag is sent |
| `{"N":110,"H":"1"}` | `{1_ok}` |
| `{"N":21,"D1":1,"H":"2"}` | `{2_false}` |
| `{"N":21,"D1":2,"H":"3"}` | `{3_150}` |
| `{"N":22,"D1":0,"H":"4"}` | `{4_822}` |
| `{"N":22,"D1":1,"H":"5"}` | `{5_612}` |
| `{"N":23,"H":"6"}` | `{6_false}` |
| `{"N":5,"D1":1,"D2":90,"H":"7"}` | `{7_ok}` |

Reading of those, some of it inference rather than fact:

- **21 takes a mode in D1.** `D1=1` is a boolean "obstacle within threshold",
  `D1=2` is a numeric distance. The two agree with each other: `false` and 150
  are consistent.
- **21 is not centimetres.** Calibrated 2026-09-06 at 10, 20, 30, 40, 60, 80
  and 100 cm, readings 8, 15, 22, 32, 45, 62, 77. Least squares over all seven
  gives `raw = 0.7714 x cm`, so **cm = raw x 1.296**, residuals within 1.3
  counts across the whole range. The intercept is -0.18 counts, zero at this
  resolution, so it is a scale error and not a datum error. The seven point fit
  agrees with the original three point one to four significant figures.
- **21 `D1=1` flips between 20 and 40 cm.** True at raw 15, false at raw 32.
  Somewhere around 26 to 40 cm, and not narrowed further. Use `D1=2` with your
  own threshold instead: it is a number rather than someone else's opinion.
- **150 is a ceiling, not a reading.** Open space returned exactly 150 five
  times running, and the smoke test hit exactly 150 twice more. That is about
  195 cm on the scale above. Treat 150 as "at least 195 cm **or no echo at
  all**", never as a distance. This matters for the safety layer: a dead sensor
  and a clear corridor are the same number, and the failure mode of trusting it
  is not stopping.
- **Readings repeat suspiciously often, but not always.** Five polls 50 ms
  apart returned the same value every time at every distance, and a later sweep
  returned 46 thirteen times running against a static target. One reading in
  that sweep came back 35, so it is not frozen, but the firmware may still be
  caching and refreshing on its own clock. Worth measuring before relying on a
  poll rate for the fast loop.
- **22 takes a sensor index in D1, and all three channels work.** Raw analogue,
  and **darker reads higher**. Measured 2026-09-06:

  | Channel | Pale | Dark | Midpoint |
  | --- | --- | --- | --- |
  | `D1=0` | 726 | 926 | 826 |
  | `D1=1` | 330 | 682 | 506 |
  | `D1=2` | 612 | 802 | 707 |

  The baselines differ enough that **thresholds have to be per channel**, not
  one global number. Channels 0 and 2 have only about 200 counts of contrast,
  so they are the weak ones.
- **23 is explained, and it can never work on this car.** From Elegoo's UNO
  source, read 2026-09-07. The flag behind it is set by:

      if (R > 950 && M > 950 && L > 950)  Car_LeaveTheGround = false;
      else                                Car_LeaveTheGround = true;

  and command 23 prints the **inverse** of that flag, so a reply of `false`
  means "on the ground". It needs **all three** channels above 950. On this car
  the middle channel reads **725 held in the air**, with nothing underneath it
  at all, so it never crosses the threshold, the condition can never be met,
  and the reply is always `false`. That is exactly what eight test combinations
  found on 2026-09-06.

  So it is not a bug in how we called it: one weak sensor defeats Elegoo's own
  detector. Not worth revisiting. Reading the line sensors directly, as
  `over_edge` does, is better regardless, because we see the numbers instead of
  inheriting someone else's threshold.
- **The cliff stop is line sensor channel 2.** With nothing underneath, the
  sensors saturate near the top of the scale. Measured 2026-09-06:

  | | ch0 | ch1 | ch2 |
  | --- | --- | --- | --- |
  | Floor | 530 | 155 | 47 |
  | Held in the air | 1017 | 725 | 1020 |
  | Front overhanging a table edge | 1017 | 725 | 1021 |
  | Darkest surface tested | 926 | 682 | 802 |

  Note the overhang row is identical to the in air row: the whole array is at
  the front and clears the edge together, so a square approach is covered. An
  edge met at a sharp angle may trip only part of the array.

  The real constraint is the bottom row. **A dark floor looks a lot like no
  floor.** Margin between darkest surface and no surface is 91, 43 and 218
  counts on the three channels, so channel 2 is the only one with room to work
  in. Threshold set at 900. Validate against the actual floors before trusting
  it, and remember the failure directions are not symmetric: a false stop on a
  black rug is a nuisance, a missed edge puts the car on the floor.
- **Command 100 does not echo the tag.** Confirmed directly: `{"N":100,"H":"99"}`
  answers `{ok}`, not `{99_ok}`. The reply is there, it just carries no tag, so
  anything matching replies by tag has to special case it. Found the hard way
  when the first real run of the client timed out on its opening stop. Whether
  any other command does the same is untested; 100 is the only one caught at
  it, and every other command in the phase 01 capture echoed its tag.
- **A timed move's reply can be a second late when the safety layer is
  polling.** The UNO prints it from its main loop when the move timer expires,
  and it then queues behind the polling traffic. A 400 ms move missed a 1.9 s
  deadline on 2026-09-07 while the car was driving perfectly. `Car.move` now
  allows `reply_timeout + duration + 1.0 s`, and `calibrate_motion.py` retries
  once rather than treating a late reply as a refusal.
- Round trip is 20 to 110 ms, so the **wire** carries 10 to 20 Hz comfortably.
  Whether the sensor behind it refreshes that fast is a separate question,
  see the caching note above.

### The serial link is the scarce resource

The ESP32 to UNO link is **9600 baud, about 960 bytes a second**, and every
sensor query and its reply is roughly 33 bytes. That budget is small enough to
break things, and it did.

The safety layer originally polled the distance and all three line channels at
10 Hz. That is 132 bytes per poll, 1320 bytes a second, **138% of the link**.
The line saturated, commands queued behind the polling, and the reply to a
timed move arrived after the client had given up. The symptom was a car that
turned happily and would not drive, which looks like a motor or battery
problem and is neither.

Now: distance plus **only** the cliff channel, at 8 Hz. 528 bytes a second,
55% of the link, leaving room for the commands that actually do things. Only
channel 2 is read because only channel 2 is used.

**Budget anything new against those 960 bytes a second.** It is the tightest
resource on the robot and nothing warns you when you exceed it.

Related, same cause: **a timed move's reply cannot arrive until the move
finishes.** Command 2 only records a mode and a start time and prints nothing;
the `{tag_ok}` comes from the main loop when the timer expires. So the reply
timeout has to include the requested duration, which `Car.move` now does.

### Battery, and how to tell

There is **no command that reports voltage**. The full command set in Elegoo's
UNO source is N 1, 2, 3, 4, 5, 7, 8, 21, 22, 23, 100, 101, 102, 105, 106 and
110, and none of them touches the battery. Reading it over the wire still needs
a firmware change, but the formula is now known, so it is a five line job:

    float Voltage = analogRead(A3) * 0.0375;
    Voltage = Voltage + (Voltage * 0.08);   // Elegoo's 8 percent compensation

**Meanwhile the car already tells you.** Below `VoltageDetection = 7.00` V the
RGB LEDs flash **red**: roughly five blinks over half a second, then dark for
2.5 seconds, repeating. It is debounced over 500 readings, about 5 seconds, so
it will not trigger on a momentary sag under load. Red flashing means charge
it. No red flashing means the pack is above 7 V.

A multimeter across the pack is the other answer, and the only one that gives a
number rather than a threshold.

### Commands the brief did not have

From the same source: **N 7**, **N 8** for the LEDs, **N 102** rocker control
mode, **N 105** LED brightness, and **N 106**, which takes `D1` from 1 to 5 and
moves the head to one of five preset positions. None investigated.

### Camera home position

`Car.home()` points the head straight ahead and level, and `CarConfig` calls it
on connect by default. Servos hold position when the power goes off, so without
it the first frame of a session is whatever the last session left behind.

Straight is `PAN_CENTRE + PAN_TRIM`, level is `TILT_LEVEL + TILT_TRIM`, with
`TILT_LEVEL` at 120 because that is where the horizon sat halfway up the frame.
Nothing can measure where a servo actually is, so if the head does not end up
looking where it should, correct the trims once by eye rather than adjusting
call sites.

### Speed, and why forward is weaker than turning

Read out of Elegoo's own UNO source, `TB6612 & MPU6050/SmartRobotCarV4.0_V1`,
on 2026-09-07. This explains a demo where the car turned happily and would not
drive forward at all.

**Turning and driving take different code paths.**

| Direction | Path | Consequence |
| --- | --- | --- |
| Left, Right | `DeviceDriverSet_Motor_control` directly | full commanded speed, uninterrupted |
| Forward, Backward | `ApplicationFunctionSet_SmartRobotCarLinearMotionControl` | see below |

Straight line motion goes through a yaw correction loop that, **every 10 ms,
stops both motors** in order to read the MPU6050:

    if (en != directionRecord || millis() - is_time > 10) {
      DeviceDriverSet_Motor_control(direction_void, 0, direction_void, 0, ...);
      MPU6050_dveGetEulerAngles(&Yaw);
    }

So forward and backward are chopped by the sampling cycle and turning is not.
**At the same commanded speed, straight motion is materially weaker**, and
below some threshold it will not start at all. Use a higher number for driving
than for turning, which is the opposite of what seems natural.

**The clamps, confirmed.** Command 2 puts the car in `CMD_CarControl_TimeLimit`,
which sets `Kp = 2` and `UpperLimit = 180`. The correction output is then
clamped to a minimum of 10 and a maximum of 180, so:

- speeds above **180 are wasted** for forward and backward, they clamp
- speeds below the stiction threshold produce noise and no motion
- **turning is not clamped** by any of this

**Command 2 stops acknowledging at the speeds where the car pivots.** Same
runs. A stalled motor draws several times its running current and would sag the
rail enough to lose the reply, which fits: at 140 the low side is commanded to
10 and is stalled rather than turning. Observed
2026-09-07: at speed 100 a 400 ms move produced a pivot on the spot and no
reply within 2.9 s, while at 165 the same command replied in 0.66 s and drove
straight. A speed-dependent missing acknowledgement is not something a longer
timeout can fix, and it is not explained yet. It does not block anything, since
the useful range is 150 to 180 and `calibrate_motion.py` now starts its sweep at
140, but it is an open question and it is recorded as one.

**Low commanded speeds pivot instead of driving, and it IS the correction.**
Observed 2026-09-07 at speeds 100 and 140: the car turns on the spot rather
than translating. An earlier note here blamed stiction and friction. That was
wrong, and the thing that ruled it out was the floor: this is hardwood, the
easiest surface there is.

The arithmetic is symmetric about the commanded speed:

    R = speed + drift*2      clamps at 180
    L = speed - drift*2      floors at 10

so **the same yaw drift produces a pivot at 140 and a wide curve at 165**. At
140 the low side floors at 10 and stops, and the car turns on the spot; at 165
it sits around 35 and the car still translates, which is why 165 was read as
"straight". 165 was not fine, it was less bad. The drift has been present
throughout and the speed only changes how visible it is.

The one variable known to control the drift is the boot-time gyro zero, and
`straight_test.py --floor` had the clean power-up procedure while
`calibrate_motion.py` did not. Test that before looking anywhere else.
`calibrate_motion.py` records a pivot as its own answer rather than as a
failure to move.

**Measured on hardwood, 8.2 V pack, 2026-09-07**, with `calibrate_motion.py
--direct --no-safety`, so command 1 and no yaw correction in the path:

| | |
| --- | --- |
| Lowest speed that drives | **150** |
| Working speed used | 170 |
| Distance per 400 ms burst | **22 cm** |
| Rate | **0.55 mm per ms**, so 10 cm is about 180 ms |

An earlier run on the same day, before the pack was fully charged, gave 17.6 cm
and 0.44 mm per ms. The 25% spread between two runs on the same floor is the
best evidence there is that these are order-of-magnitude figures rather than
constants.

Turning was not measured: the turn drove straight instead of rotating, which
is the fault below. These numbers drift with charge and surface, so re-measure
on carpet and on a low pack before treating them as constants.

**Working values**: drive at 150 to 180, turn at 100 to 130. Still worth
measuring the actual centimetres per burst with `calibrate_motion.py`, since
that varies with battery and floor and nothing on the robot can measure it.

### The IMU is fine. That whole diagnosis was the battery

**Retracted 2026-09-07.** This section used to say command 2 forward and
backward produced no motion and no reply on this car, that the cause was
Elegoo's yaw correction loop hanging on an IMU read that never returned, and
that straight motion therefore had to go through command 1. All of it was
wrong, and the workaround it justified caused a runaway.

**The car was running on a 9 V PP3 battery.** It cannot supply four motors. The
rail collapsed whenever they started, the ESP32's radio browned out first, and
the UNO carried on writing the last pulse width. On 18650 cells, tested with
`straight_test.py`:

    command 2 forward   moved, {5_ok} after 0.66 s
    command 2 backward  moved, {10_ok} after 0.67 s

A 0.66 s reply to a 600 ms move is exactly what the firmware source predicts:
command 2 records a mode and a start time, and the main loop prints `{tag_ok}`
when the timer expires. Nothing was hanging.

`CarConfig.straight_via_direct_motor` is now **False**, so straight moves go
back through command 2 and **the firmware owns the timer**. That is the safer
arrangement: the car stops itself at the end of the move even if the host
crashes, loses the network or is killed. Under command 1 the clock was a
`time.sleep` on the host, and a dead host left the car running until the
heartbeat gave up 3.5 s later.

Nothing is lost by switching back. The safety layer can still cut a drive
short, because `halt()` is command 1 at speed 0, which changes the functional
mode out from under the timed move and stops the motors.

**The lesson, since it cost a day.** Every symptom was consistent with a
sophisticated firmware fault, and the reasoning that built that story was
sound at each step. What made it wrong was an unexamined assumption about the
hardware underneath. Before diagnosing intermittent misbehaviour in software,
**measure the power supply under load**, not at rest: the PP3 read 8.x V on a
multimeter throughout, and alkaline cells recover their voltage when rested,
which is precisely why nothing reproduced between runs.

`ApplicationFunctionSet_SmartRobotCarLinearMotionControl` does still stop both
motors every 10 ms to read the gyro, so straight motion is genuinely weaker
than turning at the same commanded speed. That part of the old section holds,
and it is recorded under Speed above. What does not hold is that the read fails.

### The correction spins the car, and the reference never re-zeroes

**Observed 2026-09-07 on the floor, on 18650 cells**: command 2 forward turns
the car on the spot instead of driving it. The straight paths were only ever
tested on a box before, where free wheels at very different speeds both look
like "the wheels turned", so this was invisible until now.

The mechanism is visible in the source:

    if (en != directionRecord || Car_LeaveTheGround == false)
        yaw_So = Yaw;
    int R = (Yaw - yaw_So) * Kp + speed;   // clamps at 180
    int L = (yaw_So - Yaw) * Kp + speed;   // clamps at 10

**An earlier note here had this backwards.** It said the yaw reference resets
whenever `Car_LeaveTheGround` is false, which on this car is always, leaving
the correction term at zero and therefore harmless. The flag is set false only
when **all three** line sensors read above 950; on a floor they read about 530,
155 and 47, so the flag is **true** and the reference is **never re-zeroed
during a move**. Any steady gyro bias makes `Yaw - yaw_So` grow without limit,
R saturates at 180 while L floors at 10, and the car spins. The correction is
not inert, it is the fault.

**Cause found, same day: the boot-time gyro offset.** `MPU6050_calibration()`
samples the gyro at startup to find its zero, and handling the car while it
powers up corrupts that zero, after which the yaw drifts steadily from the
first second. `straight_test.py --floor`, run after a power cycle with the car
left still for ten seconds, drove **straight on all three paths**: command 2
with the correction, command 1 without it, and command 4 without it. So the
correction works, the IMU works, and there is nothing to fix in software.

The second candidate, that this car has a **QMI8658C** rather than an MPU6050
and is running the wrong build, is therefore not needed as an explanation.
`elegoo-docs` does ship a separate `TB6612 & QMI8658C` firmware, so it stays on
the list if the symptom ever returns after a clean power-up.

`straight_test.py --floor` is the tool to re-run if it does: it compares
command 2 against the two paths with no correction in them, and asks about the
quality of the motion rather than whether the wheels moved.


### Command 4's parameters are named backwards

**`D1`, documented as the left speed, drives the RIGHT motor.** `D2` drives the
left. Follow it through Elegoo's own source:

    CMD_MotorControlSpeed_xxx0(is_Speed_L, is_Speed_R)
      -> Motor_control(direction_just, is_Speed_L,
                       direction_just, is_Speed_R, ...)

    // and inside Motor_control:
    { //A...Right     gets speed_A, which was passed is_Speed_L
    { //B...Left      gets speed_B, which was passed is_Speed_R

Found 2026-09-07 when the agent identified a target on its right, said so, asked
to turn right, and turned left. Every turn was mirrored. `turn_test.py` could
not have caught it: both directions rotated 270 degrees and nothing recorded
which way.

`Car.tank(left=, right=)` corrects for it, so callers can use the names and
mean them. Anything sending `{"N":4}` by hand must not.

**Worth checking whether command 1's `D1` motor selection is crossed the same
way**, since it uses the same A-is-right convention. Not tested; `D1=0` means
both motors, which is all this project uses.

### Turning is command 4, and it is an arc rather than a pivot

**Measured 2026-09-07 with `turn_test.py`**, on the floor, four 400 ms bursts
per method:

| method | rotation |
| --- | --- |
| command 2 left, speed 120 | **drove straight** |
| command 2 left, speed 170 | **drove straight** |
| command 4 tank, left 0 right 170 | **270 deg**, 68 per burst |
| command 4 tank, left 170 right 0 | **270 deg**, 68 per burst |

So command 2's Left and Right no longer rotate this car at any speed tried,
and the tank command turns it well and symmetrically: **68 degrees per 400 ms
burst at speed 170**, about 0.17 deg/ms, so a right angle is roughly 530 ms.
`TURN_SPEED` is 170 and `CarConfig.turn_via_tank` routes turns through
`Car.turn_tank`.

**The turn is now an arc, not a pivot, and that is by construction.** A tank
turn drives one side and holds the other at zero, so the car swings about the
stopped wheel. A pivot in place requires the two sides to run in **opposite**
directions, which is what command 2 does and what has stopped working.

**Consequence for anything using it: turning moves the car.** Rotating about a
stopped wheel means the centre travels an arc of roughly the track width as
radius, so a 68 degree burst carries the car something like 15 to 20 cm as well
as turning it. A pivot did not. Anything that assumed turning was
position-neutral, the agent prompt included, needs to say so.

**Why the pivot stopped working is NOT known.** The car did pivot in place at
speed 120 earlier the same day, repeatedly and unmistakably. What is
established since:

- command 1 drives both sides forward, and both sides backward, correctly at
  speed 165. Reverse works per channel.
- a command 2 turn stalls one side at 80 and at 165, **watched in the air**,
  where there is no load and no current argument to make.
- the stalled side swaps with the direction of the turn, so it is not one bad
  channel.

The failing case is specifically **the two sides in opposition**. On a TB6612
the channels are independent, so there is no obvious electrical reason for
mixed directions to behave differently from matched ones. Worth noting that
between the working pivot and now, the car ran away several times, drove into
an obstacle, and spent long periods with one side stalled at speed, which is
the highest current a motor draws; degradation is a candidate but nothing
points at it specifically.

**Cheap thing to try if it matters later**: command 3, the continuous move,
takes the same Left and Right directions through the same motion function but a
different mode handler. If command 3 pivots and command 2 does not, the fault
is in the timed-move path rather than in the motors.

**Wrong turns taken today**, recorded so they are not retaken: a stuck motor
direction line, the yaw correction as positive feedback, an I2C hang in the
gyro read, the safety layer's polling starving the serial link, stiction on the
floor, a motor start threshold applying to turns, and an invariant that the two
sides can never run in opposition. Two things actually moved the diagnosis
forward all day, and neither was a theory: measuring the battery under load,
and looking at the wheels instead of at the car.


### Straight motion goes through command 1

Command 2's straight path runs Elegoo's yaw correction, which clamps its low
side to 10:

    int L = (yaw_So - Yaw) * Kp + speed;   // clamped to 10

Whatever is stalling a side during turns does the same here, and the car
circles at every speed from 100 to 180, on a charged pack, with the sensor
polling off. Command 1 has no correction in its path and drove straight at all
of them, which is where the calibration numbers above come from.

So `straight_via_direct_motor` is **on**. The cost is the firmware's move
timer: a host that dies mid-drive leaves the car running until the ESP32's
heartbeat gives up at about 3.5 s, rather than at the end of the move.
Durations are capped far below that and `halt()` is verified.


### Talking to it

`--listen`, added 2026-09-11. Say "robot" and then the instruction. The
microphone stays open, an energy gate segments speech out of the room, and each
utterance is transcribed locally with faster-whisper (`base.en`, int8, CPU), in
`ears.py`. `--wake none` falls back to push to talk, Enter to start and stop. What was said is appended to the next user turn as "The person
just said: ...". With `--listen` a report means "done, waiting", the turn cap
applies per instruction, and the session ends at Ctrl+C.

Three decisions worth keeping:

- **A spoken stop bypasses the model.** It halts through
  `SafetyLoop.emergency_stop` and `Car.halt` from the transcription thread, and
  sets a hold that makes `Pilot.execute` refuse drive and turn until the person
  says anything else. That refusal matters as much as the halt: the model is
  usually mid-thought when the person speaks, and its next call is often a
  drive it decided on before hearing them.
- **The wake word is matched at the front or the end, not anywhere.** Matching
  it wherever it fell turned "the robot is quite slow", said to someone else,
  into an instruction. The gate also judges an utterance by how much of it was
  above the threshold rather than by its length: with pre-roll in front and the
  silence that ends it behind, 130 ms of door arrived as a 1.3 second
  utterance. Both were caught by tests rather than by use.
- **Always-on listening must not hear the robot.** Its voice is in the same
  room as the microphone, so `Voice.speaking` is asked before any audio is
  kept, and whatever was part-heard when it starts talking is discarded rather
  than stitched across the interruption. Without that it transcribes its own
  narration and obeys it.
- **The microphone is the laptop's.** A Bluetooth speaker's microphone only
  works over the phone-call profile, which drops the output to phone quality
  too; a microphone on the robot hears motors; and it would hear the robot.
  The voice is paused while recording for the same last reason.
- **Conversation shape is tested end to end** in the scratchpad's
  `t_listen.py`, with a scripted model and a scripted person: strict
  alternation, every tool call answered with its results first, a held drive
  refused, and an instruction arriving after a report. The API rejects
  malformed histories, so that is the part worth proving without hardware.

### A speaker on the robot: shelved in favour of Bluetooth

A MAX98357A I2S amplifier wired to the camera module was designed, written and
then **dropped on 2026-09-11**, before it was ever compiled or soldered. The
firmware is back as it was. The voice comes from the laptop's audio output, so
a small Bluetooth speaker fixed to the chassis makes it come from the robot with
no firmware at all. The reason was the soldering: fine work on the camera board,
beside the serial link to the UNO. Recorded so a second attempt starts from
what was learned:

- **Only GPIO13 and GPIO0 are on pads.** The V1.5 board's back carries labelled
  pads: VCC, GND, IO0, **Link** (GPIO13, drives front LED D2, active low),
  TXD0 (GPIO1, the USB debug output), and TXD1 and RXD1, which are the UNO
  link and must never be touched. GPIO 2 and 14 reach only the module's edge.
  16 and 17 are the WROVER's PSRAM, whatever pinout guides say.
- **Not from the board's 3.3 V.** It is a 600 mA AP2112K already feeding the
  radio and camera. Use the car's 5 V.
- **Audio would need I2S1**, because the camera driver uses I2S0. And a
  zero-initialised `i2s_pin_config_t` puts the master clock on GPIO0.
- The schematic is `elegoo-docs/04 Related chip information/
  ESP32-WROVER-Camera-V1.0-Shield.pdf`. It was there all along; reading it first
  would have saved a round of wrong pin advice.
- The full implementation is in git history, commits `627f21f` to `ff2cc1a`.

## Open questions

Answered so far: the heartbeat and reply format on 2026-09-06, then the
ultrasonic scale, the third line sensor and the line sensor polarity in the
calibration run the same day. What is left, in the order it blocks things:

1. **Camera aim.** Mechanical, see Constraints. Nothing model driven is worth
   starting until the camera looks at the room rather than the ceiling.
2. **What 150 actually means.** Whether the cap is the firmware's or the
   sensor's, and whether a genuine no echo is distinguishable from open space.
   If it is not, the safety layer needs corroboration from a second source
   before trusting any far reading.
3. **Whether ultrasonic readings are cached.** Poll at 20 Hz while moving the
   sensor and see whether values lag. Sets the real fast loop rate.
4. **Cliff threshold against real floors.** Channel 2 at 900 works against the
   surfaces tested, but the margin to a dark floor is only 218 counts. Read the
   sensors over every floor the car will actually meet, especially the darkest,
   before phase 04 is signed off.
5. **Camera exposure.** Which `/control` settings tame the metering. The aim
   itself is not a question, it is a mechanical job, see Constraints.
6. **Speed to motion mapping.** What speed value produces what actual velocity,
   and what is the lowest value that reliably moves the car from a standstill.
7. **Turn calibration.** Milliseconds per degree at a given speed, and how much
   it varies with battery voltage and floor surface.
8. **Why command 2 stops acknowledging below about speed 140.** It replies in
   0.66 ms at 165 and not at all within 2.9 s at 100, on the same car in the
   same session. The reply is printed by the main loop when the move timer
   expires, which has nothing to do with speed, so something else is going on.
   Not blocking: the useful range is 150 to 180. `straight_test.py --floor
   --speed 100` tests it without the safety layer on the wire, which is the
   first thing to try.
9. **Battery voltage over the wire.** A3 has the divider but no stock command
   exposes the reading. Adding one is a small UNO firmware change and is worth
   doing early.
10. **Whether the stair edge still reads 1014 from other angles.** Answered
   for one approach on 2026-09-11: a real stair reads almost exactly like thin
   air, so held-in-the-air is a fair proxy. What is not known is whether that
   survives an edge met at a sharp angle, where only part of the array clears
   it. **This is the highest-value unanswered question on the list**, because
   the proposed channel-2-AND-channel-1 rule rests on it and its failure mode
   is the car on the floor below. See phase 09.
11. **Why prompt cache reads stopped at turn 9 of every run.** The head read
   held at exactly 3,875 tokens and then went to zero, at 12 to 13 accumulated
   frames and about 49 seconds, which rules out the five-minute TTL. Both known
   faults are fixed, so this may simply not recur; the test is a run long
   enough to pass both thresholds with the hit rate still high. See phase 07.

## Step 03, done

**Completed 2026-09-07.** The car runs replacement firmware in station mode,
`firmware/elegoo_cam_station/`, built on a current core:

- On the home network at an address the router can reserve, reachable from an
  ordinary machine that also has internet. This ends the two laptop shuffle.
- **Camera working**, `/capture` returning a valid JPEG in about 50 ms.
- `/status`, `/control` and `/stream` all serving.
- Command socket on port 100 with Elegoo's heartbeat semantics. **Verified end
  to end from the development machine** on 2026-09-07: `elegoo.py` connected,
  cleared the stock behaviours, read the ultrasonic, all three line sensors and
  the ground check, held the connection through four heartbeats and closed
  cleanly. The whole stack works from one machine.
- **The Upload / Cam switch must be on Cam**, and the car's own power switch
  on. The ESP32 runs happily on USB alone, so everything above can look healthy
  while the UNO is deaf or unpowered, and the symptom is a command socket that
  accepts a connection and never answers.
- Access point kept as a fallback, so a failed join cannot strand the car.
- `elegoo-cam-stock.bin` is a verified full backup, restored twice.

### The three things that cost a day

All of them came from trusting inference over the manufacturer's own source,
which was in `elegoo-docs` the whole time.

1. **The camera is `CAMERA_MODEL_M5STACK_WIDE`, not `WROVER_KIT`.** Every
   online source says WROVER_KIT. All of them are wrong, and they all trace
   back to one GitHub issue.
2. **The UNO is on Serial2, GPIO 33 and 4, not UART0.** Elegoo's sketch echoes
   that traffic to UART0 for debugging, which made the echo look like the link.
3. **The master clock is 10 MHz**, not the customary 20.

The lesson, written down so it is not relearned: when a manufacturer ships
source, that is ground truth, and anything else is a guess about what the
source says. Get it first. Pin sweeps, disassembly and six candidate pin maps
produced nothing that five minutes with `camera_pins.h` did not settle.

A secondary lesson: a single unreproduced observation is not a finding. One ACK
at sda 14 scl 33 never repeated, and building firmware on it crash looped the
board.

### Operational notes

- **`/capture` returns a STALE frame, and it caused the agent to act on the
  past.** The firmware ran `CAMERA_GRAB_WHEN_EMPTY` with `fb_count = 2`, which
  makes `esp_camera_fb_get` hand back the **oldest queued** frame rather than
  the newest. The driver refills a buffer as soon as one is free, so between
  captures a frame sits in the queue ageing, and the first capture after the
  car moves shows where it used to be.

  Found 2026-09-08. The agent scanned, saw nothing, turned 90 degrees, then
  announced it had found its target and drove at empty floor: the frame it was
  shown had been taken before the turn. It read as a judgement error and was
  not one.

  Fixed in both places. The firmware now uses `CAMERA_GRAB_LATEST`, and
  `Car.capture` drains `CAPTURE_DISCARD` frames first so a car running an older
  flash is still correct. Set `CAPTURE_DISCARD = 0` once the car is reflashed;
  each discarded frame costs about 50 ms.
- **Discard the first frame or two after camera init.** The first capture comes
  out with a heavy green cast; auto white balance settles within about three
  frames.
- Debug output on UART0 over USB is free and does not disturb the UNO.
- The camera sensor is powered through the car's 4 pin cable. With the cable
  detached or the car switched off, the ESP32 runs on USB but the camera is
  dead, which looks exactly like a wrong pin map.

## Alternative platform considered and rejected

There is a second robot to hand, an unbuilt Elegoo Conqueror Robot Tank.
Assessed 2026-09-06 from its box specification and rejected as the project
platform. Recorded so the question does not get reopened from scratch.

| | V4.0, in use | Conqueror tank |
| --- | --- | --- |
| Controller | Elegoo UNO R3 | Elegoo UNO R3, same |
| Camera sensor | OV2640 | OV2640, same |
| Camera module | **ESP32-D0WDQ6**, WROVER class | **ESP32-WROVER** |
| Camera aim | Pan only, fixed tilt | **Two axis gimbal, two SG90** |
| Motor driver | TB6612FNG | DRV8835 |
| Drive | Four wheels, skid steer | Tracks, skid steer |
| Battery | Two 18650 | 7.4 V lithium pack, 2 h |
| Mass | Light | 1640 g, 270 x 230 x 85 mm |

**This comparison was written on a false premise and the chip argument is
void.** It claimed the car had an S3 and the tank a WROVER, and made that the
deciding factor. Both are WROVER class. Note also that flashing turned out not
to need an external serial adapter: the module's own USB C port worked, which
is the opposite of what the research predicted for a WROVER.

What still holds is everything else: phases 00 to 02 are verified on the car
and would have to be redone against an interface the tank has not been shown to
have, the tank is unbuilt, and both platforms skid steer without encoders, so
the hard parts of this project are identical. The conclusion stands, but on
those grounds rather than on the chip.

Switching would also throw away the verified protocol work in phases 00 to 02
and restart it against an interface that has not been confirmed to exist. The
tracks would help on carpet, but both platforms skid steer and neither has
encoders, so the hard parts of this project are unchanged.

**What the tank is good for is its gimbal.** Two degrees of freedom from two
SG90 servos is exactly what the car lacks, and the car's UNO already has an
unused servo channel on D11 that the firmware and `elegoo.py` will drive today.
Fitting a second servo there turns the camera aim from a fixed compromise into
something the model controls. Copy the gimbal's geometry rather than stripping
a sealed kit: an SG90 is a cheap part and the tank is worth more intact.

## Architecture

Latency is the defining constraint. A frame through a cloud model and back is
1 to 4 seconds, dominated by inference. At 200 mm per second the car travels
200 to 800 mm while the model thinks. The model therefore cannot be the only
thing steering.

**One refinement from building it, 2026-09-07.** In the design as implemented
the car is stationary while the model thinks: a bounded move is issued, it
completes, then the next turn begins. So model latency costs throughput and
patience, not safety. That matters when choosing a model: a faster one makes
runs less tedious, it does not make them safer. Safety is entirely the reflex
layer's job and it polls at 10 Hz whatever the model is doing. The 200 to
800 mm figure applies to a design where the model steers continuously, which
this is not.

Two loops at different rates:

- **Fast loop, on the host, 10 to 20 Hz.** Polls the ultrasonic sensor and the
  ground check, answers the heartbeat, and holds veto power over every command
  the model issues. Stops immediately on a close range reading or ground loss.
  Contains no intelligence and never calls the network.
- **Slow loop, the model, under 1 Hz.** Receives a still frame, current sensor
  readings and recent history. Replies with tool calls from a small vocabulary:
  drive a distance, turn an angle, aim the camera, take a range reading, stop,
  report. Every action is short and bounded so a bad decision costs a fraction
  of a second of motion. The outcome of each action, including any veto and its
  reason, is fed back on the next turn.

Keep actions short for a second reason: there are no wheel encoders, so all
motion is open loop and drifts. Re-observing after every short action absorbs
that error in a way that long trajectories cannot.

### Where to aim the camera

Worked from the measured 16 cm lens height. The vertical field of view is not
known, so both plausible values are shown. "Near" is the closest ground the
camera can see; anything nearer is a blind zone. "Horizon" is where the far
distance falls in the frame, measured from the top.

| Tilt below horizontal | Near, V=40 | Horizon | Near, V=50 | Horizon |
| --- | --- | --- | --- | --- |
| 0, level | 44 cm | 50% | 34 cm | 50% |
| 5 | 34 cm | 38% | 28 cm | 40% |
| 10 | 28 cm | 25% | 23 cm | 30% |
| 15 | 23 cm | 12% | 19 cm | 20% |
| 20 | 19 cm | 0% | 16 cm | 10% |

**Aim for 10 to 15 degrees down.** Level is wrong: it wastes half the frame on
things above eye height and leaves a blind zone of 34 to 44 cm, which is longer
than the car. Past 20 degrees the horizon clips off the top and the model loses
the distant context it needs to pick a target to drive towards.

The blind zone is not a hole in the coverage, because the ultrasonic reads
happily from 10 cm and the two share a bracket. Camera from about 20 cm out,
ultrasonic inside that, and they always point the same way.

These numbers assume the lens is where the geometry says it is. Confirm by
putting a mark on the floor and finding the nearest distance that is actually
visible, then compare against the table to work out which field of view this
lens really has.

## Constraints to design around

- **No odometry.** No wheel encoders. Dead reckoning only, via timed moves and
  the inertial measurement unit. Tasks needing a remembered map of a space are
  fighting the hardware.
- **One monocular camera.** No depth. Give the model the ultrasonic reading in
  every prompt rather than expecting it to judge distance from the image. The
  height is better than the research assumed, see the confirmed section, so the
  usual complaint about models reasoning poorly from a floor level wide angle
  view is less of a problem here than expected. The aim is the problem.
- **Camera aim is solved.** It was the blocker: the first capture was mostly
  ceiling with the light blown out. A tilt servo on D11 fixed it, and the aim
  is now something the model controls rather than a fixed compromise.
- **Exposure is metered on the brightest thing in frame.** With the lens
  pointed at a ceiling light, that light wins and everything at car height is
  crushed to black. Worth attacking through `/control` regardless of the aim,
  since a room with a window will do the same thing. This is now more important
  than it would be otherwise, because reframing away from the light is not an
  option.
- **One distance ray.** The ultrasonic sensor sees only where the servo points.
- **Battery. Two 18650 cells, and nothing else.** A 9 V PP3 cannot supply the
  motors and produces an uncontrollable car, see the confirmed section.
  Behaviour becomes erratic below 7.00 V. An agent loop is worse
  than manual driving for this because the camera streams while the model
  thinks. Expect degraded motion well before the car stops moving.
- **Network topology.** Stock firmware is access point only, so a laptop joined
  to the car has no internet route and cannot reach a model. See below.

## Plan

- [x] **00** Confirm the platform. Address, video, still capture. Done.
- [x] **01** Prove the command socket. Done 2026-09-06. Heartbeat resolved,
      reply format captured. Calibration questions remain, see above.
- [x] **02** Client library. `elegoo.py`, run against the car 2026-09-06 via
      `smoke_test.py`: sensors, servo sweep, capture, all four directions and
      the heartbeat hold, all clean. Calibration constants folded in.
- [x] **02b** Phase 01 leftovers. Done 2026-09-06. Cliff detection found on
      line sensor channel 2, ultrasonic scale fitted over seven points, tilt
      servo fitted and commissioned. Camera aim parked as good enough: the
      firmware clamp keeps it near level rather than looking down, and
      re-seating the horn is deferred rather than abandoned.
      Still outstanding from this phase, folded into 04: reading the line
      sensors over the actual floors to confirm the cliff threshold.
- [x] **03** Network fix. **Done 2026-09-07**, camera and network both. The car is on the
      home network at an address the router can reserve, reachable from an
      ordinary machine, with the command socket and HTTP both working, the
      access point kept as a fallback, and a verified full flash backup of the
      stock firmware. **Camera half unsolved** under the replacement firmware,
      see the open problem section above; restoring the stock image brings the
      camera back at any time. Do not start phase 05 without a camera.
      Original notes follow.
      Patch the camera sketch to station mode so the car
      joins the home network and the host keeps its internet route. Back up the
      whole 4 MB flash first, and prove the restore works.
      Board settings, now determined rather than guessed: **ESP32 Wrover
      Module**, 4 MB flash, **Huge APP (3MB No OTA/1MB SPIFFS)**, PSRAM
      enabled, which the Wrover Module board does by default and the WROVER
      module has. Upload at 921600 on COM4 works. Use an **arduino-esp32
      1.0.x** core to match the stock build, not a current one.
      Get Elegoo's camera source for the **WROVER** board, not the S3.
      **The network must not isolate clients.** Proved the hard way on
      2026-09-07: on a guest SSID, 192.168.200.0/24, a laptop and a
      phone on the same subnet could not see each other at all. An ARP sweep
      found neither, and ping reported destination host unreachable from the
      sending machine, meaning it could not even resolve the MAC. Station mode
      is useless on such a network however well the car joins. Test any
      candidate network by pinging one device on it from another **before**
      flashing credentials into the firmware.
      Do this **before** the model loop, not after: debugging an agent loop
      with no internet on the host is miserable.
### What the safety layer allows, and why 15 cm

`stop_cm` is **15**, lowered from 25 on 2026-09-07 after the first agent run
refused to approach anything nearer than about 40 cm, which makes a task that
ends at an object impossible. Reaction distance is roughly 10 cm: about 7 cm
travelled between polls at 8 Hz and 0.55 mm/ms, plus about 3 cm of command
round trip. So 15 leaves 5 cm of margin, which is thin.

**What makes it safe is the clamp, not the threshold.** A forward move is
shortened before it starts to the clear space measured just before it, so the
car cannot ask for 800 ms, cover 44 cm, and cross the entire gap between two
polls. The poll thread is left to catch what appears mid-move rather than being
the only line of defence. `clamp_move_to_clearance` controls it.

A move that clamps below 120 ms is **refused**, not shortened. The first
version clamped up to a 120 ms floor on the reasoning that a twitch beat a
refusal; with 0.6 cm of usable space that floor drove 7 cm, straight through
the stop distance. A minimum that overrides the limit is a hole in the limit.

The model is told when a move was shortened and by how much, because a silently
truncated action makes its own dead reckoning wrong without it ever knowing.

- [x] **04** Reactive safety layer. **Gate met 2026-09-07.**
      `safety_test.py --move` passed 17 of 17 against the car on the floor: it
      refuses to drive forward into an obstacle, refuses to drive over a table
      edge, allows reversing away from both, refuses on stale readings and
      recovers when they resume, and rejects excessive speeds and durations.
      Two things remain untested rather than unbuilt, neither blocking:
      - **The interruption path.** Every refusal so far happened before the
        move started. The poll thread cutting a move short mid-flight has not
        been exercised on hardware, only against the mock. To try it, put an
        obstacle in the way during an 800 ms move.
      - **Dark floors.** Channel 2 reads 170 on this floor against a threshold
        of 900 and 1017 over an edge, which is generous. A dark rug will read
        higher. Check any new surface before driving on it.
- [x] **05** Model loop. **Ran against the car 2026-09-07 and works.** Claude
      Sonnet 5, adaptive thinking, effort low by default. Manual loop rather than
      the SDK's tool runner, for pacing, a turn budget, a host owned stop, and
      the per-turn logging. Vocabulary: drive, turn, look, scan, stop, report,
      in durations rather than distances, because that is what the robot
      accepts and a `distance_cm` parameter would be a fiction.
      Everything below came from watching the first four runs, and every one of
      them was a fault in the prompt or the harness rather than in the model:
      - **Every action returns a fresh frame.** Only look and scan did, so the
        model spent alternate turns calling look purely to see what its last
        move had done. Half the budget.
      - **Scan first.** It opened by driving on the strength of whichever
        single frame the run happened to start with.
      - **Pan offset is bearing error.** The head pans independently, so the
        model found its target by panning, then reported success from a
        sideways glance with the car facing elsewhere. Every look and scan now
        states the offset from centre in degrees and the turn in milliseconds
        that would correct it.
      - **Searching and aiming are different turns.** Coarse 90 degree steps to
        survey a new quarter of the room; small computed turns to aim at
        something already visible.
      - **No speed knob on turns.** The model was choosing 100, which this car
        does not turn reliably at, and which silently invalidated the
        degrees-per-millisecond figure it had been given.
      - **Tilt defaults were wrong for the current mount**, and the model
        drove the tilt to its minimum trying to compensate.
- [x] **06** Logging and replay. Folded into `agent.py` rather than left until
      after, because the runs worth learning from are the early ones where the
      prompt is worst. Each run writes `runs/<timestamp>/` with `run.json`, the
      task, system prompt and tool definitions, `turns.jsonl`, one line per
      turn with the tool call, the result, the sensors, latency and token
      usage, and `frames/` holding every image the model was shown.


Phases 00 to 06 are done. What is left, roughly in order of value:

- **07 Cost. Prompt caching was broken and it cost about four times.** Found
  2026-09-11 from the Anthropic dashboard: 8 September's Sonnet runs billed
  about $2, against an estimate of $0.46 made from the logs. The logs were
  wrong because they recorded `cache_read_input_tokens` and not
  `cache_creation_input_tokens`, so the expensive half was invisible.

  Reading `runs/20260908-*/turns.jsonl`, the reads sit at a constant 3,875 for
  the first eight turns of every run and then go to zero for all the rest.
  Two faults, and they compound:

  1. **`prune_images` rewrote already-sent history.** Caching is a prefix
     match, so replacing an older photograph with a placeholder invalidates
     everything from that point on, every turn.
  2. **The only breakpoint was the automatic one at the end.** Nothing marked
     the system prompt and tool definitions, which never change, so the only
     thing ever read back was that head, and nothing else could be.

  After that every turn wrote the whole growing prompt at 1.25x and read none
  of it back.

  **Correction, 2026-09-11, before any of this was tested.** The first version
  of this entry said the head expired at the five-minute cache TTL, "about ten
  turns, which is exactly where the reads stop". The arithmetic does not
  support it. Turns take a median of **4.6 seconds**, so turn 9 of run
  `20260908-215355` is **49 seconds** in, not five minutes. Five minutes is
  about 65 turns, and no run has ever been that long.

  So fault 1 is established: reads sat at a constant 3,875, exactly the size of
  the head, which is what "history is rewritten every turn" looks like. **Why
  even the head stops matching at turn 9 is NOT known**, and is now an open
  question rather than a cause. What is known is that it happens at 12 to 13
  accumulated frames rather than at any particular elapsed time.

  That mistake is the one this file keeps recording: a mechanism that fitted
  the shape of the data, stated as a finding without checking the one number
  that would have falsified it.

  Fixed: an explicit breakpoint on the system prompt, and `--keep-frames` now
  defaults to **0**, off. Keeping the frames is far cheaper than the cache
  invalidation that removing them causes, because a QVGA frame is about 100
  tokens and a cached one is 10. Prune only to protect the context window.

  Every run now prints reads against writes and says so when the share is low.
  **A cache hit rate is a number to watch, not to assume**, and an estimate
  built from a log that only records the cheap half will always look fine.

  Earlier, and still true: frames default to QVGA, which cut image tokens on a
  twenty turn run by about 93%. `--framesize` tunes it.

  **`tokens.py` is the accounting, and `--reconcile` is the part that matters.**
  It prices the runs per turn, per run and per UTC day, and then checks that
  total against Anthropic's own CSV export from the console. Against the
  2026-08-14 to 2026-09-12 export, four of the five billed days now agree to
  the token on every field the logs record. Getting there found two more
  logging faults that no amount of reading the code had:

  1. **Usage belongs to the turn, not the tool call.** The model can ask for
     two tools in one response, and the loop wrote that response's usage onto
     each record. 21,367 cache reads and 906 output tokens counted twice,
     across six turns.
  2. **Three runs logged nothing at all and were billed anyway.** A crash
     between the first API call and the first turn record leaves a `run.json`
     and no `turns.jsonl`. They are now named in the report rather than
     skipped.

  Both were found by the difference against the bill being non-zero, and
  neither would have been found any other way. **The traps that make this
  worth automating**: the export is UTC and the run directories are local, so
  an evening run here lands on the next day's bill; a missing field is not a
  zero and is printed as a floor with `>`; and the price table is the one
  input that cannot be derived from the logs, so `--reconcile` is also what
  catches it going stale.
- [x] **08 Model comparison. Done 2026-09-08: Sonnet 5 at low effort is the
  default.** It scans first, computes its turns from the head's pan offset,
  corrects an overshoot and knows when it has arrived, at about a third of the
  cost of Opus. Opus remains a `--model` away for anything needing finer
  judgement. `run.json` records the model and the exact prompt, so a comparison
  is traceable rather than an impression.
- **09 Dark floors. NOW OBSERVED, not just predicted.** On 2026-09-11 an
  exploration run had a turn stopped dead by "no floor under the front
  sensors" with **122 cm of clear air in front of it**, on a patterned rug.
  The range finder was not involved: this was the cliff stop firing on a
  surface, which is exactly the false positive this item was opened for.

  It read as the robot being timid about obstacles and it was nothing of the
  kind. Worth remembering when judging behaviour: the model backed away
  sensibly from what it was told, and what it was told was wrong.

  **Nothing recorded how close to 900 the rug was**, because only the verdict
  was logged and not the reading. Fixed: `turns.jsonl` now carries a `raw`
  block with `cliff_raw`, `range_raw` and the threshold alongside the prose the
  model sees. A verdict without its number cannot be argued with afterwards,
  which is the same lesson the cache writes taught in phase 07.

  `floor_test.py` reads all three channels over each real surface and says
  whether the threshold survives. Run it on that rug before trusting the cliff
  stop anywhere near the staircase the same run photographed.

  **The answer is probably not "raise the threshold."** Margin at the top is
  only about 120 counts, and the two failures are not symmetric: a false stop
  on a rug is an annoyance, a missed edge puts the car down the stairs.

  **Measured with `floor_test.py` on 2026-09-11, and the rug is INNOCENT at
  rest.** Twelve samples per channel per surface:

  | surface | ch0 | ch1 | ch2 (cliff) |
  | --- | --- | --- | --- |
  | Patterned rug, one spot | 862 | 585 | **732** |
  | Brown hardwood | 381 | 42 | 41 |
  | Held in the air | 1017 | 733 | 1021 |

  Spread within each surface was 2 counts or less, so these are solid
  readings, and the air figures match the 2026-09-06 measurements to within a
  count. **Channel 2 on that rug reads 732, which is 168 below the threshold**,
  and the static survey passes.

  **So the static reading does not explain the stop, and the dark-floor
  hypothesis is NOT confirmed.** Something made the reading climb 168 counts,
  58% of the entire rug-to-air span, during a turn. Two candidates, and the
  cheap one first:

  1. **The rug is patterned and only one spot was measured.** A darker part of
     the pattern could sit far higher. Measure several spots before anything
     else; it needs no motors.
  2. **The reading while turning is not the reading at rest.** A tank turn
     drives one side and holds the other, so the car pitches and rolls, and a
     sensor lifted off a deep pile reads higher. This is the harder one to
     measure and the more likely one to be true.

  The next trip will answer it regardless, because `cliff_raw` is logged now.

  **Then the patterned rug was measured spot by spot, and hypothesis 1 was
  right.** Same day, `floor_test.py`:

  | surface | ch0 | ch1 | ch2 (cliff) |
  | --- | --- | --- | --- |
  | Brown hardwood | 381 | 42 | 41 |
  | Rug, bright part | 641 | 45 | 230 |
  | Rug, plain part | 862 | 585 | 732 |
  | **Rug, dark part** | **990** | **473** | **953** |
  | Rug to hardwood edge | 396 | 81 | 46 |
  | Held in the air | 1016 | 732 | 1020 |

  **A dark patch of this rug reads 953 against no floor at all at 1020. Sixty
  seven counts.** That is 6% of the scale. The turn-2 stop is explained: the
  car was over a dark part of the pattern and the cliff stop was, by its own
  rule, correct.

  **No threshold on channel 2 alone can separate them**, and that is the real
  finding. 900 false-stops on the dark patch; anything above 953 leaves under
  67 counts to catch a real edge. This is not a tuning problem.

  **Correction to the entry above, which was wrong.** It said channels 0 and 1
  are unusable and "voting across channels is therefore not available as a
  fix". That was concluded from the plain part of the rug, before the dark part
  was measured, and the dark part reverses it. On the patch where channel 2
  fails, **channel 1 separates cleanly: 473 against 732, a 259 count gap**. Its
  worst floor reading anywhere is the plain rug at 585, still 147 below air.

  So a rule of "channel 2 high AND channel 1 high" would hold on every surface
  measured so far. **It is deliberately NOT implemented**, for two reasons that
  matter more than the elegance:

  1. **An AND rule can only ever make the stop less likely to fire**, and the
     two failure directions are not symmetric. It trades a nuisance for the
     catastrophic one.
  2. **A real edge has never been measured.** Every no-floor number in this
     file, today's included, is the car *held in the air*. A stair edge with a
     tread 20 cm below may return far more signal than empty space, and the
     whole AND rule rests on channel 1 reading high there. Nothing yet says
     it does.

  It would also cost serial budget: distance plus two line channels at 8 Hz is
  792 B/s, **82% of the 960 B/s link**, against the 55% the current poll uses.
  6 Hz brings it back to 62%.

  **The real staircase was then measured, and it reads like thin air.**
  2026-09-11, car held by hand with its front overhanging an actual stair:

  | surface | ch0 | ch1 | ch2 (cliff) | |
  | --- | --- | --- | --- | --- |
  | Brown hardwood | 381 | 42 | 41 | floor |
  | Rug, bright part | 641 | 45 | 230 | floor |
  | Rug, plain part | 862 | 585 | 732 | floor |
  | Rug, dark part | 990 | 473 | **953** | floor |
  | **Real stair edge** | 1012 | **715** | **1014** | drop |
  | Held in the air | 1016 | 732 | 1022 | drop |

  So the worry that a real edge would return more signal than empty space was
  wrong: a stair reads 1014 against air's 1022. **Held in the air is a fair
  proxy after all**, which retires that concern and validates every no-floor
  figure taken since 2026-09-06.

  **Channel 2 alone is finished as a cliff detector.** Dark rug 953, stair
  1014: 61 counts. A threshold could be squeezed in at 983, giving 30 counts
  either way, which is not a margin on a sensor whose readings move with
  battery, wear and approach angle.

  **The AND rule now has evidence.** On the one surface that fools channel 2,
  channel 1 separates cleanly:

  | | ch1 |
  | --- | --- |
  | Rug, dark part (a floor) | 473 |
  | Real stair edge (a drop) | 715 |
  | Held in the air (a drop) | 732 |

  A rule of **channel 2 above 900 AND channel 1 above 594** holds on every
  surface measured, with 121 counts of margin each way. **Still not
  implemented**, and the reasons have narrowed to two:

  1. **One edge, one angle, one position.** An edge met at a sharp angle may
     only reach part of the array. Measure the same stair from several
     approaches, and a table edge too, before touching the safety layer.
  2. **The plain rug reads 585 on channel 1**, only 9 below that line. It does
     not matter today, because its channel 2 is 732 and the rule never
     consults channel 1. It would matter the moment a surface is both dark
     enough to fool channel 2 and plain enough to read near 585.

  Serial budget is the other cost: distance plus two line channels at 8 Hz is
  792 B/s, **82% of the link**, against 55% now. 6 Hz brings it to 62%.

  Meanwhile, keep the car off the dark rug rather than widening the rule that
  protects it.
- **10 Battery over the wire.** A3 has the divider and no stock command exposes
  it. Five lines of UNO firmware, `analogRead(A3) * 0.0375 * 1.08`. Worth
  bundling with widening the servo tilt clamp, since both need the same flash.
- **Housekeeping.** A DHCP reservation for MAC `94:B5:55:14:B1:BC` so the
  address stops moving, and the wifi credentials out of the `.ino` and into a
  git-ignored `secrets.h`.

## Choosing a model for the loop

Worked through on 2026-09-07 before the first run.

**Cost is not the deciding factor at this scale.** A twenty turn run is roughly
170k input tokens with the images accumulating. That is about $0.85 on Opus 5,
$0.34 on Sonnet 5, $0.17 on Haiku 4.5. Choosing a model to save seventy pence a
run is optimising the wrong thing.

**Latency is not a safety factor either**, see the note under Architecture.

**What the task actually demands is coarse**: recognise a brightly coloured
object in a dim, wide angle, low resolution frame, judge whether it is left or
right of centre, and pick one of six tools with small integer arguments. Sonnet
5 would very likely do it.

**Where a stronger model earns its place** is the awkward cases: two similar
objects in frame, a target that disappears behind furniture, noticing after
four turns that the strategy is not working, and not looping between turn left
and turn right.

**Settled 2026-09-08: Sonnet 5 at low effort, and it is the default.** The
plan was to start on Opus and step down, because a weaker model failing is hard
to tell apart from a bad prompt. That was the right order, and the step down
went through cleanly: Sonnet drives this robot well, at about a third of the
cost.

**Worth recording, because it kept being the answer.** Every failure blamed on
the model turned out to be the harness: camera frames that were a move out of
date, a scan that returned no photographs of the sides, a turn direction
mirrored by crossed parameter names in Elegoo's firmware, and narration the API
was never obliged to produce. Four rounds of "the model is being stupid" were
four harness bugs. Be slow to conclude the model is the weak part.

## Conventions

- **Power the car up on a flat surface and leave it alone for ten seconds.**
  The UNO calibrates the gyro's zero offset at boot, and handling the car while
  it does corrupts that zero. The correction loop then drifts until one side
  saturates at 180 and the other floors at 10, and the car turns on the spot
  instead of driving forward. Cost a diagnosis on 2026-09-07. Nothing reports
  this, and the only symptom is a car that will not drive straight.
- Put the car on a box so the wheels are clear whenever testing movement.
- **A box hides steering faults.** Free wheels at 180 and at 10 both look like
  "the wheels turned", so the spin above was invisible until the car was on the
  floor. Use a box to prove a stop, and the floor to prove a direction.
- Send `{"N":110}` early in a session to clear the stock behaviours, otherwise
  obstacle avoidance competes for the motors.
- **Use `Car.halt()` as the stop.** It is `{"N":1,"D1":0,"D2":0,"D3":1}`,
  command 1 with both motors at speed 0, and it is verified directly on this
  car in every condition tested.
- **Command 100 also stops the motors on this unit, but do not rely on it.**
  Observed twice with `stop_test.py`, 2026-09-07, the second time with the
  script's own confounder removed. Elegoo's published source says it should
  not: it sets a functional mode, prints `{ok}`, and no mode handler's "not my
  mode" branch touches the motors. That is now the **third** disagreement
  between that source and this car, after the undocumented servo clamp and
  command 1 not acknowledging, so the published source is not the firmware
  here. Prefer `halt()`, whose mechanism is visible and whose behaviour does
  not depend on which build is loaded.
- **An earlier entry here said flatly that command 100 does not stop the
  motors. That was wrong**, and worth recording because of how it went wrong.
  It came from reading the published source rather than from testing, and the
  runaway that appeared to confirm it was the battery. A source-derived claim
  was promoted to a confirmed finding by a coincidence. Original text follows,
  for the mechanism, which is still worth knowing about the published build:
  command 100
  only sets `Functional_Mode = CMD_ClearAllFunctions_Standby_mode` and prints
  `{ok}`; it never touches the motors, and every mode handler's "not my mode"
  branch just clears a flag, so the last PWM written keeps being written.
  Command 2 hid this for months because its own timer calls `stop_it`. The
  moment straight motion moved to command 1, which has no timer, nothing was
  stopping the car and it drove into an obstacle. Use `Car.halt()`; `Car.stop()`
  calls it first and then sends 100 as housekeeping.
- **`Car.halt()` does stop the car after a forward drive.** `wedge_probe.py`,
  2026-09-07: spun the wheels with command 1 forward at speed 165, polled the
  range finder ten times over four seconds, then halted. Every poll answered
  and the wheels stopped. The UNO also answered all ten polls during a turn and
  all five at rest, so **it is not wedged by motion**.
- **`stop_probe.py` contradicts that, reproducibly.** It failed identically on
  a second run after a power cycle, so it is not stale state; the two scripts
  genuinely disagree. Three things differ between them: drive time before the
  stop, 1 s against 4 s; the stop being one frame against three; and whether
  anything else was on the wire in between. `stop_diff.py` varies those one at
  a time, and also asks the two questions `stop_probe.py` never did, namely
  whether the UNO still answers at the moment the stop fails, and whether the
  wheels are driven or merely coasting. The stop is a coast, not a brake:
  `DeviceDriverSet_Motor_control` writes `direction_void` with speed 0, and
  unloaded wheels on a box run down slowly, so a coast read as a failure would
  explain the whole contradiction. Details of the original failure:
  command 1 at speed 0, command 1 with direction 0, command 4 both sides 0,
  command 3 direction 9, command 2 direction 9, command 110 and command 100,
  all seven, across four command numbers and three functional modes. The same
  frames worked in `wedge_probe.py`. Only one of these runs describes the car.
  The most likely difference is state: `stop_test.py` had just crashed with the
  wheels turning, so `stop_probe.py` may have run against a UNO already in a
  bad state that its own closing power cycle then cleared. **Unproven.** Until
  it reproduces one way or the other, treat the stop as working but not
  trusted, and keep the car on a box.
- **THE CAR WAS RUNNING ON A 9 V PP3 BATTERY.** Established 2026-09-07, after
  a day of chasing a stop that would not work. The V4.0 expects two 18650
  lithium cells, which deliver amps. A PP3 has an internal resistance of a
  couple of ohms fresh and far more as it drains, and four motors starting draw
  well over an amp, so the terminal voltage collapses the moment they engage.
  The multimeter read 8.x V, but that is open circuit and says nothing about
  the pack under load; a fresh PP3 reads about 9.5 V, so 8.x is already part
  used.

  **This explains every confusing result of the day, including the ones that
  contradicted each other.** The rail sags, the ESP32's radio is the hungriest
  thing on the car and goes first, so commands stop arriving; the UNO stays
  powered enough to keep writing the last pulse width, so the wheels keep
  turning. From the host that is indistinguishable from a wedged controller,
  and from the car it is a runaway.

  It also explains the irreproducibility. **Alkaline cells recover their
  voltage when rested.** `wedge_probe.py` ran after a pause and passed;
  `stop_probe.py` ran straight after other motor tests and failed, twice, with
  the same frames. The hidden variable was never drive time or frame count or
  traffic on the wire, it was how long the battery had been resting.

  **Consequences.** Nothing measured on this supply is evidence about the car.
  In particular these entries were all taken on it and are void: "command 2
  forward produces no motion and no reply", the whole IMU-is-broken diagnosis
  built on top of it, "two servos plus four motors did not brown out the 5 V
  rail", and every stop-path result from 2026-09-07. Re-measure on 18650s
  before believing any of it. **No motor testing until the car has proper
  cells.**
- **With 18650 cells the stop works, in every case tested.** `stop_diff.py`,
  2026-09-07, all four cells stopped the car: one frame and three frames, after
  one second of driving and after four, on a quiet wire and a busy one, with
  the UNO answering three of three polls each time. `Car.halt()` is therefore
  one frame, `{"N":1,"D1":0,"D2":0,"D3":1}`, command 1 at speed 0. There was
  never anything wrong with it. Resting pack voltage at the time: 7.3 V.
- **In the failing case the host stops hearing anything at all.** `stop_diff.py`
  cell A, 2026-09-07: drive 1 s with a quiet wire, three-frame stop, wheels
  still driving under power, and **zero of three follow-up sensor polls came
  back**. So the failure is not the car ignoring a stop frame. Something
  between the host and the motors stops responding, and the car runs on the
  last pulse width because nothing is left to change it.
- **"Silent" was ambiguous in that run and the ambiguity matters.** The probe
  reported a reply timeout and a dead TCP socket as the same thing. They are
  different faults: a wedged UNO, against a radio that has gone away while the
  car keeps driving. The motors starting is the largest current step the car
  makes, so a sagging pack browning out the ESP32 fits the evidence as well as
  anything on the UNO does, and it would explain why polling through the drive
  seemed to help: those runs simply had a healthier pack. `stop_diff.py` now
  separates the two and prints which.
- **The I2C hang hypothesis is dead.** The idea was that the gyro read blocks
  AVR `Wire` for ever, since Elegoo never call `Wire.setWireTimeout`. It fitted
  the evidence well and predicted the UNO would go silent while driving. It did
  not: ten of ten polls answered mid-drive. Recorded because the reasoning was
  sound and it should not be re-derived.
- **Which frame does stop it is not yet known**, and the published source is no
  longer trusted on the point. That source says command 1 with `D3=0` calls
  `stop_it`, which writes zero to both motors with no IMU in the path; on this
  car, 2026-09-07, it did not stop the wheels. In the same session command 1
  never acknowledged either, while 100 and 110 both replied normally. Together
  with the undocumented servo clamp, that is two independent disagreements:
  **the UNO here is running something other than the source in `elegoo-docs`.**
  `Car.halt()` therefore fires three zero-speed frames rather than one, and
  `stop_probe.py` exists to find out which is doing the work.
- **Command 1 is not acknowledged on this car.** A forward frame timed out at
  1.5 s while the wheels were visibly turning. `motor_direct` no longer waits,
  so `move` and `drive_straight` return None for forward and backward. Waiting
  bought nothing and cost everything: the timeout raised out of the caller with
  the motors already running, which is how the stop got skipped.
- Anything that starts the motors owns the stop, and the stop belongs in a
  `finally`, not on the happy path.
- Keep the car address in configuration, not hardcoded. It changes once, at
  step 03.
- After step 03, give the car a DHCP reservation on the router so the address
  stops moving.

## Files

Not all of these are in the git repository. `elegoo.py`, `safety.py`,
`agent.py`, the firmware, and the three verification and calibration scripts
are; the one-off diagnostics, the raw session logs, the stock firmware backup
and Elegoo's download bundle are kept locally, for the reasons in the README.
The findings from all of them are in this file, which is the point.


- `elegoo.py` - the client library. Standard library only. Socket wrapper with
  the heartbeat echo on its own thread, tagged commands with reply matching,
  typed methods for the whole command set, sensor parsers and the still frame
  grabber. Use it as a context manager: it stops the car on the way out of an
  exception as well as a clean return. Holds no policy, makes no decisions.
- `elegoo_probe.py` - the probe. Standard library only. Opens the command
  socket, timestamps traffic both ways, answers the heartbeat, and gives an
  interactive prompt. `--auto` runs every heartbeat strategy and captures the
  sensor replies unattended, `--calibrate` walks the remaining open questions
  with the car in hand, `--log` tees everything to a file so a run on the
  machine joined to the car can be carried back.
- `run.txt` - the phase 01 run, 2026-09-06. Source for everything in the reply
  format section above.
- `firmware/elegoo_cam_station/elegoo_cam_station.ino` - replacement camera
  firmware for step 03. Station mode with the access point kept as a fallback,
  plus `/capture`, `/stream`, `/control`, `/status` and the port 100 bridge
  with its heartbeat. Written fresh rather than patched from Elegoo's sketch,
  because the module's job is small, fully characterised, and this way it
  builds on a current core.
- `elegoo-cam-stock.bin` - full 4 MB backup of the stock camera firmware,
  `sha256 06e3e934...`. Restore with
  `esptool --port COM4 write-flash 0 elegoo-cam-stock.bin`. Verified by
  restoring it and re-running the smoke test.
- `safety.py` - the phase 04 reactive safety layer. Polls the sensors on its
  own thread, holds veto power over every action, and stops the car itself. No
  intelligence, no network. Every verdict carries a reason, refusals included,
  because the model needs the reason fed back on the next turn.
- `turn_test.py` - measures four ways of changing heading and reports degrees
  per burst for each. Found the tank turn, and found that command 2's Left and
  Right no longer rotate this car at all.
- `motor_map_test.py` - drives each direction with the car held up and asks
  which way each side's wheels actually turned.
- `stop_test.py` - proves the car can be stopped, on the box, before it is
  allowed on a floor again. Four routes: `halt()`, `stop()`, bare command 100
  (expected to fail), and `drive_straight`. Run this after any change to the
  motion or stop path.
- `straight_test.py` - asks whether command 2 drives straight now the car has
  real cells. If it does, `straight_via_direct_motor` goes off and the firmware
  owns the move timer again, which is safer than the host owning it.
- `stop_diff.py` - resolves the disagreement between `stop_probe.py` and
  `wedge_probe.py` by varying drive time, frame count and wire traffic one at a
  time across four cells, polling the UNO for liveness right after each stop,
  and distinguishing driven wheels from coasting ones.
- `wedge_probe.py` - asks whether the UNO is still answering while the wheels
  turn, in three phases: still, turning, driving. The question that matters
  after `stop_probe.py` found no stop at all, because silence while driving
  means no stop frame can ever work.
- `motor_map_test.py` - drives each direction with the car held up and asks
  which way each side's wheels actually turned. Four rows that between them
  say whether a motor channel's direction is stuck, a channel is dead, or the
  motors are fine and the fault is in the command path.
- `stop_probe.py` - finds a frame that actually stops this car, by trying
  seven candidates one at a time and asking you which worked. Written after
  `stop_test.py` proved the assumed answer wrong. Rescues with every candidate
  on a failure, and the car's own power switch is the backstop.
- `safety_test.py` - proves the phase 04 gate: commands the car at a wall and
  at a table edge and checks that it refuses, printing pass or fail per case.
  `--move` includes the tests that actually drive the motors.
- `capture_serial.py` - reads a serial port to screen and to a file at once,
  pulsing the board's reset line first so a sketch that prints once in setup()
  is caught. Scrollback ate two probe runs before this existed.
- `elegoo-docs/` - Elegoo's official download bundle. The camera source is in
  `02 .../04 Code of Carmer (ESP32)/`, and `camera_pins.h` there is the ground
  truth that a day of guessing failed to reach.
- `agent.py` - the phase 05 model loop, with phase 06 logging built in.
  The model gets a frame, the sensors and the last outcome; replies with
  one tool call; the host executes it through the safety layer and hands back
  what happened plus a fresh frame.
- `tokens.py` - what the runs cost, and whether that agrees with the bill.
  Prices `runs/*/turns.jsonl` per turn, per run and per UTC day, and
  `--reconcile` checks the total against Anthropic's CSV export. The
  reconciliation is the point: it has already found a caching fault, a double
  count and three unlogged runs, none of which the logs could report on their
  own. Export the CSV from the console under Usage; it is git-ignored.
- `floor_test.py` - reads all three line sensors over each floor the car will
  actually meet, and checks `CLIFF_THRESHOLD` against them. Reports the worst
  single sample rather than the median, because the cliff stop fires on one
  poll. Written after a patterned rug stopped a turn with 122 cm of clear air
  ahead.
- `demo_look_around.py` - the robot looks around, drives a little and retraces
  its way back. Exercises every subsystem in one run.
- `feasibility-report.html` - background research and reasoning.

## References

- Official UNO firmware, pin map and command examples:
  https://github.com/elegooofficial/ELEGOO-Smart-Robot-Car-Kit-V4.0-New
- Community protocol reference, derived from the ESP32-S3 sources, so it matches
  this unit:
  https://github.com/ekulkisnek/elegoo-car-custom-tools/blob/main/docs/protocol-reference-2026-03-24/PROTOCOL_REFERENCE.md
- Python client that patches the firmware for station mode:
  https://pypi.org/project/elegoo-robot-car4/
- ESP32-S3 to UNO serial wiring and the Upload / Cam switch:
  https://forum.arduino.cc/t/serial-info-from-esp32-s3-camera-to-arduino-uno-elegoo-kit/1452804
- Elegoo guide to flashing the camera module:
  https://us.elegoo.com/blogs/learn/elegoo-smart-robot-car-v4-0-with-camera-upload-code-to-the-camera-module
