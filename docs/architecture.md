# Architecture

## Components

| Component | Where | Role |
|-----------|-------|------|
| Server | `server/src/shopping_list/` | Source of truth. FastAPI over SQLite; serves the HTTP API and the phone web UI. |
| Web UI | `server/src/shopping_list/{templates,static}/` | Plain HTML/JS page for phones: add, tick, re-file, clear, manage aisles. No build step. |
| Firmware | `firmware/src/` | The PaperMono app. Renders a local copy of the list and syncs it with the server over Wi-Fi. |
| Classifier | `server/src/shopping_list/classifier.py` | Optional. Sorts never-seen item names into aisles via the Claude Code CLI. |

The server is the only thing that writes the database. Both clients talk plain
HTTP/JSON to it; neither talks to the other.

## Data model

Three tables (`server/src/shopping_list/db.py`):

- **categories**: the aisles. `sort_order` is the walking order. There's always
  an `Uncategorized` row that sorts last and can't be renamed or deleted.
- **items**: what's on the list right now: name, aisle, purchased flag, and an
  optional quantity + unit (e.g. `2` `L`, or a plain count with no unit).
- **catalog**: every item name ever added, mapped to the aisle it was last filed
  under. This drives autosuggest and means an item is classified at most once.

Deleting an aisle sets `category_id` to NULL on its items and catalog entries
(`ON DELETE SET NULL`). Those are then reported as `Uncategorized`, and a catalog
entry with no aisle counts as a miss, so the name is classified again next time.

### How a new item gets its aisle

`repository.resolve_category_id` tries, in order:

1. An explicit `category_id` in the request. The device and web UI send one when
   the user picked an autosuggestion, since they already know its aisle.
2. The catalog entry for that name (case-insensitive).
3. The classifier, if one is configured and at least one aisle exists. It either
   names an existing aisle or proposes a new one, which is created at the end of
   the walk.
4. `Uncategorized`.

The result is always written back to the catalog. Re-filing an item from the web
UI also updates its catalog entry, which is how a bad classification gets
corrected for good.

## Sync protocol

The device keeps a full copy of the list and syncs it by replacing everything,
not by exchanging deltas:

```
connect Wi-Fi
for each queued offline edit, in order:      POST /api/items  or  PATCH /api/items/{id}
    2xx       -> done, remove from queue
    4xx       -> the server will never accept it (e.g. item already deleted): drop it
    other     -> network error or 5xx: keep it for next time
GET /api/sync                               -> categories + items + catalog, pre-sorted
                                            (+ "firmware" if an update is on offer)
disconnect Wi-Fi
re-add any still-queued "add item" edits as local placeholders
save the result to flash
```

