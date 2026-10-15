# HTTP API

JSON over HTTP, no authentication (see [server.md](server.md#security)). The
running server also serves interactive OpenAPI docs at `/docs`.

Errors use FastAPI's shape, `{"detail": "..."}`. A `4xx` from a device's queued
edit means "never retry this": the firmware drops the edit (see
[architecture.md](architecture.md#sync-protocol)).

## Objects

```jsonc
// Category (an aisle)
{ "id": 3, "name": "Dairy", "sort_order": 2 }

// Item
{
  "id": 17,
  "name": "Milk",
  "category_id": 3,            // null if its aisle was deleted
  "category_name": "Dairy",    // "Uncategorized" when category_id is null
  "purchased": false,
  "quantity": 2,                // null if not tracked
  "unit": "L",                  // null for a plain count (rendered as "×2"); always null if quantity is null
  "created_at": "2026-09-26T08:12:44.120Z",
  "updated_at": "2026-09-26T08:12:44.120Z"
}

// Catalog entry (a previously-added name)
{ "name": "Milk", "category_id": 3, "category_name": "Dairy" }
```

## Sync

### `GET /api/sync`

Everything a client needs, in one response. The firmware uses nothing else to
read.

```json
{ "categories": [Category], "items": [Item], "catalog": [CatalogEntry], "next_sync_in_s": 1800, "synced_at": "14:05" }
```

- `categories` are in walking order (`sort_order`, then name).
- `items` are grouped by aisle in walking order, then alphabetical within an
  aisle. Items with no aisle sort with `Uncategorized`, last. Clients render in
  this order and rely on it for grouping.
- `catalog` is alphabetical.
- `next_sync_in_s` is how many seconds the device should wait before its next
  periodic sync, from the [sync schedule](#sync-schedule). Always present, between
  30 and 43200 (12 hours). A client that doesn't sleep can ignore it.
- `synced_at` is the server's local time of day when it answered, as `HH:MM` in
  `SHOPPING_LIST_TIMEZONE`. The device shows it in its header ("Synced 14:05"), because
  it has no clock and a panel that only redraws on a sync can't show an age.
- `firmware` is only present when the server wants the requesting device on a
  different firmware version. See [Firmware updates](#firmware-updates).

Devices may also send optional health headers, which the server records as metrics
(see [Observability](server.md#observability-opentelemetry)) and never uses to change the response:

| Header | Range | |
|--------|-------|--|
| `X-Battery-Percent` | 0-100 | Battery level |
| `X-Wifi-Rssi` | -127-0 | Wi-Fi signal, dBm |
| `X-Free-Heap` | >= 0 | Free heap, bytes |
| `traceparent` | W3C | Trace context; the server's spans for this sync join that trace |

Missing, malformed or out-of-range values are ignored; they never fail the sync.

## Sync schedule

Which times the device syncs is set on the server, from the web UI's **Schedule**
page. It is a preset for weekdays and a preset for weekends (Saturday and
Sunday), read in the server's timezone (`SHOPPING_LIST_TIMEZONE`, see
[server.md](server.md#configuration)). Each preset is one or more time windows
and an interval inside them; outside every window the device is left alone.
Windows are half-open, so an hourly 07:00-09:00 window syncs at 07:00 and 08:00.

### `GET /api/sync-schedule`

```json
{
  "weekday": "work",
  "weekend": "day",
  "weekend_same": false,
  "presets": [
    {
      "id": "work",
      "name": "Before and after work",
      "description": "Hourly 07:00 to 09:00 and 17:00 to 22:00. Quiet the rest of the day.",
      "interval_minutes": 60,
      "windows": [{ "start": "07:00", "end": "09:00" }, { "start": "17:00", "end": "22:00" }],
      "syncs_per_day": 7
    }
  ]
}
```

`presets` lists every choice (the built-in ones are `work`, `day`, `saver` and
`always`). When `weekend_same` is true weekends follow `weekday`, and `weekend`
is kept only so the choice isn't lost if the box is unticked again. A fresh install uses
`day` for both.

### `PUT /api/sync-schedule` -> `200`

Body: `{ "weekday": "work", "weekend": "day", "weekend_same": false }`. Replies
with the same document as `GET`. `422` if either preset id is unknown.

The change reaches a device on its next sync, because that is when it is told
how long to sleep.

## Firmware updates

The device says which firmware it's running with a request header on
`GET /api/sync`:

```
X-Firmware-Version: 1.3.2
```

The server decides whether that device should update. If so, the sync response
gains one extra top-level field:

```jsonc
{
  "categories": [...], "items": [...], "catalog": [...],
  "firmware": {
    "version": "1.4.0",
    "url": "/api/firmware/1.4.0.bin",   // relative to the server address
    "sha256": "9f2c...",                // 64 lowercase hex characters
    "size": 1310720                     // bytes
  }
}
```

The key is **omitted entirely** (not `null`) when there's nothing to do: the
device already reports the target version, the header is missing or malformed,
no target version is configured, or the binary for it isn't on the server. A
missing binary is logged as a warning and never fails the sync.

The device installs `version` if it differs from what it's running, not only if
it's newer. That is what makes a rollback work: point the server's target back
at an older version.

### `GET /api/firmware/{version}.bin` -> `200`

The application image (`application/octet-stream`) for that version, with
`Content-Length` and an `ETag`. This is the app-only `firmware.bin` that
PlatformIO builds, **not** the merged factory image the web flasher uses. `404`
if the server has no image for that version.

## Items

### `POST /api/items` → `201 Item`

```json
{ "name": "Milk", "category_id": 3, "quantity": 2, "unit": "L" }
```

`name` is 1 to 128 characters and is trimmed; blank names get a `400`.
`category_id` is optional. Leave it out to let the server choose the aisle
([how](architecture.md#how-a-new-item-gets-its-aisle)); that can take a few
seconds if the classifier runs. An unknown `category_id` gets a `400`.
`quantity` and `unit` are both optional; `unit` (max 32 chars) is meaningless
without a `quantity` and gets a `400`, as does a `quantity` that isn't `> 0`.

### `PATCH /api/items/{id}` → `200 Item`

Any subset of:

```json
{ "purchased": true, "name": "Oat milk", "category_id": 4, "quantity": 1, "unit": "L" }
```

Setting `category_id` also updates the catalog, so future adds of this name go
to the new aisle. An unknown item gets a `404`; an unknown `category_id` gets a
`400`. `quantity`/`unit` follow the same validation as create; unlike the other
fields, explicitly sending `"quantity": null` (and `"unit": null`) clears it -
omitting the field entirely leaves it unchanged.

### `DELETE /api/items/{id}` → `204`

`404` if it doesn't exist.

### `POST /api/items/clear-purchased` → `204`

Deletes every purchased item.

## Categories

### `GET /api/categories` → `200 [Category]`

In walking order.

### `POST /api/categories` → `201 Category`

```json
{ "name": "Frozen" }
```

Added at the end of the walk, just before `Uncategorized`. A name that already
exists, compared case-insensitively, gets a `409`.

### `PATCH /api/categories/{id}` → `200 Category`

```json
{ "name": "Freezer", "sort_order": 5 }
```

Both fields are optional. The web UI reorders aisles by swapping the
`sort_order` of two neighbours. Renaming to a name another aisle already has
gets a `409`, and renaming `Uncategorized` gets a `400`.

### `DELETE /api/categories/{id}` → `204`

Its items and catalog entries become uncategorised. Deleting `Uncategorized`
gets a `400`.

## Catalog

### `GET /api/catalog/suggest?q=mil&limit=8` → `200 [CatalogEntry]`

Case-insensitive substring match, with prefix matches first. `limit` is 1 to 25
(default 8), and an empty `q` returns `[]`. Used by the web UI as you type; the
device searches its own copy of the catalog instead.

### `GET /api/catalog` → `200 [CatalogEntry]`

The whole catalog, alphabetical.

### `PATCH /api/catalog?name=Bananna` → `200 CatalogEntry`

Body: `{ "name": "Banana", "category_id": 3 }`, both optional. Renames the entry
and/or moves it to another aisle; `name` in the query string is the current
(exact) name. Items already on the list keep their own name. `404` if there's no
such entry, `400` for a blank name or unknown `category_id`, `409` if the new
name matches another entry (case-insensitive).

### `DELETE /api/catalog?name=Bananna` → `204`

Forgets a name so it stops autosuggesting. Items on the list are untouched, and
adding the name again re-creates the entry. `404` if there's no such entry.

## Health

### `GET /api/health` → `{"status": "ok"}`
