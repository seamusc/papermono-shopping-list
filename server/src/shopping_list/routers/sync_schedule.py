import logging
import sqlite3

from fastapi import APIRouter, Depends

from .. import repository
from ..db import get_db
from ..models import PresetOut, SyncScheduleIn, SyncScheduleOut, TimeWindow
from ..schedule import PRESETS, Schedule

router = APIRouter(prefix="/api", tags=["sync schedule"])
log = logging.getLogger(__name__)


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _out(schedule: Schedule) -> SyncScheduleOut:
    return SyncScheduleOut(
        weekday=schedule.weekday,
        weekend=schedule.weekend,
        weekend_same=schedule.weekend_same,
        presets=[
            PresetOut(
                id=p.id,
                name=p.name,
                description=p.description,
                interval_minutes=p.interval_minutes,
                windows=[TimeWindow(start=_hhmm(s), end=_hhmm(e)) for s, e in p.windows],
                syncs_per_day=p.syncs_per_day(),
            )
            for p in PRESETS
        ],
    )


@router.get("/sync-schedule", response_model=SyncScheduleOut)
def get_sync_schedule(conn: sqlite3.Connection = Depends(get_db)):
    """The saved schedule plus every preset that can be chosen for it."""
    return _out(repository.get_sync_schedule(conn))


@router.put("/sync-schedule", response_model=SyncScheduleOut)
def put_sync_schedule(body: SyncScheduleIn, conn: sqlite3.Connection = Depends(get_db)):
    """Replace the schedule. Takes effect on each device's next sync: that is when it is told how
    long to wait, so a device already asleep for an hour keeps its old wake time until then."""
    previous = repository.get_sync_schedule(conn)
    schedule = Schedule(weekday=body.weekday, weekend=body.weekend, weekend_same=body.weekend_same)
    repository.set_sync_schedule(conn, schedule)
    log.info(
        "sync schedule changed: weekday %s -> %s, weekend %s -> %s (weekend_same %s)",
        previous.weekday,
        schedule.weekday,
        previous.weekend,
        schedule.weekend,
        schedule.weekend_same,
        extra={
            "schedule.weekday": schedule.weekday,
            "schedule.old_weekday": previous.weekday,
            "schedule.weekend": schedule.weekend,
            "schedule.old_weekend": previous.weekend,
            "schedule.weekend_same": schedule.weekend_same,
        },
    )
    return _out(schedule)
