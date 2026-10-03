const listContainer = document.getElementById("listContainer");
const emptyMessage = document.getElementById("emptyMessage");
const addForm = document.getElementById("addForm");
const itemInput = document.getElementById("itemInput");
const qtyInput = document.getElementById("qtyInput");
const unitInput = document.getElementById("unitInput");
const addItemBtn = document.getElementById("addItemBtn");
const suggestionsEl = document.getElementById("suggestions");
const clearPurchasedBtn = document.getElementById("clearPurchasedBtn");

const quantityModal = document.getElementById("quantityModal");
const closeQuantityBtn = document.getElementById("closeQuantityBtn");
const quantityQtyInput = document.getElementById("quantityQtyInput");
const quantityUnitInput = document.getElementById("quantityUnitInput");
const clearQuantityBtn = document.getElementById("clearQuantityBtn");
const saveQuantityBtn = document.getElementById("saveQuantityBtn");
let quantityEditingItemId = null;

const aislesModal = document.getElementById("aislesModal");
const manageAislesBtn = document.getElementById("manageAislesBtn");
const closeAislesBtn = document.getElementById("closeAislesBtn");
const aisleList = document.getElementById("aisleList");
const addAisleForm = document.getElementById("addAisleForm");
const aisleInput = document.getElementById("aisleInput");
const copyListBtn = document.getElementById("copyListBtn");
const toast = document.getElementById("toast");

let currentItems = [];
let suggestTimer = null;
let chosenCategoryId = null; // set when a suggestion is tapped

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail || `${options.method || "GET"} ${path} failed (${res.status})`);
  }
  if (res.status === 204) return null;
  return res.json();
}

function formatQuantity(item) {
  if (item.quantity === null || item.quantity === undefined) return null;
  return item.unit ? `${item.quantity} ${item.unit}` : `×${item.quantity}`;
}

function groupByCategory(items) {
  const groups = new Map();
  for (const item of items) {
    if (!groups.has(item.category_name)) groups.set(item.category_name, []);
    groups.get(item.category_name).push(item);
  }
  return groups;
}

async function loadList() {
  const data = await api("/api/sync");
  renderList(data.items, data.categories);
}

function renderList(items, categories) {
  currentItems = items;
  listContainer.innerHTML = "";
  if (items.length === 0) {
    listContainer.appendChild(emptyMessage);
    emptyMessage.textContent = "Your list is empty.";
    return;
  }

  const groups = groupByCategory(items); // items already arrive pre-sorted by aisle order
  for (const [categoryName, groupItems] of groups) {
    const title = document.createElement("div");
    title.className = "section-title";
    title.textContent = categoryName;
    listContainer.appendChild(title);

    for (const item of groupItems) {
      listContainer.appendChild(renderItemRow(item, categories));
    }
  }
}

function renderItemRow(item, categories) {
  const row = document.createElement("div");
  row.className = "item-row" + (item.purchased ? " purchased" : "");
  row.dataset.id = item.id;

  const checkbox = document.createElement("div");
  checkbox.className = "checkbox";
  checkbox.addEventListener("click", () => togglePurchased(item.id, !item.purchased));

  const name = document.createElement("div");
  name.className = "name";
  name.textContent = item.name;

  const qtyText = formatQuantity(item);
  const qtyChip = document.createElement("button");
  qtyChip.type = "button";
  qtyChip.className = "qty-chip" + (qtyText ? "" : " unset");
  qtyChip.textContent = qtyText || "qty";
  qtyChip.addEventListener("click", (e) => {
    e.stopPropagation();
    openQuantityModal(item);
  });

  const categorySelect = document.createElement("select");
  categorySelect.className = "category-select";
  categorySelect.setAttribute("aria-label", `Aisle for ${item.name}`);
  for (const cat of categories) {
    const opt = document.createElement("option");
    opt.value = cat.id;
    opt.textContent = cat.name;
    if (cat.id === item.category_id) opt.selected = true;
    categorySelect.appendChild(opt);
  }
  categorySelect.addEventListener("click", (e) => e.stopPropagation());
  categorySelect.addEventListener("change", () => setItemCategory(item.id, Number(categorySelect.value)));

  const del = document.createElement("button");
  del.className = "delete";
  del.textContent = "✕";
  del.addEventListener("click", () => deleteItem(item.id));

  row.append(checkbox, name, qtyChip, categorySelect, del);
  return row;
}

