"""All SQL lives here; the routers only translate between HTTP and these functions."""

import json
import logging
import sqlite3
import time

from . import telemetry
from .classifier import ClassificationError, Classifier
from .db import UNCATEGORIZED_NAME, UNCATEGORIZED_SORT_ORDER
from .models import CatalogEntry, CategoryOut, ItemOut
from .schedule import DEFAULT_SCHEDULE, PRESETS_BY_ID, Schedule

log = logging.getLogger(__name__)

# Items and catalog entries whose aisle was deleted have category_id NULL (ON DELETE SET NULL); both
# are reported as belonging to Uncategorized.
_ITEM_SELECT = """
SELECT items.id, items.name, items.category_id, items.purchased,
       items.quantity, items.unit,
       items.created_at, items.updated_at,
       COALESCE(categories.name, :uncategorized) AS category_name
FROM items LEFT JOIN categories ON categories.id = items.category_id
"""

_CATALOG_SELECT = """
SELECT catalog.name, catalog.category_id,
       COALESCE(categories.name, :uncategorized) AS category_name
FROM catalog LEFT JOIN categories ON categories.id = catalog.category_id
"""


# ---------------------------------------------------------------------------- categories


def _category(row: sqlite3.Row) -> CategoryOut:
    return CategoryOut(id=row["id"], name=row["name"], sort_order=row["sort_order"])


def list_categories(conn: sqlite3.Connection) -> list[CategoryOut]:
    rows = conn.execute("SELECT id, name, sort_order FROM categories ORDER BY sort_order, name")
    return [_category(r) for r in rows]


def get_category(conn: sqlite3.Connection, category_id: int) -> CategoryOut | None:
    row = conn.execute("SELECT id, name, sort_order FROM categories WHERE id = ?", (category_id,)).fetchone()
    return _category(row) if row else None


def find_category_by_name(conn: sqlite3.Connection, name: str) -> CategoryOut | None:
    row = conn.execute(
        "SELECT id, name, sort_order FROM categories WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    return _category(row) if row else None


def uncategorized_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT id FROM categories WHERE name = ?", (UNCATEGORIZED_NAME,)).fetchone()["id"]


def create_category(conn: sqlite3.Connection, name: str) -> CategoryOut:
    """Append a new aisle to the end of the walk, just ahead of Uncategorized."""
    cur = conn.execute(
        "INSERT INTO categories (name, sort_order) "
        "SELECT ?, COALESCE(MAX(sort_order), -1) + 1 FROM categories WHERE name != ?",
        (name, UNCATEGORIZED_NAME),
    )
    created = get_category(conn, cur.lastrowid)
    log.info(
        "added category %r (id %d, sort_order %d)",
        created.name,
        created.id,
        created.sort_order,
        extra={
            "category.id": created.id,
            "category.name": created.name,
            "category.sort_order": created.sort_order,
        },
    )
    return created


def update_category(conn: sqlite3.Connection, category_id: int, name: str, sort_order: int) -> CategoryOut:
    conn.execute(
        "UPDATE categories SET name = ?, sort_order = ? WHERE id = ?", (name, sort_order, category_id)
    )
    return get_category(conn, category_id)


def delete_category(conn: sqlite3.Connection, category_id: int) -> None:
    # Items and catalog entries in this aisle fall back to NULL via ON DELETE SET NULL, and are
    # read back as Uncategorized, so nothing else needs touching here.
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))


# ---------------------------------------------------------------------------- items


