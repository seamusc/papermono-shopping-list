import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from opentelemetry import trace

from .. import repository, telemetry
from ..classifier import Classifier
from ..db import get_db
from ..models import ItemCreate, ItemOut, ItemUpdate

router = APIRouter(prefix="/api/items", tags=["items"])
log = logging.getLogger(__name__)


def get_classifier(request: Request) -> Classifier:
    return request.app.state.classifier


def _clean_name(raw: str) -> str:
    # Pydantic's min_length alone lets an all-whitespace name through.
    name = raw.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name cannot be blank")
    return name


def _require_category(conn: sqlite3.Connection, category_id: int) -> None:
    if repository.get_category(conn, category_id) is None:
        raise HTTPException(status_code=400, detail="Unknown category_id")


def _normalize_quantity(quantity: float | None, unit: str | None) -> tuple[float | None, str | None]:
    """A unit only means anything alongside a quantity ("g" on its own is meaningless) - reject
    that combination rather than silently storing a confusing state. An empty/whitespace-only unit
    is treated as "no unit", same as never providing one."""
    unit = unit.strip() if unit else None
    if unit == "":
        unit = None
    if unit is not None and quantity is None:
        raise HTTPException(status_code=400, detail="unit requires a quantity")
    if quantity is not None and quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be greater than 0")
    return quantity, unit


@router.post("", response_model=ItemOut, status_code=201)
def create_item(
    body: ItemCreate,
    conn: sqlite3.Connection = Depends(get_db),
    classifier: Classifier = Depends(get_classifier),
):
    name = _clean_name(body.name)
    if body.category_id is not None:
        _require_category(conn, body.category_id)
    quantity, unit = _normalize_quantity(body.quantity, body.unit)
    category_id = repository.resolve_category_id(conn, name, body.category_id, classifier)
    item = repository.create_item(conn, name, category_id, quantity, unit)
    telemetry.items_created.add(1)
    trace.get_current_span().set_attribute("item.name", name)
    log.info(
        "added item %r (id %d)",
        name,
        item.id,
        extra={
            "item.id": item.id,
            "item.name": name,
            "category.id": item.category_id,
            "category.name": item.category_name,
            "item.quantity": quantity,
            "item.unit": unit,
        },
    )
    return item


@router.patch("/{item_id}", response_model=ItemOut)
def update_item(item_id: int, body: ItemUpdate, conn: sqlite3.Connection = Depends(get_db)):
    item = repository.get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    name = item.name if body.name is None else _clean_name(body.name)
    if body.category_id is not None:
        _require_category(conn, body.category_id)
        # A manual re-file is the only way to correct a bad auto-classification, so it also updates
        # the catalog; otherwise the next time this name is added it would go straight back to the
        # wrong aisle.
        repository.remember(conn, name, body.category_id)

    # Unlike the fields above, `None` is a legitimate target value for quantity/unit (it means
    # "clear it") - `model_fields_set` is the only way to tell "omitted" from "sent null".
    fields_set = body.model_fields_set
    quantity = item.quantity if "quantity" not in fields_set else body.quantity
    unit = item.unit if "unit" not in fields_set else body.unit
    quantity, unit = _normalize_quantity(quantity, unit)

    if body.purchased is not None and body.purchased != item.purchased:
        telemetry.items_purchased.add(1, {"purchased": body.purchased})
        log.info(
            "item %r marked %s",
            name,
            "purchased" if body.purchased else "not purchased",
            extra={"item.id": item_id, "item.name": name, "item.purchased": body.purchased},
        )
    trace.get_current_span().set_attribute("item.name", name)

    new_category_id = item.category_id if body.category_id is None else body.category_id
    if new_category_id != item.category_id:
        log.info(
            "moved item %r (id %d): category %s -> %d",
            name,
            item_id,
            item.category_id,
            new_category_id,
            extra={
                "item.id": item_id,
                "item.name": name,
                "category.old_id": item.category_id,
                "category.id": new_category_id,
            },
        )
    if name != item.name:
        log.info(
            "renamed item %r -> %r (id %d)",
            item.name,
            name,
            item_id,
            extra={"item.id": item_id, "item.name": name, "item.old_name": item.name},
        )
    if (quantity, unit) != (item.quantity, item.unit):
        log.info(
            "changed quantity of item %r (id %d): %s %s -> %s %s",
            name,
            item_id,
            item.quantity,
            item.unit,
            quantity,
            unit,
            extra={
                "item.id": item_id,
                "item.name": name,
                "item.old_quantity": item.quantity,
                "item.old_unit": item.unit,
                "item.quantity": quantity,
                "item.unit": unit,
            },
        )

    return repository.update_item(
        conn,
        item_id,
        name=name,
        category_id=new_category_id,
        purchased=item.purchased if body.purchased is None else body.purchased,
        quantity=quantity,
        unit=unit,
    )


@router.delete("/{item_id}", status_code=204)
def delete_item(item_id: int, conn: sqlite3.Connection = Depends(get_db)):
    item = repository.get_item(conn, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    repository.delete_item(conn, item_id)
    log.info(
        "deleted item %r (id %d)", item.name, item_id, extra={"item.id": item_id, "item.name": item.name}
    )


@router.post("/clear-purchased", status_code=204)
def clear_purchased(conn: sqlite3.Connection = Depends(get_db)):
    removed = repository.clear_purchased(conn)
    log.info("cleared %d purchased item(s)", removed, extra={"items.removed": removed})
