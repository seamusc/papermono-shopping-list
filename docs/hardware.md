# PaperMono hardware notes

What this firmware has learned about driving the
[M5Stack PaperMono](https://docs.m5stack.com/en/core/PaperMono), written for
anyone building their own firmware for it. Everything here is taken from
`firmware/src/hal/` and `firmware/platformio.ini`; where a value is a choice
rather than a hardware fact, it says so.

Tested on the **C153** only. See [firmware.md](firmware.md#hardware) for the
variants.

## At a glance

| Part | What it is | How the firmware reaches it |
|------|------------|-----------------------------|
| SoC | ESP32-S3R8: 16 MB flash, 8 MB octal PSRAM | Arduino framework via PlatformIO |
| Display | 3.97" 480×800 e-paper, SSD1677 controller | SPI, through M5GFX (`M5.Display`) |
| Touch | FT6336G capacitive, single controller | I²C `0x38`, through `M5.Touch` |
| Power | M5PM1 power-management IC | I²C `0x6E`, `M5PM1` library |
| IO expander | M5IOE1 | I²C `0x4F`, `M5IOE1` library |
| Charger | IP2315 | Kept **off** the I²C bus (see below) |
| Buttons | Two side keys, plus the power button on the PM1 | GPIO 2 and 3; power button through the PM1 |
| LED | Status LED: red from the PM1, green and blue from IOE1 PWM | See [LED](#status-led) |
| Frontlight | Adjustable | `M5.Display.setBrightness()` |
| Not used here | LoRa radio, NFC, RTC, buzzer | Left alone, except the buzzer pin, which is held low |

## Pin map

ESP32-S3 GPIOs (`firmware/src/hal/bsp.h`):

| Signal | GPIO | Notes |
|--------|------|-------|
| System I²C SDA | 47 | Shared by PM1, IOE1 and touch |
| System I²C SCL | 48 | |
| EPD MOSI | 14 | SSD1677 on SPI2 |
| EPD SCLK | 15 | |
| EPD CS | 16 | |
| EPD DC | 17 | |
| EPD BUSY | 18 | |
| Touch INT | 4 | Active low. See [touch](#touch) for when it can't be trusted. |
| Key 1 (button A) | 2 | Input, pull-up, active low |
| Key 2 (button B) | 3 | Input, pull-up, active low |
| PM1 IRQ | 1 | RTC-capable; the power button's wake line |
| Buzzer PWM | 42 | Unused; driven low so it stays silent |

M5IOE1 expander pins:

| Signal | IOE1 pin | Notes |
|--------|----------|-------|
| EPD 3.3 V enable | 3 | Push-pull, high = panel powered |
| EPD reset | 5 | Active low |
| Touch reset | 6 | Active low |
| LED green | 8 | PWM channel 2 |
| LED blue | 9 | PWM channel 1 |
| Charger I²C enable | 11 | **Keep low** |
| Touch VDD enable | 13 | Push-pull, high = touch powered |

The LoRa radio's antenna-switch and reset lines are also on the expander. This
firmware deliberately never touches them.

## Build settings that matter

From `firmware/platformio.ini`:

- **PSRAM is required.** `board_build.arduino.memory_type = qio_opi` with
  `-D BOARD_HAS_PSRAM`. M5GFX won't drive the panel without it.
- **Board definition:** `esp32-s3-devkitc-1`, with the flash size and
  partition table overridden for 16 MB. There's no PaperMono-specific board
  file; M5Unified detects the board at runtime.
- **USB:** `ARDUINO_USB_CDC_ON_BOOT=1` and `ARDUINO_USB_MODE=1`, so the serial
  log appears on the USB-C port at 115200 baud.
- **Loop stack:** raised to 16 KB (`ARDUINO_LOOP_STACK_SIZE=16384`). The default
  8 KB overflowed while parsing JSON on top of the drawing code. That's this
  app's need, not the board's.
- **Partitions** (`partitions_16mb.csv`): NVS, `otadata`, two 7 MB OTA app slots
  and a 1.9 MB data partition mounted as LittleFS. See
  [firmware.md](firmware.md#partition-layout-and-ota).

## Bring-up sequence

`BSP::init()` in `firmware/src/hal/bsp.cpp` does this, in this order:

1. **`M5.begin()`** with `clear_display = false` and the mic, speaker and IMU
   disabled. Set the frontlight brightness straight after, before anything is
   drawn, otherwise it flashes at 100 % on boot. Then turn auto-display off
   (`setAutoDisplay(false)`) so nothing reaches the panel until you ask.
2. **M5PM1** on `M5.In_I2C`. Try 400 kHz first: `begin()` checks the chip ID,
   so a failure at that speed shows up straight away, and then fall back to
   100 kHz. Up to three attempts per speed, 100 ms apart. Once it's up:
   - `ldoSetPowerHold(true)`: keeps the board powered on battery after the
     power button is released.
   - `setSingleResetDisable(true)`: lets software handle the power button
     instead of it resetting the board.
   - Clear the GPIO, system and button IRQs and read the button flag once, so
     the press that turned the device on isn't read as a request to turn it off.
3. **M5IOE1** on the same bus, with the same 400 kHz then 100 kHz probe. Only
   configure pins once the probe succeeds, so a failed probe can't leave a rail
   in the wrong state.
4. **Rails and resets** through the expander: panel 3.3 V on, touch VDD on,
   then pulse each reset low for 10 ms and wait 20 ms after releasing it.
5. **Charger off the bus:** drive IOE1 pin 11 low.
6. **Side keys** as inputs with pull-ups; **buzzer** pin low.
7. **LED off.** The PM1 lights the red LED after power-on, so it has to be
   turned off explicitly.

After that, `Epd::init()` clears the panel with a full refresh, and the app
can start drawing.

## Display

M5GFX drives the SSD1677. The firmware keeps rotation `0` (portrait, 480×800)
and auto-display off, and pushes pixels only in two ways (`hal/epd.cpp`):

| | Partial update | Full refresh |
|--|--|--|
| M5GFX mode | `epd_fastest` | `epd_quality` |
| Call | `display(x, y, w, h)` | `display()` then `waitDisplay()` |
| Result | No flash, 1-bit, lighter text | About 1 s flash, solid black text |

**Limit partial updates.** A long run of fast partial updates builds up ghosting
and DC imbalance, which can damage the panel permanently. The firmware counts
them and turns the 10th into a full refresh (`Config::kMaxPartialRefreshes`).
The limit of 10 comes from M5Stack's guidance for this panel and from community
experience; it isn't a measured threshold.

**Draw greys as dot patterns.** The fast waveform pushes mid-greys to black or
white, so a grey fill looks different after each partial update. Header bars
are drawn with a 1-bit dot pattern instead (`UI::dotScreen` in
`ui/widgets.h`), which looks the same under both waveforms.

**Don't upload custom waveforms.** A waveform (SSD1677 command `0x32`) that
isn't DC-balanced can permanently damage the panel. This firmware only uses the
waveforms M5GFX provides.

## Touch

`hal/touch.cpp` reads the FT6336G through `M5.Touch` (`getTouchPointRaw()`, then
clamped to 480×800) and turns each press into one event:

| Event | Rule |
|-------|------|
| Click | Moved less than 50 px in both directions, lifted within 1 s |
| Swipe left or right | Moved more than 70 px horizontally and less than 60 px vertically, within 600 ms |
| Swipe up or down | Moved more than 60 px vertically and less than 60 px horizontally, within 600 ms |

The swipe thresholds are tuning choices carried over from MonoMesh.

`M5.update()` is the most expensive I²C call in the loop, because it reads both
the touch controller and the PM1. The firmware calls it straight away while
touch INT (GPIO 4) is low, and otherwise only every 50 ms.
**Watch out:** M5GFX can leave the FT6336 in a polling mode where INT never
pulses. If INT hasn't gone low for 3 s, the firmware treats the pin as
unreliable and polls on every loop, so touch doesn't start lagging.

## Power

**Power button.** It's wired to the PM1, not a GPIO. Read it through both
`M5.BtnPWR.wasClicked()` and the PM1's own button flag (`btnGetFlag`, polled
every 100 ms). Ignore it for the first 2.5 s after boot.

**Turning off** (`BSP::shutdownHardware()`):

1. Frontlight and LED off, then panel and touch rails off through the expander.
2. `ldoSetPowerHold(false)`, then `M5PM1::shutdown()`, then `M5.Power.powerOff()`.
3. **Fallback:** with USB power connected, the PM1 can refuse to cut power. If
   the board is still running 150 ms later, it goes into deep sleep instead,
   woken by the power button (PM1 IRQ on GPIO 1, active low, EXT0) or a 60 s
   timer. Never deep-sleep without a wake source: a device that looks dead and
   ignores the power button until a hardware reset is worse than one that
   wakes up again.

The PM1's always-on part keeps drawing about 20 µA after shutdown. That figure
is carried over from MonoMesh; it hasn't been measured on this firmware.

**Battery.**
- Voltage comes from `M5.Power.getBatteryVoltage()`.
- Charging is detected from the PM1's VIN reading: 4400 mV or more counts as
  charging.
- The percentage is a straight line from 3200 mV (0 %) to 4150 mV (100 %). It
  reads high when the battery is nearly empty, so protection uses millivolts
  instead: below 3450 mV while not charging, the device shows a "Battery Low"
  screen and turns off. The 3450 mV cut-off comes from MonoMesh's measured
  discharge curve.

## Status LED

| Colour | Driven by |
|--------|-----------|
| Red | PM1 LED enable (`setLedEnLevel`), on or off only |
| Green | IOE1 PWM channel 2 (pin 8) |
| Blue | IOE1 PWM channel 1 (pin 9) |

The PWM frequency is 5 kHz. The firmware blinks green for 200 ms after a
successful sync and red after a failed one.

## I²C bus hygiene

- PM1, IOE1 and the touch controller share one bus (GPIO 47/48) through
  `M5.In_I2C`.
- **Keep the IP2315 charger off it.** With IOE1 pin 11 high, the charger joins
  the bus and can lock it up. Drive that pin low early and leave it there.
- Probe at 400 kHz and fall back to 100 kHz, as described in
  [bring-up](#bring-up-sequence).

## Download mode and recovery

Hold the power button for about 2 seconds, until the small red LED blinks.
Before flashing anything, back up the full 16 MB flash, because it holds the
unit's RF calibration data. [firmware.md](firmware.md#back-up-the-device-first)
has the commands.

## Using this code in your own firmware

`firmware/src/hal/` doesn't depend on the rest of the app, apart from reading
`kMaxPartialRefreshes` from `config.h`. To start your own project from it:

1. Copy `hal/` and `ui/widgets.h` (if you want the card and header style), plus
   the `platformio.ini` build settings and `lib_deps` above.
2. Provide a `config.h` with `Config::kMaxPartialRefreshes`.
3. In `setup()`:

   ```cpp
   BSP::getInstance().init();          // power, rails, resets, LED off
   Epd::getInstance().init();          // clears the panel with a full refresh
   TouchManager::getInstance().init();
   ```

4. In `loop()`: call `BSP::update()` and `TouchManager::update()`, handle
   `checkPowerButton()`, pop touch events, draw into `M5.Display` and push
   changes with `Epd::partialUpdate()` or `Epd::fullRefresh()`.

The code is GPL-3.0, and parts of it derive from
[MonoMesh](https://github.com/andrecolz/MonoMesh), so a derived firmware has to
be GPL-3.0 as well.

## Not yet explored

- Measured current draw in each state, including light sleep (see [firmware.md](firmware.md#sleep)).
- Cutting the panel's analog supply during sleep (M5Stack's UserDemo does this 500 ms after a fast
  refresh). It would save power, but it makes the next fast refresh rebuild the panel's history.
- Longer sleeps by shutting the PMIC down with an RTC wake (touch cannot wake from that).
- The RTC, NFC and the LoRa radio.
- Grey levels beyond the two M5GFX modes used here.
- Whether the C153-LITE needs any changes.
