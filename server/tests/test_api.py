import re


def test_new_deployment_has_uncategorized_bucket(client):
    categories = client.get("/api/categories").json()
    names = [c["name"] for c in categories]
    assert "Uncategorized" in names


def test_create_category_appends_before_uncategorized(client):
    client.post("/api/categories", json={"name": "Produce"})
    client.post("/api/categories", json={"name": "Dairy"})
    categories = client.get("/api/categories").json()
    ordered = [c["name"] for c in sorted(categories, key=lambda c: c["sort_order"])]
    assert ordered == ["Produce", "Dairy", "Uncategorized"]


def test_create_category_duplicate_name_rejected(client):
    client.post("/api/categories", json={"name": "Produce"})
    resp = client.post("/api/categories", json={"name": "Produce"})
    assert resp.status_code == 409


def test_add_item_with_explicit_category(client):
    cat = client.post("/api/categories", json={"name": "Produce"}).json()
    resp = client.post("/api/items", json={"name": "Bananas", "category_id": cat["id"]})
    assert resp.status_code == 201
    item = resp.json()
    assert item["category_id"] == cat["id"]
    assert item["category_name"] == "Produce"
    assert item["purchased"] is False


def test_add_item_unknown_name_falls_back_to_uncategorized_when_classification_disabled(client):
    client.post("/api/categories", json={"name": "Produce"})
    resp = client.post("/api/items", json={"name": "Mystery Snack"})
    item = resp.json()
    assert item["category_name"] == "Uncategorized"


def test_add_item_reuses_catalog_mapping_without_reclassifying(client):
    cat = client.post("/api/categories", json={"name": "Produce"}).json()
    client.post("/api/items", json={"name": "Bananas", "category_id": cat["id"]})
    # Second time, no category_id given — should resolve from the catalog entry
    # written by the first call, not fall back to Uncategorized.
    resp = client.post("/api/items", json={"name": "Bananas"})
    item = resp.json()
    assert item["category_id"] == cat["id"]


def test_toggle_purchased(client):
    item = client.post("/api/items", json={"name": "Milk"}).json()
    resp = client.patch(f"/api/items/{item['id']}", json={"purchased": True})
    assert resp.json()["purchased"] is True

    resp = client.patch(f"/api/items/{item['id']}", json={"purchased": False})
    assert resp.json()["purchased"] is False


def test_patch_unknown_item_404(client):
    resp = client.patch("/api/items/999", json={"purchased": True})
    assert resp.status_code == 404


def test_delete_item(client):
    item = client.post("/api/items", json={"name": "Milk"}).json()
    resp = client.delete(f"/api/items/{item['id']}")
    assert resp.status_code == 204
    resp = client.patch(f"/api/items/{item['id']}", json={"purchased": True})
    assert resp.status_code == 404


def test_clear_purchased_only_removes_purchased_items(client):
    a = client.post("/api/items", json={"name": "Milk"}).json()
    client.post("/api/items", json={"name": "Eggs"})
    client.patch(f"/api/items/{a['id']}", json={"purchased": True})

    client.post("/api/items/clear-purchased")

    items = client.get("/api/sync").json()["items"]
    names = [i["name"] for i in items]
    assert "Milk" not in names
    assert "Eggs" in names


def test_sync_orders_items_by_category_sort_order_then_name(client):
    dairy = client.post("/api/categories", json={"name": "Dairy"}).json()
    produce = client.post("/api/categories", json={"name": "Produce"}).json()
    client.patch(f"/api/categories/{dairy['id']}", json={"sort_order": 5})
    client.patch(f"/api/categories/{produce['id']}", json={"sort_order": 1})

    client.post("/api/items", json={"name": "Milk", "category_id": dairy["id"]})
    client.post("/api/items", json={"name": "Cheese", "category_id": dairy["id"]})
    client.post("/api/items", json={"name": "Apples", "category_id": produce["id"]})

    items = client.get("/api/sync").json()["items"]
    names = [i["name"] for i in items]
    assert names == ["Apples", "Cheese", "Milk"]


