const listEl = document.getElementById("catalogList");
const filterInput = document.getElementById("filterInput");
const countLabel = document.getElementById("countLabel");
const toast = document.getElementById("toast");

let entries = []; // CatalogEntry[] from GET /api/catalog, alphabetical
let categories = []; // Category[] from GET /api/categories
let editing = null; // name of the entry being renamed, if any

let toastTimer = null;
function showToast(message, ok = false) {
  toast.textContent = message;
  toast.classList.toggle("ok", ok);
  toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 4000);
}

// Entries are keyed by name, which can hold characters awkward in a path, so it goes in the query string.
async function api(method, name, body) {
  const res = await fetch(`/api/catalog?name=${encodeURIComponent(name)}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `Server said ${res.status}`;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function sortEntries() {
  entries.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
}

function categorySelect(entry) {
  const select = document.createElement("select");
  select.className = "category-select";
  select.setAttribute("aria-label", `Aisle for ${entry.name}`);
  for (const c of categories) {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = c.name;
    opt.selected = c.name === entry.category_name;
    select.appendChild(opt);
  }
  select.addEventListener("change", async () => {
    try {
      Object.assign(entry, await api("PATCH", entry.name, { category_id: Number(select.value) }));
      showToast(`Moved ${entry.name} to ${entry.category_name}.`, true);
    } catch (err) {
      showToast(`Couldn't move it: ${err.message}`);
    }
    render();
  });
  return select;
}

function editRow(entry) {
  const li = document.createElement("li");
  li.className = "catalog-row";
  const input = document.createElement("input");
  input.type = "text";
  input.value = entry.name;
  input.maxLength = 128;
  input.setAttribute("aria-label", `New name for ${entry.name}`);
  const save = document.createElement("button");
  save.textContent = "Save";
  const cancel = document.createElement("button");
  cancel.textContent = "Cancel";
  cancel.className = "secondary-btn";

  const commit = async () => {
    const name = input.value.trim();
    if (!name || name === entry.name) {
      editing = null;
      render();
      return;
    }
    save.disabled = true;
    try {
      Object.assign(entry, await api("PATCH", entry.name, { name }));
      sortEntries();
      editing = null;
      showToast(`Renamed to ${entry.name}.`, true);
      render();
    } catch (err) {
      save.disabled = false;
      showToast(`Couldn't rename it: ${err.message}`);
    }
  };
  save.addEventListener("click", commit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") commit();
    if (e.key === "Escape") cancel.click();
  });
  cancel.addEventListener("click", () => {
    editing = null;
    render();
  });
  li.append(input, save, cancel);
  setTimeout(() => input.focus(), 0);
  return li;
}

function viewRow(entry) {
  const li = document.createElement("li");
  li.className = "catalog-row";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = entry.name;
  const rename = document.createElement("button");
  rename.className = "secondary-btn";
  rename.textContent = "Rename";
  rename.setAttribute("aria-label", `Rename ${entry.name}`);
  rename.addEventListener("click", () => {
    editing = entry.name;
    render();
  });
  const del = document.createElement("button");
  del.className = "secondary-btn danger";
  del.textContent = "Delete";
  del.setAttribute("aria-label", `Delete ${entry.name}`);
  del.addEventListener("click", async () => {
    if (!confirm(`Delete "${entry.name}" from the catalogue? Items on your list are kept.`)) return;
    try {
      await api("DELETE", entry.name);
      entries = entries.filter((e) => e !== entry);
      showToast(`Deleted ${entry.name}.`, true);
    } catch (err) {
      showToast(`Couldn't delete it: ${err.message}`);
    }
    render();
  });
  li.append(name, categorySelect(entry), rename, del);
  return li;
}

function render() {
  const q = filterInput.value.trim().toLowerCase();
  const shown = entries.filter((e) => !q || e.name.toLowerCase().includes(q));
  countLabel.textContent = q ? `${shown.length} of ${entries.length}` : `${entries.length} names`;
  if (!shown.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = q ? "No matches." : "The catalogue is empty.";
    listEl.replaceChildren(li);
    return;
  }
  listEl.replaceChildren(...shown.map((e) => (e.name === editing ? editRow(e) : viewRow(e))));
}

filterInput.addEventListener("input", render);

async function load() {
  try {
    const [cat, cats] = await Promise.all([fetch("/api/catalog"), fetch("/api/categories")]);
    if (!cat.ok || !cats.ok) throw new Error(`Server said ${cat.ok ? cats.status : cat.status}`);
    entries = await cat.json();
    categories = await cats.json();
    render();
  } catch (err) {
    document.getElementById("loadingMessage").textContent = "Couldn't load the catalogue. Reload to try again.";
    showToast(`Couldn't load the catalogue: ${err.message}`);
  }
}

load();