def _item(row: sqlite3.Row) -> ItemOut:
    return ItemOut(
        id=row["id"],
        name=row["name"],
        category_id=row["category_id"],
        category_name=row["category_name"],
        purchased=bool(row["purchased"]),
        quantity=row["quantity"],
        unit=row["unit"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_items(conn: sqlite3.Connection) -> list[ItemOut]:
    """Every item, grouped in aisle (walking) order, then alphabetically within an aisle.

    Both clients render in exactly this order, so it's part of the API contract.
    """
    # Orphaned items (NULL category) sort with the real Uncategorized bucket, so the two form one
    # contiguous, alphabetised group.
    rows = conn.execute(
        _ITEM_SELECT + " ORDER BY COALESCE(categories.sort_order, :last), items.name",
        {"uncategorized": UNCATEGORIZED_NAME, "last": UNCATEGORIZED_SORT_ORDER},
    )
    return [_item(r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> ItemOut | None:
    row = conn.execute(
        _ITEM_SELECT + " WHERE items.id = :id", {"uncategorized": UNCATEGORIZED_NAME, "id": item_id}
    ).fetchone()
    return _item(row) if row else None


def create_item(
    conn: sqlite3.Connection,
    name: str,
    category_id: int,
    quantity: float | None = None,
    unit: str | None = None,
) -> ItemOut:
    cur = conn.execute(
        "INSERT INTO items (name, category_id, quantity, unit) VALUES (?, ?, ?, ?)",
        (name, category_id, quantity, unit),
    )
    return get_item(conn, cur.lastrowid)


def update_item(
    conn: sqlite3.Connection,
    item_id: int,
    name: str,
    category_id: int | None,
    purchased: bool,
    quantity: float | None,
    unit: str | None,
) -> ItemOut:
    conn.execute(
        "UPDATE items SET name = ?, category_id = ?, purchased = ?, quantity = ?, unit = ?, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
        (name, category_id, int(purchased), quantity, unit, item_id),
    )
    return get_item(conn, item_id)


def delete_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))


def clear_purchased(conn: sqlite3.Connection) -> int:
    """Delete every ticked-off item and return how many there were."""
    return conn.execute("DELETE FROM items WHERE purchased = 1").rowcount


# ---------------------------------------------------------------------------- catalog


def _catalog_entry(row: sqlite3.Row) -> CatalogEntry:
    return CatalogEntry(name=row["name"], category_id=row["category_id"], category_name=row["category_name"])


def list_catalog(conn: sqlite3.Connection) -> list[CatalogEntry]:
    rows = conn.execute(_CATALOG_SELECT + " ORDER BY catalog.name", {"uncategorized": UNCATEGORIZED_NAME})
    return [_catalog_entry(r) for r in rows]


def suggest(conn: sqlite3.Connection, query: str, limit: int) -> list[CatalogEntry]:
    """Case-insensitive substring match, with prefix matches ranked first."""
    rows = conn.execute(
        _CATALOG_SELECT + " WHERE catalog.name LIKE :contains COLLATE NOCASE"
        " ORDER BY (catalog.name LIKE :prefix COLLATE NOCASE) DESC, catalog.name LIMIT :limit",
        {
            "uncategorized": UNCATEGORIZED_NAME,
            "contains": f"%{query}%",
            "prefix": f"{query}%",
            "limit": limit,
        },
    )
    return [_catalog_entry(r) for r in rows]


def remember(conn: sqlite3.Connection, item_name: str, category_id: int) -> None:
    """Record (or correct) which aisle an item name belongs to."""
    conn.execute(
        "INSERT INTO catalog (name, category_id) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET category_id = excluded.category_id",
        (item_name, category_id),
    )


def resolve_category_id(
    conn: sqlite3.Connection, item_name: str, category_id: int | None, classifier: Classifier
) -> int:
    """Decide which aisle a newly added item goes in, and remember the answer.

    Priority: an explicit category_id from the caller (e.g. the device already knew it from its
    cached catalog) > an existing catalog entry for this name > the classifier > Uncategorized.
    """
    if category_id is None:
        category_id = _lookup_catalog(conn, item_name)
    if category_id is None:
        category_id = _classify(conn, item_name, classifier)
    remember(conn, item_name, category_id)
    return category_id


def _lookup_catalog(conn: sqlite3.Connection, item_name: str) -> int | None:
    # A catalog row whose aisle was since deleted has a NULL category_id. That has to count as a
    # miss, or the name would be stuck in Uncategorized forever instead of being re-classified.
    row = conn.execute(
        "SELECT category_id FROM catalog WHERE name = ? COLLATE NOCASE AND category_id IS NOT NULL",
        (item_name,),
    ).fetchone()
    return row["category_id"] if row else None


def _classify(conn: sqlite3.Connection, item_name: str, classifier: Classifier) -> int:
    aisles = [c for c in list_categories(conn) if c.name != UNCATEGORIZED_NAME]
    if not aisles:
        # Nothing to choose between yet; don't let the classifier invent the first aisle.
        telemetry.classifications.add(1, {"outcome": "skipped"})
        log.info(
            "skipped classifying %r: no aisles exist yet",
            item_name,
            extra={"item.name": item_name, "classifier.outcome": "skipped"},
        )
        return uncategorized_id(conn)

    with telemetry.tracer.start_as_current_span("classify item") as span:
        span.set_attribute("item.name", item_name)
        start = time.perf_counter()
        try:
            result = classifier.classify(item_name, [c.name for c in aisles])
        except ClassificationError as exc:
            outcome = "failed"
            span.record_exception(exc)
            log.warning(
                "classifying %r failed: %s",
                item_name,
                exc,
                extra={
                    "item.name": item_name,
                    "classifier.outcome": "failed",
                    "classifier.duration_ms": round((time.perf_counter() - start) * 1000),
                },
            )
            category_id = uncategorized_id(conn)
        else:
            existing = find_category_by_name(conn, result.category_name)
            outcome = "existing" if existing is not None else "new_aisle"
            category_id = (
                existing.id if existing is not None else create_category(conn, result.category_name).id
            )
            span.set_attribute("category.name", result.category_name)
            log.info(
                "classified %r -> %r (%s)",
                item_name,
                result.category_name,
                outcome,
                extra={
                    "item.name": item_name,
                    "category.name": result.category_name,
                    "classifier.outcome": outcome,
                    "classifier.duration_ms": round((time.perf_counter() - start) * 1000),
                },
            )
        span.set_attribute("classifier.outcome", outcome)
        elapsed = time.perf_counter() - start
    telemetry.classifications.add(1, {"outcome": outcome})
    telemetry.classify_duration.record(elapsed, {"outcome": outcome})
    return category_id


_SYNC_SCHEDULE_KEY = "sync_schedule"


def get_sync_schedule(conn: sqlite3.Connection) -> Schedule:
    """The saved sync schedule, or the default if none is saved or the stored value is unusable
    (say, a preset that a later version removed) - a bad row must never stop the list syncing."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (_SYNC_SCHEDULE_KEY,)).fetchone()
    if row is None:
        return DEFAULT_SCHEDULE
    try:
        data = json.loads(row["value"])
        schedule = Schedule(
            weekday=data["weekday"], weekend=data["weekend"], weekend_same=bool(data["weekend_same"])
        )
    except (ValueError, KeyError, TypeError):
        log.warning("ignoring unreadable stored sync schedule")
        return DEFAULT_SCHEDULE
    if schedule.weekday not in PRESETS_BY_ID or schedule.weekend not in PRESETS_BY_ID:
        log.warning("stored sync schedule names an unknown preset; using the default")
        return DEFAULT_SCHEDULE
    return schedule


def set_sync_schedule(conn: sqlite3.Connection, schedule: Schedule) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (
            _SYNC_SCHEDULE_KEY,
            json.dumps(
                {
                    "weekday": schedule.weekday,
                    "weekend": schedule.weekend,
                    "weekend_same": schedule.weekend_same,
                }
            ),
        ),
    )
