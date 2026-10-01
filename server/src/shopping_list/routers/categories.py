import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from .. import repository
from ..db import UNCATEGORIZED_NAME, get_db
from ..models import CategoryCreate, CategoryOut, CategoryUpdate

router = APIRouter(prefix="/api/categories", tags=["categories"])
log = logging.getLogger(__name__)


def _get_or_404(conn: sqlite3.Connection, category_id: int) -> CategoryOut:
    category = repository.get_category(conn, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    return category


@router.get("", response_model=list[CategoryOut])
def list_categories(conn: sqlite3.Connection = Depends(get_db)):
    return repository.list_categories(conn)


@router.post("", response_model=CategoryOut, status_code=201)
def create_category(body: CategoryCreate, conn: sqlite3.Connection = Depends(get_db)):
    if repository.find_category_by_name(conn, body.name) is not None:
        raise HTTPException(status_code=409, detail="Category already exists")
    return repository.create_category(conn, body.name)


@router.patch("/{category_id}", response_model=CategoryOut)
def update_category(category_id: int, body: CategoryUpdate, conn: sqlite3.Connection = Depends(get_db)):
    category = _get_or_404(conn, category_id)
    if category.name == UNCATEGORIZED_NAME and body.name not in (None, UNCATEGORIZED_NAME):
        raise HTTPException(status_code=400, detail="Cannot rename the Uncategorized bucket")
    if body.name is not None:
        clash = repository.find_category_by_name(conn, body.name)
        if clash is not None and clash.id != category_id:
            raise HTTPException(status_code=409, detail="Category already exists")
    updated = repository.update_category(
        conn,
        category_id,
        name=body.name if body.name is not None else category.name,
        sort_order=body.sort_order if body.sort_order is not None else category.sort_order,
    )
    if updated.name != category.name:
        log.info(
            "renamed category %r -> %r (id %d)",
            category.name,
            updated.name,
            category_id,
            extra={
                "category.id": category_id,
                "category.name": updated.name,
                "category.old_name": category.name,
            },
        )
    if updated.sort_order != category.sort_order:
        log.info(
            "reordered category %r (id %d): sort_order %d -> %d",
            updated.name,
            category_id,
            category.sort_order,
            updated.sort_order,
            extra={
                "category.id": category_id,
                "category.name": updated.name,
                "category.old_sort_order": category.sort_order,
                "category.sort_order": updated.sort_order,
            },
        )
    return updated


@router.delete("/{category_id}", status_code=204)
def delete_category(category_id: int, conn: sqlite3.Connection = Depends(get_db)):
    category = _get_or_404(conn, category_id)
    if category.name == UNCATEGORIZED_NAME:
        raise HTTPException(status_code=400, detail="Cannot delete the Uncategorized bucket")
    repository.delete_category(conn, category_id)
    log.info(
        "deleted category %r (id %d)",
        category.name,
        category_id,
        extra={"category.id": category_id, "category.name": category.name},
    )