def test_catalog_suggest_matches_substring(client):
    client.post("/api/items", json={"name": "Whole Milk"})
    resp = client.get("/api/catalog/suggest", params={"q": "milk"})
    matches = [m["name"] for m in resp.json()]
    assert "Whole Milk" in matches


def test_catalog_suggest_empty_query_returns_nothing(client):
    resp = client.get("/api/catalog/suggest", params={"q": ""})
    assert resp.json() == []


def test_add_item_whitespace_only_name_rejected(client):
    resp = client.post("/api/items", json={"name": "   "})
    assert resp.status_code == 400


def test_rename_item_to_whitespace_only_rejected(client):
    item = client.post("/api/items", json={"name": "Milk"}).json()
    resp = client.patch(f"/api/items/{item['id']}", json={"name": "   "})
    assert resp.status_code == 400


def test_cannot_rename_uncategorized_bucket(client):
    categories = client.get("/api/categories").json()
    uncategorized = next(c for c in categories if c["name"] == "Uncategorized")
    resp = client.patch(f"/api/categories/{uncategorized['id']}", json={"name": "Renamed"})
    assert resp.status_code == 400


def test_delete_category(client):
    cat = client.post("/api/categories", json={"name": "Produce"}).json()
    resp = client.delete(f"/api/categories/{cat['id']}")
    assert resp.status_code == 204
    categories = client.get("/api/categories").json()
    assert all(c["id"] != cat["id"] for c in categories)


def test_delete_category_reassigns_its_items_to_uncategorized(client):
    cat = client.post("/api/categories", json={"name": "Produce"}).json()
    item = client.post("/api/items", json={"name": "Apples", "category_id": cat["id"]}).json()

    client.delete(f"/api/categories/{cat['id']}")

    items = client.get("/api/sync").json()["items"]
    updated = next(i for i in items if i["id"] == item["id"])
    assert updated["category_name"] == "Uncategorized"
    assert updated["category_id"] is None


def test_cannot_delete_uncategorized_bucket(client):
    categories = client.get("/api/categories").json()
    uncategorized = next(c for c in categories if c["name"] == "Uncategorized")
    resp = client.delete(f"/api/categories/{uncategorized['id']}")
    assert resp.status_code == 400


def test_delete_unknown_category_404(client):
    resp = client.delete("/api/categories/999")
    assert resp.status_code == 404


def test_create_category_duplicate_name_is_case_insensitive(client):
    client.post("/api/categories", json={"name": "Produce"})
    resp = client.post("/api/categories", json={"name": "produce"})
    assert resp.status_code == 409


def test_rename_category_onto_existing_name_rejected(client):
    client.post("/api/categories", json={"name": "Produce"})
    dairy = client.post("/api/categories", json={"name": "Dairy"}).json()
    resp = client.patch(f"/api/categories/{dairy['id']}", json={"name": "Produce"})
    assert resp.status_code == 409


def test_orphaned_items_group_with_uncategorized_alphabetically(client):
    produce = client.post("/api/categories", json={"name": "Produce"}).json()
    client.post("/api/items", json={"name": "Zucchini"})
    client.post("/api/items", json={"name": "Apples", "category_id": produce["id"]})
    client.delete(f"/api/categories/{produce['id']}")

    items = client.get("/api/sync").json()["items"]
    assert [(i["name"], i["category_name"]) for i in items] == [
        ("Apples", "Uncategorized"),
        ("Zucchini", "Uncategorized"),
    ]


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_add_item_with_quantity_and_unit(client):
    resp = client.post("/api/items", json={"name": "Flour", "quantity": 500, "unit": "g"})
    item = resp.json()
    assert item["quantity"] == 500
    assert item["unit"] == "g"


def test_add_item_with_plain_count_quantity(client):
    resp = client.post("/api/items", json={"name": "Eggs", "quantity": 12})
    item = resp.json()
    assert item["quantity"] == 12
    assert item["unit"] is None


def test_add_item_unit_without_quantity_rejected(client):
    resp = client.post("/api/items", json={"name": "Flour", "unit": "g"})
    assert resp.status_code == 400


def test_add_item_zero_quantity_rejected(client):
    resp = client.post("/api/items", json={"name": "Flour", "quantity": 0})
    assert resp.status_code == 400