async function togglePurchased(id, purchased) {
  await api(`/api/items/${id}`, { method: "PATCH", body: JSON.stringify({ purchased }) });
  await loadList();
}

async function deleteItem(id) {
  await api(`/api/items/${id}`, { method: "DELETE" });
  await loadList();
}

async function setItemCategory(id, categoryId) {
  await api(`/api/items/${id}`, { method: "PATCH", body: JSON.stringify({ category_id: categoryId }) });
  await loadList();
}

// ---- copy list ----

// Uses the last-loaded items (no await before the write) so the click's user activation survives,
// which Safari requires. Falls back to execCommand where the async clipboard API is unavailable
// (plain-http LAN access).
async function copyToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  const ok = document.execCommand("copy");
  ta.remove();
  if (!ok) throw new Error("Copy failed");
}

let copyTimer = null;
copyListBtn.addEventListener("click", async () => {
  const names = currentItems.filter((item) => !item.purchased).map((item) => item.name);
  if (names.length === 0) {
    showError("Nothing to copy");
    return;
  }
  await copyToClipboard(names.join("\n"));
  copyListBtn.textContent = "Copied!";
  clearTimeout(copyTimer);
  copyTimer = setTimeout(() => (copyListBtn.textContent = "Copy"), 1500);
});

// ---- quantity editing ----

function openQuantityModal(item) {
  quantityEditingItemId = item.id;
  quantityQtyInput.value = item.quantity === null || item.quantity === undefined ? "" : item.quantity;
  quantityUnitInput.value = item.unit || "";
  quantityModal.classList.remove("hidden");
  quantityQtyInput.focus();
}

function closeQuantityModal() {
  quantityModal.classList.add("hidden");
  quantityEditingItemId = null;
}

closeQuantityBtn.addEventListener("click", closeQuantityModal);

saveQuantityBtn.addEventListener("click", async () => {
  const raw = quantityQtyInput.value.trim();
  const quantity = raw === "" ? null : Number(raw);
  const unit = quantity === null ? null : (quantityUnitInput.value.trim() || null);
  await api(`/api/items/${quantityEditingItemId}`, {
    method: "PATCH",
    body: JSON.stringify({ quantity, unit }),
  });
  closeQuantityModal();
  await loadList();
});

clearQuantityBtn.addEventListener("click", async () => {
  await api(`/api/items/${quantityEditingItemId}`, {
    method: "PATCH",
    body: JSON.stringify({ quantity: null, unit: null }),
  });
  closeQuantityModal();
  await loadList();
});

clearPurchasedBtn.addEventListener("click", async () => {
  await api("/api/items/clear-purchased", { method: "POST" });
  await loadList();
});

// ---- add item + autosuggest ----

itemInput.addEventListener("input", () => {
  chosenCategoryId = null;
  clearTimeout(suggestTimer);
  const q = itemInput.value.trim();
  if (q.length < 2) {
    suggestionsEl.classList.add("hidden");
    return;
  }
  suggestTimer = setTimeout(() => fetchSuggestions(q), 150);
});

async function fetchSuggestions(q) {
  const matches = await api(`/api/catalog/suggest?q=${encodeURIComponent(q)}`);
  if (matches.length === 0) {
    suggestionsEl.classList.add("hidden");
    return;
  }
  suggestionsEl.innerHTML = "";
  for (const match of matches) {
    const li = document.createElement("li");
    const nameSpan = document.createElement("span");
    nameSpan.textContent = match.name;
    const catSpan = document.createElement("span");
    catSpan.className = "cat";
    catSpan.textContent = match.category_name;
    li.append(nameSpan, catSpan);
    li.addEventListener("click", () => {
      itemInput.value = match.name;
      chosenCategoryId = match.category_id;
      suggestionsEl.classList.add("hidden");
      itemInput.focus();
    });
    suggestionsEl.appendChild(li);
  }
  suggestionsEl.classList.remove("hidden");
}

addForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = itemInput.value.trim();
  if (!name) return;
  suggestionsEl.classList.add("hidden");

  // A brand-new item name with no catalog match triggers a classifier call on the server
  // (server/src/shopping_list/classifier.py), which can take several seconds - without this the
  // page just sits there looking unresponsive until it finishes.
  const knownCategory = chosenCategoryId !== null;
  itemInput.disabled = true;
  addItemBtn.disabled = true;
  addItemBtn.textContent = knownCategory ? "Adding…" : "Sorting…";

  const rawQty = qtyInput.value.trim();
  const quantity = rawQty === "" ? null : Number(rawQty);
  const unit = quantity === null ? null : (unitInput.value.trim() || null);

  try {
    await api("/api/items", {
      method: "POST",
      body: JSON.stringify({ name, category_id: chosenCategoryId, quantity, unit }),
    });
    itemInput.value = "";
    qtyInput.value = "";
    unitInput.value = "";
    chosenCategoryId = null;
    await loadList();
  } finally {
    itemInput.disabled = false;
    addItemBtn.disabled = false;
    addItemBtn.textContent = "Add";
  }
});

// ---- aisle management ----

manageAislesBtn.addEventListener("click", async () => {
  await loadAisles();
  aislesModal.classList.remove("hidden");
});
closeAislesBtn.addEventListener("click", () => aislesModal.classList.add("hidden"));

async function loadAisles() {
  const categories = await api("/api/categories");
  aisleList.innerHTML = "";
  categories
    .filter((c) => c.name !== "Uncategorized")
    .forEach((cat, idx, arr) => {
      const li = document.createElement("li");
      li.className = "aisle-row";
      const nameSpan = document.createElement("span");
      nameSpan.className = "aisle-name";
      nameSpan.textContent = cat.name;

      const up = document.createElement("button");
      up.textContent = "↑";
      up.disabled = idx === 0;
      up.addEventListener("click", () => swapAisleOrder(arr, idx, idx - 1));

      const down = document.createElement("button");
      down.textContent = "↓";
      down.disabled = idx === arr.length - 1;
      down.addEventListener("click", () => swapAisleOrder(arr, idx, idx + 1));

      const del = document.createElement("button");
      del.textContent = "✕";
      del.className = "aisle-delete";
      del.addEventListener("click", () => deleteAisle(cat));

      li.append(nameSpan, up, down, del);
      aisleList.appendChild(li);
    });
}

async function deleteAisle(cat) {
  if (!confirm(`Delete "${cat.name}"? Items in it move to Uncategorized.`)) return;
  await api(`/api/categories/${cat.id}`, { method: "DELETE" });
  await loadAisles();
  await loadList();
}

async function swapAisleOrder(categories, i, j) {
  const a = categories[i];
  const b = categories[j];
  await Promise.all([
    api(`/api/categories/${a.id}`, { method: "PATCH", body: JSON.stringify({ sort_order: b.sort_order }) }),
    api(`/api/categories/${b.id}`, { method: "PATCH", body: JSON.stringify({ sort_order: a.sort_order }) }),
  ]);
  await loadAisles();
}

addAisleForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = aisleInput.value.trim();
  if (!name) return;
  await api("/api/categories", { method: "POST", body: JSON.stringify({ name }) });
  aisleInput.value = "";
  await loadAisles();
});

// Every action above runs from an event handler, so a failed request (no signal in the shop, server
// down) would otherwise vanish silently.
let toastTimer = null;
function showError(message) {
  toast.textContent = message;
  toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 4000);
}
window.addEventListener("unhandledrejection", (e) => {
  showError(e.reason instanceof TypeError ? "Can't reach the server" : String(e.reason?.message || e.reason));
});

loadList();
