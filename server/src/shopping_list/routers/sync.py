import logging
import re
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request, Response

from .. import firmware, repository, schedule, telemetry
from ..db import get_db
from ..models import SyncResponse

router = APIRouter(prefix="/api", tags=["sync"])
_POWER_RE = re.compile(r"[a-z0-9_=;]{1,120}")
log = logging.getLogger(__name__)


def _reading(raw: str | None, low: int, high: int) -> int | None:
    """A device-reported integer, or None if it's missing, malformed or outside [low, high]."""
    try:
        value = int(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and low <= value <= high else None


@router.get("/sync", response_model=SyncResponse)
def sync(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    x_firmware_version: str | None = Header(default=None),
    # Optional device health from newer firmware (see docs/api.md). Typed as str and checked by hand:
    # a garbled reading must never fail a list sync, so anything implausible is just ignored.
    x_battery_percent: str | None = Header(default=None),
    x_wifi_rssi: str | None = Header(default=None),
    x_free_heap: str | None = Header(default=None),
    # Free-form power history from deep-sleeping firmware, logged verbatim if it looks sane.
    x_power: str | None = Header(default=None),
):
    """Full-state snapshot: everything a client needs to render the list, pre-sorted into aisle
    order, plus the whole catalog for offline autosuggest.

    Deliberately a full replace rather than a delta protocol. The payload is a few KB for a household
    list, so resending everything is simpler and more robust than tracking versions across the
    device's long Wi-Fi-off windows.

    `next_sync_in_s` tells the device how long to wait before its next periodic sync (see schedule.py).
    A device that sends `X-Firmware-Version` may also get a `firmware` offer (see firmware.py).
    """
    battery = _reading(x_battery_percent, 0, 100)
    rssi = _reading(x_wifi_rssi, -127, 0)
    free_heap = _reading(x_free_heap, 0, 1 << 30)
    # Only a short run of plain characters gets into the log; anything else is ignored, never an error.
    power = x_power if x_power and _POWER_RE.fullmatch(x_power) else None
    # Ignoring a bad reading is deliberate (it must never fail a sync), but not silently.
    rejected = [
        header
        for header, raw, value in (
            ("X-Battery-Percent", x_battery_percent, battery),
            ("X-Wifi-Rssi", x_wifi_rssi, rssi),
            ("X-Free-Heap", x_free_heap, free_heap),
            ("X-Power", x_power, power),
        )
        if raw is not None and value is None
    ]
    if rejected:
        log.warning(
            "ignored implausible device readings: %s",
            ", ".join(rejected),
            extra={"sync.rejected_headers": rejected},
        )
    if x_firmware_version is not None:
        log.info(
            "sync from firmware %s (battery %s%%, rssi %s dBm, free heap %s%s)",
            x_firmware_version,
            battery,
            rssi,
            free_heap,
            f", power {power}" if power else "",
            extra={
                "device.firmware_version": x_firmware_version,
                "device.battery_percent": battery,
                "device.wifi_rssi": rssi,
                "device.free_heap": free_heap,
                "device.power": power,
            },
        )
        telemetry.record_device_health(x_firmware_version, battery, rssi, free_heap)
    telemetry.syncs.add(1, {"firmware.version": x_firmware_version or "unknown"})
    offer = firmware.offer_for(request.app.state.settings, x_firmware_version)
    if offer:
        telemetry.firmware_offers.add(1, {"from": x_firmware_version, "to": offer.version})
        log.info(
            "offering firmware %s to device on %s",
            offer.version,
            x_firmware_version,
            extra={
                "firmware.from": x_firmware_version,
                "firmware.to": offer.version,
                "firmware.size": offer.size,
            },
        )
    now = datetime.now(UTC)
    body = SyncResponse(
        categories=repository.list_categories(conn),
        items=repository.list_items(conn),
        catalog=repository.list_catalog(conn),
        next_sync_in_s=schedule.next_sync_delay(
            repository.get_sync_schedule(conn), now, request.app.state.tz
        ),
        synced_at=now.astimezone(request.app.state.tz).strftime("%H:%M"),
        firmware=offer,
    )
    log.info(
        "sync served: %d item(s), next sync in %ds",
        len(body.items),
        body.next_sync_in_s,
        extra={
            "sync.items": len(body.items),
            "sync.categories": len(body.categories),
            "sync.next_sync_in_s": body.next_sync_in_s,
            "sync.firmware_offered": offer is not None,
        },
    )
    # `firmware` must be absent, not null, when there's no offer, but `response_model_exclude_none`
    # would also strip the legitimate nulls elsewhere (an item's category_id, quantity, unit). So
    # serialise by hand; response_model above still documents the shape in /docs.
    exclude = None if body.firmware else {"firmware"}
    return Response(body.model_dump_json(exclude=exclude), media_type="application/json")