def test_update_item_sets_quantity(client):
    item = client.post("/api/items", json={"name": "Milk"}).json()
    resp = client.patch(f"/api/items/{item['id']}", json={"quantity": 2, "unit": "L"})
    updated = resp.json()
    assert updated["quantity"] == 2
    assert updated["unit"] == "L"


def test_update_item_clears_quantity(client):
    item = client.post("/api/items", json={"name": "Milk", "quantity": 2, "unit": "L"}).json()
    resp = client.patch(f"/api/items/{item['id']}", json={"quantity": None, "unit": None})
    updated = resp.json()
    assert updated["quantity"] is None
    assert updated["unit"] is None


def test_update_item_omitting_quantity_leaves_it_unchanged(client):
    item = client.post("/api/items", json={"name": "Milk", "quantity": 2, "unit": "L"}).json()
    resp = client.patch(f"/api/items/{item['id']}", json={"purchased": True})
    updated = resp.json()
    assert updated["quantity"] == 2
    assert updated["unit"] == "L"


def test_sync_includes_quantity(client):
    client.post("/api/items", json={"name": "Milk", "quantity": 2, "unit": "L"})
    items = client.get("/api/sync").json()["items"]
    milk = next(i for i in items if i["name"] == "Milk")
    assert milk["quantity"] == 2
    assert milk["unit"] == "L"


def test_index_serves_web_ui_with_versioned_assets(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert re.search(r"/static/app\.js\?v=[0-9a-f]{10}", resp.text)
    assert resp.headers["cache-control"] == "no-cache"


def test_catalog_list_is_alphabetical(client):
    client.post("/api/items", json={"name": "Milk"})
    client.post("/api/items", json={"name": "Apples"})
    names = [e["name"] for e in client.get("/api/catalog").json()]
    assert names == ["Apples", "Milk"]


def test_catalog_rename_fixes_typo_and_keeps_list_item(client):
    client.post("/api/items", json={"name": "Bananna"})
    resp = client.patch("/api/catalog", params={"name": "Bananna"}, json={"name": "Banana"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Banana"
    names = [e["name"] for e in client.get("/api/catalog").json()]
    assert names == ["Banana"]
    assert [i["name"] for i in client.get("/api/sync").json()["items"]] == ["Bananna"]


def test_catalog_move_to_other_category(client):
    cat = client.post("/api/categories", json={"name": "Produce"}).json()
    client.post("/api/items", json={"name": "Tomato"})
    resp = client.patch("/api/catalog", params={"name": "Tomato"}, json={"category_id": cat["id"]})
    assert resp.json()["category_name"] == "Produce"


def test_catalog_rename_onto_existing_name_conflicts(client):
    client.post("/api/items", json={"name": "Milk"})
    client.post("/api/items", json={"name": "Mlk"})
    resp = client.patch("/api/catalog", params={"name": "Mlk"}, json={"name": "milk"})
    assert resp.status_code == 409


def test_catalog_patch_rejects_unknown_entry_blank_name_and_category(client):
    client.post("/api/items", json={"name": "Milk"})
    assert client.patch("/api/catalog", params={"name": "Nope"}, json={"name": "x"}).status_code == 404
    assert client.patch("/api/catalog", params={"name": "Milk"}, json={"name": "  "}).status_code == 400
    assert (
        client.patch("/api/catalog", params={"name": "Milk"}, json={"category_id": 9999}).status_code == 400
    )


def test_catalog_delete_removes_entry_only(client):
    client.post("/api/items", json={"name": "Milk"})
    assert client.delete("/api/catalog", params={"name": "Milk"}).status_code == 204
    assert client.get("/api/catalog").json() == []
    assert len(client.get("/api/sync").json()["items"]) == 1
    assert client.delete("/api/catalog", params={"name": "Milk"}).status_code == 404


def test_catalog_page_served(client):
    resp = client.get("/catalog")
    assert resp.status_code == 200
    assert re.search(r"/static/catalog\.js\?v=[0-9a-f]{10}", resp.text)
    assert client.get("/static/catalog.js").status_code == 200
