import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from shopping_list.schedule import (
    DEFAULT_SCHEDULE,
    MAX_DELAY_S,
    MIN_DELAY_S,
    PRESETS_BY_ID,
    Schedule,
    next_sync_delay,
    resolve_timezone,
)

DUBLIN = ZoneInfo("Europe/Dublin")

WORK = Schedule(weekday="work", weekend="work", weekend_same=True)
SPLIT = Schedule(weekday="saver", weekend="always", weekend_same=False)


def at(y, mo, d, h, mi, s=0):
    """A moment in Dublin local time."""
    return datetime(y, mo, d, h, mi, s, tzinfo=DUBLIN)


# 2026-09-29 is a Tuesday, 2026-10-03 a Saturday.


def test_inside_a_window_the_next_sync_is_one_interval_away():
    assert next_sync_delay(WORK, at(2026, 9, 29, 17, 0), DUBLIN) == 60 * 60


def test_sync_seconds_are_counted_from_the_real_moment_not_the_minute():
    assert next_sync_delay(WORK, at(2026, 9, 29, 17, 0, 20), DUBLIN) == 60 * 60 - 20


def test_last_sync_in_a_window_waits_for_the_next_window():
    # 08:00 is the last hourly sync of 07:00-09:00; the next window opens at 17:00.
    assert next_sync_delay(WORK, at(2026, 9, 29, 8, 0), DUBLIN) == 9 * 60 * 60


def test_before_a_window_waits_until_it_opens():
    assert next_sync_delay(WORK, at(2026, 9, 29, 6, 0), DUBLIN) == 60 * 60


def test_overnight_gap_is_capped_so_the_device_still_checks_in():
    # 21:00 is the last sync of the evening window; 07:00 is ten hours away, under the 12 h cap.
    assert next_sync_delay(WORK, at(2026, 9, 29, 21, 0), DUBLIN) == 10 * 60 * 60
    # A schedule with a longer gap gets capped rather than sleeping the full time.
    long_gap = Schedule(weekday="saver", weekend="saver", weekend_same=True)
    assert next_sync_delay(long_gap, at(2026, 9, 29, 19, 0), DUBLIN) == 12 * 60 * 60


def test_weekend_profile_applies_on_saturday():
    # Weekdays hourly; weekends every 15 minutes.
    assert next_sync_delay(SPLIT, at(2026, 9, 29, 10, 0), DUBLIN) == 60 * 60
    assert next_sync_delay(SPLIT, at(2026, 10, 3, 10, 0), DUBLIN) == 15 * 60


def test_weekend_same_ignores_the_weekend_preset():
    same = Schedule(weekday="saver", weekend="always", weekend_same=True)
    assert next_sync_delay(same, at(2026, 10, 3, 10, 0), DUBLIN) == 60 * 60


def test_midnight_hands_over_to_the_next_days_profile():
    # Friday 23:50 with "always" on weekends only: weekday preset is quiet, so Saturday 00:00 wins.
    friday_quiet = Schedule(weekday="saver", weekend="always", weekend_same=False)
    assert next_sync_delay(friday_quiet, at(2026, 10, 2, 23, 50), DUBLIN) == 10 * 60


def test_delay_never_below_the_floor():
    # Two seconds before the 07:00 window opens.
    assert next_sync_delay(WORK, at(2026, 9, 29, 6, 59, 58), DUBLIN) >= MIN_DELAY_S


def test_clock_times_are_read_in_the_configured_timezone():
    # 06:00 UTC is 07:00 in Dublin (BST), inside the window: next sync is one hour on.
    now = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)
    assert next_sync_delay(WORK, now, DUBLIN) == 60 * 60
    # In UTC the same instant is 06:00, before the window opens.
    assert next_sync_delay(WORK, now, UTC) == 60 * 60
    now = datetime(2026, 9, 29, 7, 30, tzinfo=UTC)  # 08:30 Dublin, 07:30 UTC
    assert next_sync_delay(WORK, now, DUBLIN) == 9 * 60 * 60 - 30 * 60


def test_daylight_saving_change_does_not_shift_the_wake_time():
    # Clocks go back on Sunday 2026-10-25 at 02:00 BST -> 01:00 GMT. Sunday's 22:00 sync (last of the
    # evening window at 21:00) should still be told to wake at 07:00 local, 10 hours later.
    assert next_sync_delay(WORK, at(2026, 10, 25, 21, 0), DUBLIN) == 10 * 60 * 60


def test_default_schedule_is_quiet_overnight():
    delay = next_sync_delay(DEFAULT_SCHEDULE, at(2026, 9, 29, 21, 30), DUBLIN)
    assert delay == 9 * 60 * 60 + 30 * 60  # 21:30 -> 07:00