If the response offers a firmware update, the device then runs the
[update procedure](#firmware-updates-ota) as a separate step. It never runs
inside the sync itself.

**Why a full replace:** the whole payload is a few KB for a household list, and
the device spends most of its time offline. Resending everything is simpler and
more robust than keeping versions consistent across long offline stretches.

**Why push before pull:** otherwise an item ticked just before the sync would be
un-ticked by the snapshot that follows it.

**Why drop on 4xx:** the queue is replayed in order, so one edit the server
will never accept (for example, ticking an item someone else has since cleared)
would otherwise block every edit behind it forever.

### When the device syncs

Between syncs the device is light-sleeping (see [firmware.md](firmware.md#sleep)); a timer set from
`next_sync_in_s` wakes it for each scheduled sync, and a key or touch wakes it for use. Everything
below happens while it is awake.

- At boot, straight after showing the cached list.
- On the server's schedule: each sync response says how long to wait (`next_sync_in_s`,
  see [Sync schedule](api.md#sync-schedule)), so the device stays quiet overnight
  and syncs more at weekends if so configured. Without an answer (a failed sync, or
  an older server) it falls back to every hour (`Config::kSyncIntervalMs`).
- Opportunistically on any tap, if the last successful sync is more than 5
  minutes old (`Config::kTapSyncStaleMs`) - otherwise a stale list would just
  sit there until the next scheduled sync while someone's actively using the
  device. The header shows "syncing..." while this runs.
- As soon as possible after a local edit (tick, add or quantity change), with
  a shorter Wi-Fi timeout (5 s rather than 15 s) so a tap made out of range
  fails quickly.
- Never while the keyboard, settings or quantity screen is open. The keyboard
  holds pointers into the catalog, which a sync replaces.
- After a failed sync, not again for 30 s, so repeated taps don't each wait out
  the timeout.

Wi-Fi is switched off between syncs. A sync blocks the main loop, so touch isn't
serviced while one runs.

## Firmware updates (OTA)

The device can replace its own firmware over Wi-Fi. The server decides when.

```
GET /api/sync   X-Firmware-Version: 1.3.2
  <- "firmware": {version, url, sha256, size}      (only if it should update)
sync finishes normally: list saved, screen refreshed
if a firmware offer came back and it's safe (battery, no recent failure):
    check `size` fits the idle OTA slot
    GET <url>, streaming into the idle slot and hashing as it goes
    hash != sha256   -> abort, leave the running firmware alone
    switch the boot partition to the new slot, reboot
new firmware boots "pending verify"
    first successful sync -> mark it valid
    any reset before that -> the bootloader reverts to the previous image,
                             and that version is never offered to this device again
```

**Why the server decides:** the device only reports what it's running. The
server can hold back or roll back a version without a firmware change, skips
the `firmware` field once the device is current, and can log which version each
sync came from. The device's only rule is "if offered a version that isn't mine,
install it", with no "newer than" comparison, so pointing the target at an older
version is a rollback.

**Why a body field, and the version in a header:** the device already parses
the sync JSON, so one more optional field costs nothing, and a body survives
reverse proxies untouched. The version goes the other way, as a request header,
because the server is the only reader and a header keeps it out of the URL and
cache key of `/api/sync`.

**Why after the sync:** the list is refreshed first, so a failed or skipped
update never leaves the user looking at stale data.

**Why a checksum and no signature:** the sha256 catches a truncated or
corrupted download. It doesn't stop someone who can already answer the device's
requests from serving both a different image and its hash. On a private LAN
with the [unauthenticated API](server.md#security) that's the same trust the
list itself already relies on. If the server were ever reachable from outside,
sign the images instead.

**Why rollback matters here:** the device is awkward to reach, so a bad image
must undo itself. The rollback is the ESP32 bootloader's own (`main.cpp` overrides
the Arduino core's `verifyRollbackLater()` so a new image isn't marked valid at
boot). The image is only marked valid after a successful sync, so an image that
boots but can't reach the server (say, a wrong server URL) is reverted too, the
next time the device resets. Any reset counts, including a crash, a brownout, or
the power button, so don't power the device off until it has synced after an
update.

**Why a rolled-back version is remembered:** the server doesn't know the update
failed, so it would offer the same version again on the next sync and the device
would install and revert it in a loop. The device keeps the last rolled-back
version in NVS and ignores an offer of it. To try again, publish a fixed build
under a new version number.

**Safety rules the device applies:**

- Only starts when charging or at 50% battery or more
  (`Config::kOtaMinBatteryPercent`), so a flash never runs out of power halfway.
- Only follows a relative `url`, so a response can't point it at another host.
- Checks `size` against the idle slot and against `Content-Length` before
  writing anything, and gives up on a download that stalls for 15 s.
- After a failed attempt it doesn't retry for 24 hours (or until the next
  reboot), so a bad link can't turn every sync into a download-and-fail loop.
- Like any sync it blocks the main loop, so the screen shows an "updating"
  message first.

The flash layout that makes this possible (two app slots plus `otadata`) is in
[firmware.md](firmware.md#partition-layout-and-ota).

## Offline behaviour

The device reads and writes a local store (`firmware/src/sync/local_store.cpp`):
JSON files on LittleFS holding the last snapshot and the queue of offline edits.
NVS isn't used because it caps a single value at about 4000 bytes, which a catalog
of only about 60 items would exceed.

- **Ticking an item** updates the screen immediately and queues a `PATCH`.
- **Adding an item** adds a grey placeholder under a "PENDING SYNC" heading and
  queues a `POST`. A placeholder can't be ticked until a sync gives it a real
  server id.
- The screen, autosuggest and the queue all survive a reboot.

## Firmware structure

```
firmware/src/
  main.cpp              loop: board upkeep, touch routing, side buttons, sync scheduling
  config.h              tunables; pulls Wi-Fi/server settings from secrets.h (git-ignored)
  model.h               Category / Item / CatalogEntry / PendingAction
  hal/
    bsp.*               power IC, IO expander, rails, battery, power button, LED, frontlight
    epd.*               e-paper refresh policy (partial vs full)
    touch.*             FT6336G touch -> click / swipe events
  sync/
    sync_client.*       Wi-Fi + HTTP sync (the protocol above)
    ota.*               firmware update: download, verify, switch slot, rollback
    local_store.*       LittleFS cache + offline queue
    model_json.*        JSON mapping shared by the two, matching the server's models.py
  ui/
    widgets.h           shared drawing / hit-testing primitives
    list_screen.*       the main list
    keyboard.*          on-screen keyboard + autosuggest
    settings_screen.*   frontlight, sync now, status
    quantity_screen.*   set/edit an item's quantity + unit
```

Drivers and overlays are singletons; the list data and the list screen are
owned by `main.cpp`, which passes the data to the screen and the sync client.

## Display refresh policy

E-paper has two relevant update modes (`firmware/src/hal/epd.h`):

| | Partial update | Full refresh |
|--|--|--|
| Waveform | M5GFX `epd_fastest` (1-bit) | M5GFX `epd_quality` |
| Area | A rectangle | Whole panel |
| Looks like | Instant, no flash, lighter text | About 1 s black/white flash, crisp black text |
| Used for | Ticking, scrolling, typing, settings steps | Boot, a sync that changed something, closing the keyboard or settings |

After 10 partial updates in a row (`Config::kMaxPartialRefreshes`), the next one
becomes a full refresh. Long runs of partial updates build up ghosting and DC
imbalance on the panel, and can permanently damage it.

The header bar is drawn as a black-on-white dot pattern rather than a grey fill.
The 1-bit partial waveform pushes greys to black or white, so a grey bar would
look different after every partial update.
