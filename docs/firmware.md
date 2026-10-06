# Firmware

## Hardware

An [M5Stack PaperMono](https://docs.m5stack.com/en/core/PaperMono): ESP32-S3R8
(16 MB flash, 8 MB octal PSRAM), a 3.97" 480×800 SSD1677 e-paper panel with an
FT6336G capacitive touch layer, an M5PM1 power-management IC and an M5IOE1 IO
expander. It's been tested on the **C153**. The C153-LITE has the same core
hardware minus NFC and LoRa, which this firmware doesn't use, so it should work
but is untested.

[hardware.md](hardware.md) has the pin map, I²C addresses, bring-up sequence and
the display, touch and power details.

## Configure

Wi-Fi and the server address are compiled in:

```sh
cd firmware
cp src/secrets.example.h src/secrets.h
$EDITOR src/secrets.h
```

```c
#define SHOPPING_LIST_WIFI_SSID "your-wifi-ssid"
#define SHOPPING_LIST_WIFI_PASSWORD "your-wifi-password"
#define SHOPPING_LIST_SERVER_URL "http://192.168.1.50:8000"   // no trailing slash
```

`secrets.h` is git-ignored, and the build fails with a clear error if it's
missing. Set it correctly before flashing: the ESP32 Wi-Fi stack saves whatever
credentials it's given to flash, so a build with placeholder values would
overwrite credentials a previous firmware stored.

Other tunables (fallback sync interval, timeouts, the partial-refresh limit) are in
`src/config.h`.

The firmware's own version is `FW_VERSION` in `platformio.ini`'s `build_flags`.
The device sends it on every sync, and the server compares it with the version
it wants the device on (see [OTA](#partition-layout-and-ota)). Bump it for every
build you intend to publish.

## Build

With [PlatformIO](https://platformio.org/) (CLI or the VS Code extension):

```sh
pio run
```

The first build downloads the ESP32 toolchain and libraries, which takes a few
minutes.

### Building in a container

To keep PlatformIO and its toolchains off your machine, build in Docker instead:

```sh
export SHOPPING_LIST_WIFI_SSID='your-wifi-ssid'
export SHOPPING_LIST_WIFI_PASSWORD='your-wifi-password'
export SHOPPING_LIST_SERVER_URL='http://192.168.1.50:8000'   # http, no trailing slash
just firmware            # or: firmware/builder/build.sh -c settings.env
```

The settings are injected at build time: the builder writes them into a
`secrets.h` inside a throwaway copy of the source, so nothing is stored in the
image or the working tree. `-c` takes a file of `KEY=VALUE` lines (no quotes).
All three are always required -- the build fails immediately, naming what's
missing, if any is unset; there is no fallback to an existing `secrets.h` or to
`secrets.example.h`. The image is written to `firmware/dist/<version>.bin`;
toolchains are cached in the `papermono-pio` Docker volume (about 2.5 GB).

## Back up the device first

Before flashing anything onto a PaperMono for the first time, save a full copy of
its 16 MB flash. It holds that unit's RF calibration data, and it's the only way
back to the factory firmware.

1. Put the device in download mode: hold the power button for about 2 seconds,
   until the small red LED blinks.
2. Read the whole flash (`esptool` v5 syntax; v4 uses `read_flash`):

   ```sh
   pip install esptool
   esptool --chip esp32s3 --port /dev/ttyACM0 read-flash 0 0x1000000 papermono-backup.bin
   ```

   On macOS the port is `/dev/cu.usbmodem*`; on Windows it's `COMx`.

To restore it later: `esptool --chip esp32s3 --port /dev/ttyACM0 write-flash 0 papermono-backup.bin`.

Don't run `erase-flash` unless you have that backup.

## Flash

### Over USB with PlatformIO

```sh
pio run -t upload
pio device monitor        # serial log at 115200 baud
```

If the upload can't connect, put the device in download mode (see above) and try
again.

### From a browser

If the machine the device is plugged into doesn't have PlatformIO, build a single
merged image and flash it from the server's own web flasher (Chrome or Edge, over
WebSerial - see [server.md](server.md)):

```sh
pio run
tools/make_factory_image.sh --catalog ../server/src/shopping_list/flasher/firmware/catalog.json
```

This copies `papermono-shopping-list.factory.bin` next to that catalog and
registers it, creating `catalog.json` if it doesn't exist yet. Both the image
and `catalog.json` are git-ignored, so neither gets committed. Then open
`http://<server-host>:8000/flash/`, connect, and pick it from the catalog - or
skip the catalog step entirely and flash any local `.bin` file by picking it
directly in the page.

## Partition layout and OTA

`partitions_16mb.csv` has two equal app slots (`ota_0`, `ota_1`) plus `otadata`,
which records which one boots. An update is written to whichever slot isn't
running, and the boot slot only switches once it's verified. The old image stays
in the other slot, which is what lets a bad update roll back. See
[architecture.md](architecture.md#firmware-updates-ota) for the protocol.

**Moving from the old single-slot layout needs one USB flash.** The partition
table lives at a fixed offset and can't be changed over the air. Flash the new
firmware once, with `pio run -t upload` or the merged image from the
[web flasher](#from-a-browser), and every update after that can go over Wi-Fi.
`nvs`, `phy_init` and `spiffs` are at the same offsets as before, so that flash
keeps the Wi-Fi credentials and the cached list. The old app image ends up
partly overwritten, so there's no going back to the old firmware without
flashing it again. (The [backup](#back-up-the-device-first) still restores
everything.)

The merged factory image is rebuilt from the partition table by
`tools/make_factory_image.sh`, which reads the `otadata` and `ota_0` offsets
from `partitions_16mb.csv`. OTA itself needs only the app-only `firmware.bin`
(see [server.md](server.md#firmware-updates-ota)). The web flasher's "app only"
option writes at 0x10000, which is `otadata` in this layout, so use the merged
image.

The running version is `FW_VERSION`. A firmware flashed over USB starts out
confirmed, since nothing has to be verified; only images installed by OTA go
through the pending-verify step.

## Using it

| Do this | What happens |
|---------|--------------|
| Tap an item | Tick it off, or untick it. Syncs straight away if Wi-Fi is reachable. |
| Tap the quantity on the right of an item's row | Opens a screen to set or change its quantity/unit (e.g. `x3` or `500 g`). Tapping **UNIT** there opens a popup listing all 8 options; tap one to pick it. Shows a muted **qty** placeholder when none is set. |
| Tap **+ ADD ITEM** | Opens the keyboard. As you type, up to three suggestions from past items appear above it; tap one to use it, including its aisle. **SEND** opens the quantity screen for the new item; **CANCEL** there abandons the add. |
| Swipe right, or tap above the keyboard | Closes the keyboard without adding anything. |
| Swipe up or down, or press the side buttons | Page through the list. |
| Tap **SETTINGS** in the add bar (or the header bar, as a shortcut) | Opens settings: frontlight level, **SYNC NOW**, and status (Wi-Fi network, last sync result, battery). Opens with a quick partial update rather than a full-panel flash. |
| Short press the power button | Power off. |

The status LED blinks green after a successful sync and red after a failed one.
The header shows when the last successful sync happened, as a time of day taken from the server ("Synced 14:05", or "Offline, last 14:05" if the latest attempt failed; "syncing..." / "updating firmware..." while one runs) on the left and the battery percentage on the right, both in large type. The device syncs
on the schedule set in the web UI (by default every 30 minutes from 07:00 to
22:00 and never overnight; the server sends the wait after each sync, with an
hourly fallback if it can't be reached) and also opportunistically on any tap if the last
sync is more than 5 minutes old, so actively using it keeps the list fresh
without needing a fast fixed interval running in the background the rest of
the time. Firmware from before 1.4.0 ignores the schedule and syncs hourly.

Items added while the server can't be reached show in grey under **PENDING SYNC**
and can't be ticked until they've synced. Everything on screen and every queued
edit survives a reboot.

## Telemetry

Each sync request also carries `X-Battery-Percent`, `X-Wifi-Rssi`, `X-Free-Heap` and a W3C
`traceparent`. The server turns them into metrics and joins its spans to that trace when
OpenTelemetry is enabled ([details](server.md#observability-opentelemetry)). The device runs no
OpenTelemetry SDK and sends nothing anywhere but your server. The trace id is also printed on the
serial log (`sync traceparent ...`), so a sync can be matched to the server's view of it.

## Power

Wi-Fi is on only during a sync, and the frontlight is off unless you turn it on.
When the battery drops to 3.45 V (resting, not charging), the device draws a
"Battery Low" screen and powers off. E-paper keeps showing the last image with no
power.

### Sleep

Between syncs the device light-sleeps: the CPU stops and the radio is off, but RAM, the panel
and the touch controller keep their state. It wakes with the list still on the glass, and a
tap that wakes it is not lost. It wakes for:

| Wake | What happens |
|------|--------------|
| **Timer** | The next scheduled sync (the server's `next_sync_in_s`). It syncs quietly, repaints the panel only if the list changed, and sleeps again about 1.5 s later. No LED, no "syncing...". |
| **Touch** | The touch controller's interrupt (GPIO 4). Same as a key wake. |
| **Side key or power button** | Wakes up for a person: the backlight returns to its previous level, it syncs if the list is more than 5 minutes old, and it sleeps after 30 s with no input (8 s if nothing was pressed at all). The wake itself, and a sync it starts, draw nothing on the panel. The power-button press that wakes it does not also power it off. |

Things that keep it awake: a sync in progress or waiting, and an open keyboard, settings or
quantity screen (closed after 2 minutes idle).

Why light sleep and not deep sleep: deep sleep reboots the chip, so the panel driver starts from
scratch and the panel came back blank on the first refresh after a wake. M5Stack's own Arduino
guide keeps the display state through light sleep, and the best-known PaperMono firmware (the
ESPHome Home Assistant project) sleeps this way for normal operation. Light sleep draws more than
deep sleep would; the numbers below are unmeasured.

The `X-Power` header on each sync reports wakes by reason and time awake against asleep since the
previous sync (`timer=2;touch=1;key=0;pb=0;other=0;awake_ms=31400;slept_ms=600000`), and the
server logs it. That is the way to check the device really sleeps.

The header can lag: a quiet sync that changes nothing leaves the panel untouched, so
"Synced 14:05" may be a few syncs old until the next repaint. Build with
`-D SHOPPING_LIST_SLEEP=0` to keep the device awake (for debugging over serial: light sleep drops the
USB serial port from the host).

**Not yet measured:** current draw in each state.

## Troubleshooting

**It never syncs.** Open settings (tap the header). If **LAST SYNC** says
**FAILED**, check that **WI-FI** shows the network you expect, that the server URL
in `secrets.h` is reachable from that network (try `curl <url>/api/health` from
another device on it), and look at the serial log (`pio device monitor`) for the
HTTP status or Wi-Fi timeout.

**It seems frozen for a few seconds after a tap.** A sync is running, and syncs
block the UI. Out of Wi-Fi range, a sync after a tap gives up after 5 s. If Wi-Fi
connects but the server doesn't answer, each request can wait up to 8 s. See
[known limitations](design-notes.md#known-limitations).

**The screen looks ghosted or washed out.** Any action that does a full refresh
cleans it up, such as closing settings. One also happens automatically after
every 10 partial updates.

**It doesn't respond at all.** Press the power button. If USB power is connected
while shutting down, the power IC may refuse to cut power; the firmware then
falls back to deep sleep, which the power button (or a 60 s timer) wakes it from.

## Panel safety

These rules come from M5Stack's guidance and community experience with this
panel. `src/hal/epd.cpp` enforces the first one.

- Don't run an unbounded string of fast partial refreshes. Do a full refresh at
  least every ~10.
- Don't upload custom SSD1677 waveforms (command `0x32`). A waveform that isn't
  DC-balanced can permanently damage the panel.
- Keep the IP2315 charger IC off the shared I²C bus (M5IOE1 pin 11 low). It can
  lock the bus.
