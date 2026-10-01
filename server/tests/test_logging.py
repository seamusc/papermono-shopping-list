"""The server's structured logs: each state change emits one INFO record carrying `extra=` attributes."""

import logging

import pytest


@pytest.fixture
def records(caplog):
    with caplog.at_level(logging.INFO, logger="shopping_list"):
        yield lambda: caplog.records


def _find(records, text):
    matches = [r for r in records() if text in r.getMessage()]
    assert matches, f"no log containing {text!r}; got {[r.getMessage() for r in records()]}"
    return matches[-1]


def test_category_lifecycle_is_logged(client, records):
    cat = client.post("/api/categories", json={"name": "Fridge"}).json()
    assert _find(records, "added category 'Fridge'").__dict__["category.id"] == cat["id"]

    client.patch(f"/api/categories/{cat['id']}", json={"sort_order": 5})
    reorder = _find(records, "reordered category 'Fridge'")
    assert reorder.__dict__["category.sort_order"] == 5
    assert "category.old_sort_order" in reorder.__dict__

    client.patch(f"/api/categories/{cat['id']}", json={"name": "Chilled"})
    assert _find(records, "renamed category 'Fridge' -> 'Chilled'").__dict__["category.old_name"] == "Fridge"

    client.delete(f"/api/categories/{cat['id']}")
    _find(records, "deleted category 'Chilled'")


def test_item_changes_are_logged(client, records):
    a = client.post("/api/categories", json={"name": "Freezer"}).json()
    b = client.post("/api/categories", json={"name": "Fridge"}).json()
    item = client.post("/api/items", json={"name": "Coleslaw", "category_id": a["id"]}).json()

    client.patch(f"/api/items/{item['id']}", json={"category_id": b["id"]})
    moved = _find(records, "moved item 'Coleslaw'")
    assert moved.__dict__["category.old_id"] == a["id"]
    assert moved.__dict__["category.id"] == b["id"]

    client.patch(f"/api/items/{item['id']}", json={"name": "Slaw", "quantity": 2, "unit": "kg"})
    _find(records, "renamed item 'Coleslaw' -> 'Slaw'")
    assert _find(records, "changed quantity of item 'Slaw'").__dict__["item.quantity"] == 2

    client.patch(f"/api/items/{item['id']}", json={"purchased": True})
    client.post("/api/items/clear-purchased")
    assert _find(records, "cleared 1 purchased").__dict__["items.removed"] == 1

    other = client.post("/api/items", json={"name": "Milk", "category_id": b["id"]}).json()
    client.delete(f"/api/items/{other['id']}")
    _find(records, "deleted item 'Milk'")


def test_unchanged_item_patch_logs_no_move(client, records):
    cat = client.post("/api/categories", json={"name": "Fridge"}).json()
    item = client.post("/api/items", json={"name": "Milk", "category_id": cat["id"]}).json()
    client.patch(f"/api/items/{item['id']}", json={"category_id": cat["id"]})
    assert not [r for r in records() if "moved item" in r.getMessage()]


def test_sync_schedule_change_is_logged(client, records):
    client.put("/api/sync-schedule", json={"weekday": "work", "weekend": "always", "weekend_same": True})
    rec = _find(records, "sync schedule changed")
    assert rec.__dict__["schedule.weekday"] == "work"


def test_sync_logs_response_and_rejected_readings(client, records):
    client.get("/api/sync", headers={"X-Firmware-Version": "1.6.0", "X-Battery-Percent": "500"})
    assert _find(records, "ignored implausible device readings").__dict__["sync.rejected_headers"] == [
        "X-Battery-Percent"
    ]
    served = _find(records, "sync served")
    assert served.__dict__["sync.next_sync_in_s"] > 0


def test_requests_are_logged_but_quiet_paths_only_on_failure(client, records):
    client.get("/api/health")
    client.get("/api/catalog/suggest", params={"q": "mi"})
    assert not [r for r in records() if r.getMessage().startswith("GET /api/health")]
    client.post("/api/categories", json={"name": "A"})
    client.post("/api/categories", json={"name": "A"})  # 409
    ok = _find(records, "POST /api/categories -> 201")
    assert ok.__dict__["http.status_code"] == 201
    conflict = _find(records, "POST /api/categories -> 409")
    assert conflict.levelno == logging.WARNING


def test_missing_firmware_download_is_logged(client, records):
    client.get("/api/firmware/9.9.9.bin")
    assert _find(records, "unknown version").__dict__["firmware.version"] == "9.9.9"
