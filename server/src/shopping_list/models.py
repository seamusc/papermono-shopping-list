"""Request/response bodies. These are the wire format the firmware parses too (see
firmware/src/sync/model_json.h), so field names are part of the device contract."""

from pydantic import BaseModel, Field, field_validator

from .schedule import PRESETS_BY_ID


class CategoryOut(BaseModel):
    id: int
    name: str
    sort_order: int


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    sort_order: int | None = None


class ItemOut(BaseModel):
    id: int
    name: str
    category_id: int | None
    category_name: str
    purchased: bool
    quantity: float | None
    unit: str | None
    created_at: str
    updated_at: str


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # Omit to let the server pick an aisle (catalog lookup, then the classifier).
    category_id: int | None = None
    quantity: float | None = None
    unit: str | None = Field(default=None, max_length=32)


class ItemUpdate(BaseModel):
    purchased: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=128)
    category_id: int | None = None
    # Unlike the fields above, None is a legitimate target value for quantity/unit (it means
    # "clear it"), not just "not sent" - callers must check `model_fields_set` rather than
    # `is None` to tell the two apart. See routers/items.py.
    quantity: float | None = None
    unit: str | None = Field(default=None, max_length=32)


class CatalogEntry(BaseModel):
    name: str
    category_id: int | None
    category_name: str


class CatalogUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    category_id: int | None = None


class FirmwareOffer(BaseModel):
    version: str
    # Relative to the server address the device already has.
    url: str
    sha256: str
    size: int


class SyncResponse(BaseModel):
    categories: list[CategoryOut]
    items: list[ItemOut]
    catalog: list[CatalogEntry]
    # Seconds the device should wait before its next periodic sync; see schedule.py.
    next_sync_in_s: int
    # Local time of day the server answered ("14:05"), in the configured timezone. The device has no
    # clock and its e-paper header can't tick, so it shows this fixed time instead of an age.
    synced_at: str
    # Left out of the JSON entirely (not null) unless the device should update; see routers/sync.py.
    firmware: FirmwareOffer | None = None


class SyncScheduleIn(BaseModel):
    # Preset ids, see schedule.PRESETS.
    weekday: str
    weekend: str
    weekend_same: bool = False

    @field_validator("weekday", "weekend")
    @classmethod
    def _known_preset(cls, value: str) -> str:
        if value not in PRESETS_BY_ID:
            raise ValueError(f"unknown preset {value!r}; expected one of {sorted(PRESETS_BY_ID)}")
        return value


class TimeWindow(BaseModel):
    start: str  # "HH:MM"
    end: str  # "HH:MM", or "24:00" for end of day


class PresetOut(BaseModel):
    id: str
    name: str
    description: str
    interval_minutes: int
    windows: list[TimeWindow]
    syncs_per_day: int


class SyncScheduleOut(SyncScheduleIn):
    presets: list[PresetOut]
