import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import repository
from ..db import get_db
from ..models import CatalogEntry, CatalogUpdate

router = APIRouter(prefix="/api/catalog", tags=["catalog"])
log = logging.getLogger(__name__)


@router.get("", response_model=list[CatalogEntry])
def list_catalog(conn: sqlite3.Connection = Depends(get_db)):
    """The whole catalog, alphabetical. Lets a client review it for typos and stale entries."""
    return repository.list_catalog(conn)


@router.get("/suggest", response_model=list[CatalogEntry])
def suggest(
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=8, ge=1, le=25),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Live autosuggest for the web UI.

    The device doesn't call this: it filters the copy of the catalog it got from its last
    /api/sync, so typing works while its Wi-Fi is off between syncs.
    """
    if not q.strip():
        return []
    return repository.suggest(conn, q.strip(), limit)


def _require_entry(conn: sqlite3.Connection, name: str) -> CatalogEntry:
    entry = repository.get_catalog_entry(conn, name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Catalog entry not found")
    return entry


# The name is the key and may contain characters that are awkward in a path ("/", "%"), so these
# take it as a query parameter.
@router.patch("", response_model=CatalogEntry)
def update_entry(
    body: CatalogUpdate,
    name: str = Query(min_length=1, max_length=128),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Fix a misspelled name and/or change the aisle. Items already on the list keep their name."""
    entry = _require_entry(conn, name)

    new_name = entry.name
    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="name cannot be blank")
        clash = conn.execute(
            "SELECT name FROM catalog WHERE name = ? COLLATE NOCASE AND name != ?", (new_name, entry.name)
        ).fetchone()
        if clash:
            raise HTTPException(status_code=409, detail=f"Catalog already has {clash['name']!r}")

    category_id = entry.category_id
    if body.category_id is not None:
        if repository.get_category(conn, body.category_id) is None:
            raise HTTPException(status_code=400, detail="Unknown category_id")
        category_id = body.category_id

    updated = repository.update_catalog_entry(conn, entry.name, new_name, category_id)
    log.info(
        "updated catalog entry %r -> %r (%s)",
        entry.name,
        updated.name,
        updated.category_name,
        extra={
            "catalog.old_name": entry.name,
            "catalog.name": updated.name,
            "category.id": updated.category_id,
        },
    )
    return updated


@router.delete("", status_code=204)
def delete_entry(name: str = Query(min_length=1, max_length=128), conn: sqlite3.Connection = Depends(get_db)):
    """Forget a name so it stops autosuggesting. Items already on the list are untouched."""
    entry = _require_entry(conn, name)
    repository.delete_catalog_entry(conn, entry.name)
    log.info("deleted catalog entry %r", entry.name, extra={"catalog.name": entry.name})