@pytest.mark.parametrize(
    ("preset", "expected"),
    [("work", 7), ("day", 30), ("saver", 12), ("always", 96)],
)
def test_syncs_per_day(preset, expected):
    assert PRESETS_BY_ID[preset].syncs_per_day() == expected


def test_resolve_timezone():
    assert resolve_timezone("") is None
    assert resolve_timezone("Europe/Dublin") == DUBLIN
    with pytest.raises(ValueError, match="not a known timezone"):
        resolve_timezone("Mars/Olympus")


def test_max_delay_is_a_sensible_bound():
    assert MIN_DELAY_S < MAX_DELAY_S == 12 * 60 * 60


# --- HTTP ---


def test_schedule_defaults_to_daytime_for_both(client):
    body = client.get("/api/sync-schedule").json()
    assert body["weekday"] == "day"
    assert body["weekend"] == "day"
    assert body["weekend_same"] is True
    ids = [p["id"] for p in body["presets"]]
    assert ids == ["work", "day", "saver", "always"]


def test_presets_describe_their_windows(client):
    work = next(p for p in client.get("/api/sync-schedule").json()["presets"] if p["id"] == "work")
    assert work["interval_minutes"] == 60
    assert work["windows"] == [{"start": "07:00", "end": "09:00"}, {"start": "17:00", "end": "22:00"}]
    assert work["syncs_per_day"] == 7
    always = next(p for p in client.get("/api/sync-schedule").json()["presets"] if p["id"] == "always")
    assert always["windows"] == [{"start": "00:00", "end": "24:00"}]


def test_put_schedule_persists(client):
    resp = client.put(
        "/api/sync-schedule", json={"weekday": "work", "weekend": "always", "weekend_same": False}
    )
    assert resp.status_code == 200
    body = client.get("/api/sync-schedule").json()
    assert (body["weekday"], body["weekend"], body["weekend_same"]) == ("work", "always", False)


def test_put_schedule_keeps_the_weekend_choice_when_same_is_ticked(client):
    client.put("/api/sync-schedule", json={"weekday": "work", "weekend": "always", "weekend_same": True})
    body = client.get("/api/sync-schedule").json()
    assert body["weekend"] == "always"
    assert body["weekend_same"] is True


def test_put_schedule_rejects_unknown_preset(client):
    resp = client.put("/api/sync-schedule", json={"weekday": "nope", "weekend": "day"})
    assert resp.status_code == 422
    assert client.get("/api/sync-schedule").json()["weekday"] == "day"


def test_unreadable_stored_schedule_falls_back_to_default(client):
    db = client.app.state.settings.db_path
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO settings (key, value) VALUES ('sync_schedule', 'not json')")
    conn.commit()
    conn.close()
    assert client.get("/api/sync-schedule").json()["weekday"] == "day"
    assert client.get("/api/sync").status_code == 200


def test_sync_tells_the_device_when_to_come_back(client):
    client.put("/api/sync-schedule", json={"weekday": "always", "weekend": "always", "weekend_same": True})
    delay = client.get("/api/sync").json()["next_sync_in_s"]
    assert 14 * 60 <= delay <= 15 * 60


def test_sync_reports_the_local_time_of_day(client):
    import re

    assert re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", client.get("/api/sync").json()["synced_at"])


def test_sync_logs_device_power_history(client, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="shopping_list"):
        resp = client.get(
            "/api/sync",
            headers={"X-Firmware-Version": "1.5.1", "X-Power": "boot=touch;timer=2;awake_ms=31400"},
        )
    assert resp.status_code == 200
    assert "power boot=touch;timer=2;awake_ms=31400" in caplog.text


def test_sync_ignores_a_garbled_power_header(client, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="shopping_list"):
        resp = client.get("/api/sync", headers={"X-Firmware-Version": "1.5.1", "X-Power": "boot=<script>"})
    assert resp.status_code == 200
    assert "power " not in caplog.text


def test_sync_delay_is_within_bounds_for_every_preset(client):
    for preset in PRESETS_BY_ID:
        client.put("/api/sync-schedule", json={"weekday": preset, "weekend": preset, "weekend_same": True})
        delay = client.get("/api/sync").json()["next_sync_in_s"]
        assert MIN_DELAY_S <= delay <= MAX_DELAY_S


def test_schedule_page_is_served_and_linked_from_the_list(client):
    page = client.get("/schedule")
    assert page.status_code == 200
    assert "Sync schedule" in page.text
    assert 'href="/schedule"' in client.get("/").text
    assert client.get("/static/schedule.js").status_code == 200
