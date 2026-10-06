# Server

A small FastAPI app with a single SQLite file. It serves the [HTTP API](api.md),
the phone web UI at `/`, and the [PaperMono web flasher](../server/src/shopping_list/flasher)
at `/flash/` (flash a device straight from the phone/browser over WebSerial - see
[firmware.md](firmware.md#flash)). Python 3.11 or newer.

## Run it locally

```sh
cd server
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
SHOPPING_LIST_CLASSIFIER=none .venv/bin/uvicorn shopping_list.main:app --reload --host 0.0.0.0
```

Then open `http://localhost:8000/`. The database is created at
`data/shopping.db` under the current directory.

Tests and lint:

```sh
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## Configuration

All configuration is through environment variables:

| Variable | Default | |
|----------|---------|--|
| `SHOPPING_LIST_DB` | `data/shopping.db` | SQLite file. Relative to the working directory. Created on first run. |
| `SHOPPING_LIST_CLASSIFIER` | `claude` | `claude` to auto-sort new items, `none` to disable. |
| `CLAUDE_BIN` | `claude` | Path to the Claude Code CLI, if it isn't on the service's `PATH`. |
| `SHOPPING_LIST_CLASSIFY_TIMEOUT` | `30` | Seconds before giving up on a classification. |
| `SHOPPING_LIST_FIRMWARE_DIR` | `data/firmware` | Directory holding OTA images, one `<version>.bin` each. Relative to the working directory. The systemd unit and Docker image point it at `firmware/` under their data directory. |
| `SHOPPING_LIST_FIRMWARE_VERSION` | *(unset)* | The version every device should be running. Unset means no OTA is offered. |
| `SHOPPING_LIST_TIMEZONE` | *(unset)* | IANA timezone, e.g. `Europe/Dublin`, that the [sync schedule](#sync-schedule) reads its clock times and weekends in. Unset uses the server's own local time, which is UTC in most containers, so set it. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset)* | Turns on OpenTelemetry export when set. See [Observability](#observability-opentelemetry). |

A new database starts with only the `Uncategorized` aisle. Add your real aisles
from the web UI (**Aisles**) in the order you walk the shop. The classifier only
runs once at least one aisle exists.

## Sync schedule

The device doesn't decide when to sync; the server tells it. Every sync response
carries `next_sync_in_s`, and the device sleeps that long (see
[the API](api.md#sync-schedule)). Open **Schedule** at the top of the web UI, or
`/schedule`, to choose a preset for weekdays and another for weekends:

| Preset | When |
|--------|------|
| Before and after work | Hourly 07:00-09:00 and 17:00-22:00 |
| Daytime (default) | Every 30 minutes 07:00-22:00 |
| Battery saver | Hourly 08:00-20:00 |
| Always fresh | Every 15 minutes, day and night |

Presets are defined in `src/shopping_list/schedule.py`; add one there and it
appears on the page. Taps and edits on the device still sync straight away
whatever the schedule says. Changes take effect on a device's next sync. The wait
is never more than 12 hours, so a device always checks in at least that often.
Set `SHOPPING_LIST_TIMEZONE`, or "07:00" means 07:00 on the server's clock.

## Auto-sorting with Claude

When an item name has never been seen before, the server runs
`claude -p "<prompt>"` with the list of aisles, and files the item under the
aisle Claude names, or creates the new one it proposes. The answer is saved to
the catalog, so each distinct name costs at most one call, typically a few
seconds. If the CLI is missing, fails or times out, the item goes to
`Uncategorized`, and re-filing it from the web UI teaches the catalog.

To enable it on the server host:

1. Install the [Claude Code CLI](https://code.claude.com/docs/en/setup).
2. Authenticate it non-interactively with **one** of:
   - `ANTHROPIC_API_KEY`: a Claude API key (usage-billed), or
   - `CLAUDE_CODE_OAUTH_TOKEN`: a long-lived token from running `claude setup-token`
     on a machine with a browser (uses a Claude subscription).
3. Put that variable in the service's environment. If the CLI isn't on the
   service's `PATH` (often `/usr/local/bin` isn't under systemd), set `CLAUDE_BIN`
   to its full path.

Don't leave a placeholder `ANTHROPIC_API_KEY` set alongside an OAuth token. The
API key takes precedence, so every classification quietly fails and everything
lands in `Uncategorized`.

## Firmware updates (OTA)

Devices report their version in an `X-Firmware-Version` header on every sync,
and the server offers an update whenever that differs from
`SHOPPING_LIST_FIRMWARE_VERSION` (see [the API](api.md#firmware-updates) and
[how it works](architecture.md#firmware-updates-ota)). To publish a release:

1. Set `FW_VERSION` in `firmware/platformio.ini` and build with `pio run`.
2. Copy the app image, **not** the factory image, into the firmware directory,
   named after the version:

   ```sh
   sudo install -d -o shopping-list /var/lib/papermono-shopping-list/firmware   # first time only
   sudo install -o shopping-list firmware/.pio/build/papermono/firmware.bin \
      /var/lib/papermono-shopping-list/firmware/1.4.0.bin
   ```

3. Set `SHOPPING_LIST_FIRMWARE_VERSION=1.4.0` in the service's environment and
   restart it. Devices pick it up on their next sync, and the server logs each
   sync's reported version so you can watch them arrive.

To roll back, set the variable to an older version whose `.bin` is still in the
directory. To stop offering updates, unset it. The sha256 and size sent to
devices are computed from the file itself, so there's no catalog to keep in
step. A version with no matching file is logged and skipped, and never breaks
list sync.

## Observability (OpenTelemetry)

The server can export traces, metrics and logs over OTLP/HTTP. It is off unless
`OTEL_EXPORTER_OTLP_ENDPOINT` is set. Install the extra
(`pip install '.[otel]'`; the Docker image already includes it) and configure
it with the standard OpenTelemetry environment variables. For a backend that takes
OTLP directly, e.g. Bronto (EU region):

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=https://ingestion.eu.bronto.io
OTEL_EXPORTER_OTLP_HEADERS=x-bronto-api-key=<ingestion key>   # a secret: keep it in the env file
OTEL_SERVICE_NAME=shopping-list                               # the default
OTEL_RESOURCE_ATTRIBUTES=service.namespace=home
```

A Collector works the same way; only the endpoint and headers change. What's emitted:

- **Traces:** a span per HTTP request (`traceparent` from the caller is honoured), plus a
  `classify item` span around each aisle classification.
- **Metrics** (`shopping_list.*`): items created and purchased, classifier calls by outcome and
  their duration, sync requests and firmware offers/downloads by version, and the last battery,
  Wi-Fi RSSI and free heap each firmware version reported.
- **Logs:** the `shopping_list` logger, correlated to the active trace.

### What gets logged

Every state change and request emits one record on the `shopping_list` logger. The message is
human-readable; the detail is in structured attributes (`extra=`), exported as log fields over OTLP and
appended as `[key=value ...]` to the stdout/journal line.

| Event | Attributes (prefix) |
|-------|---------------------|
| Server start/stop, DB migration | `config.*`, `db.migration` |
| Every request (health, autosuggest and static assets only when they fail) | `http.method`, `http.target`, `http.status_code`, `http.duration_ms`, `client.address`; 4xx is WARNING, 5xx and unhandled exceptions ERROR |
| Category added / renamed / reordered / deleted | `category.id`, `category.name`, `category.old_name`, `category.sort_order`, `category.old_sort_order` |
| Item added / renamed / moved / quantity changed / ticked / deleted, purchased items cleared | `item.*`, `category.id`, `category.old_id`, `items.removed` |
| Aisle classification (incl. skipped and failed) | `item.name`, `category.name`, `classifier.outcome`, `classifier.duration_ms` |
| Device sync, ignored device readings, sync response | `device.*`, `sync.*` |
| Sync schedule changed | `schedule.*`, `schedule.old_*` |
| Firmware offered / download started / unknown version requested | `firmware.*` |

New code that changes state should log the same way: one INFO line, dotted attribute keys, no
formatting of values into the message beyond what a human needs.

Item names are included as span attributes and in log lines. Don't point this at a backend you
wouldn't trust with your shopping list.

## Deploy with systemd

```sh
sudo useradd --system --home /var/lib/papermono-shopping-list shopping-list
sudo git clone https://github.com/<you>/papermono-shopping-list /opt/papermono-shopping-list
cd /opt/papermono-shopping-list/server
sudo python3 -m venv .venv && sudo .venv/bin/pip install .

sudo install -m 600 deploy/shopping-list.env.example /etc/papermono-shopping-list.env
sudoedit /etc/papermono-shopping-list.env

sudo cp deploy/shopping-list.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shopping-list
curl http://localhost:8000/api/health
```

The unit keeps its data in `/var/lib/papermono-shopping-list` (created
automatically) and reads secrets from `/etc/papermono-shopping-list.env`.

To update: `git pull`, `sudo .venv/bin/pip install .`, then
`sudo systemctl restart shopping-list`. The schema is created with
`CREATE TABLE IF NOT EXISTS`, plus a couple of `ALTER TABLE ADD COLUMN`
migrations run on every startup for columns added after the initial release
(see `db.py`); there's no general migration framework beyond that.

## Deploy with Docker

```sh
docker build -t papermono-shopping-list server
docker run -d --name shopping-list -p 8000:8000 -v shopping-list-data:/data papermono-shopping-list
```

The image bundles the Claude Code CLI, but auto-sorting is off by default
(`SHOPPING_LIST_CLASSIFIER=none`). Set `SHOPPING_LIST_CLASSIFIER=claude` and pass
`ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` to turn it on. The CLI keeps its
state under `$HOME`, which is `/data` in the image.

## Security

**The API has no authentication.** Anyone who can reach the port can read and
change the list. That's fine on a home LAN, which is where the device needs to
reach it.

To use the web UI away from home, don't expose the port directly. Publish it
through a reverse proxy or tunnel that authenticates users first, such as
Cloudflare Tunnel with Cloudflare Access, oauth2-proxy, or Caddy or nginx with
an auth module. Keep the device on the plain LAN address. It can't do an
interactive login, and it doesn't need to leave the network.

The web UI sends item and aisle names to the server only. The classifier sends
new item names and your aisle names to Anthropic through the Claude Code CLI;
set `SHOPPING_LIST_CLASSIFIER=none` if you don't want that.

## Backups

Everything is in the one SQLite file. Copy it safely while the server is running
with:

```sh
sqlite3 /var/lib/papermono-shopping-list/shopping.db ".backup shopping-backup.db"
```
