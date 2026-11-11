"use strict";

const ZXING_URL = "https://cdn.jsdelivr.net/npm/@zxing/browser@0.1.5/umd/zxing-browser.min.js";

const state = { items: [], sections: [], pantry: "Pantry", editing: null, maxMissing: "2", view: "list" };
const $ = (sel) => document.querySelector(sel);

// ---------- API ----------
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function refresh() {
  state.items = await api("GET", "/api/items");
  render();
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), 2600);
}

async function guard(fn) {
  try { await fn(); } catch (e) { toast(e.message); }
}

// ---------- Rendering ----------
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) node.append(c);
  return node;
}

function sectionOptions(select, { selected = "auto", includePantry = false } = {}) {
  select.replaceChildren(
    el("option", { value: "auto" }, "Auto-detect aisle"),
    ...state.sections.map((s) => el("option", { value: s }, s)),
    includePantry ? el("option", { value: state.pantry }, `${state.pantry} (I have this)`) : null,
  );
  select.value = selected;
}

function groupBySection(items) {
  const groups = new Map();
  for (const item of items) {
    if (!groups.has(item.section)) groups.set(item.section, []);
    groups.get(item.section).push(item);
  }
  return groups; // server already sorted by aisle order
}

function render() {
  const listItems = state.items.filter((i) => i.section !== state.pantry);
  const pantryItems = state.items.filter((i) => i.section === state.pantry);

  const list = $("#list");
  list.replaceChildren(
    ...[...groupBySection(listItems)].map(([section, items]) =>
      el("div", { class: "group" },
        el("h2", {}, section),
        el("ul", { class: "items" }, items.map((i) => renderItem(i, false)))),
    ),
  );
  $("#list-empty").hidden = listItems.length > 0;
  $("#clear-checked").hidden = !listItems.some((i) => i.checked);

  const pantry = $("#pantry");
  pantry.replaceChildren(
    pantryItems.length
      ? el("div", { class: "group" },
          el("ul", { class: "items" }, pantryItems.map((i) => renderItem(i, true))))
      : "",
  );
  $("#pantry-empty").hidden = pantryItems.length > 0;
}

function renderItem(item, inPantry) {
  if (state.editing === item.id) return renderEditForm(item);

  const startEdit = () => { state.editing = item.id; render(); };
  return el("li", { class: "item" + (item.checked ? " checked" : "") },
    inPantry ? null : el("input", {
      type: "checkbox",
      checked: item.checked,
      "aria-label": `Got ${item.name}`,
      onchange: (e) => guard(async () => {
        await api("PATCH", `/api/items/${item.id}`, { checked: e.target.checked });
        await refresh();
      }),
    }),
    el("div", { class: "text", onclick: startEdit },
      el("div", { class: "name" }, item.name),
      item.quantity ? el("div", { class: "qty" }, item.quantity) : null),
    el("div", { class: "actions" },
      inPantry ? el("button", {
        class: "icon", title: "Add to shopping list", "aria-label": `Add ${item.name} to shopping list`,
        onclick: () => guard(async () => {
          await api("POST", "/api/items", { name: item.name, section: "auto" });
          toast(`Added ${item.name} to the list`);
          await refresh();
        }),
      }, "🛒") : null,
      el("button", { class: "icon", title: "Edit", "aria-label": `Edit ${item.name}`, onclick: startEdit }, "✏️"),
      el("button", {
        class: "icon danger", title: "Delete", "aria-label": `Delete ${item.name}`,
        onclick: () => guard(async () => {
          await api("DELETE", `/api/items/${item.id}`);
          await refresh();
        }),
      }, "🗑️")),
  );
}

function renderEditForm(item) {
  const select = el("select", { name: "section", "aria-label": "Store section" });
  sectionOptions(select, { selected: item.section_auto ? "auto" : item.section, includePantry: true });
  const cancel = () => { state.editing = null; render(); };
  const form = el("form", {
    class: "edit-form",
    autocomplete: "off",
    onsubmit: (e) => {
      e.preventDefault();
      const f = new FormData(form);
      guard(async () => {
        await api("PUT", `/api/items/${item.id}`, {
          name: f.get("name"),
          quantity: f.get("quantity"),
          section: f.get("section"),
        });
        state.editing = null;
        await refresh();
      });
    },
    onkeydown: (e) => { if (e.key === "Escape") cancel(); },
  },
    el("input", { name: "name", value: item.name, required: true, maxlength: 200, "aria-label": "Item name" }),
    el("input", { name: "quantity", value: item.quantity, maxlength: 200, placeholder: "Quantity", "aria-label": "Quantity" }),
    select,
    el("div", { class: "row-actions" },
      el("button", { type: "button", class: "ghost", onclick: cancel }, "Cancel"),
      el("button", { type: "submit", class: "primary" }, "Save")),
  );
  queueMicrotask(() => form.querySelector("input").focus());
  return el("li", {}, form);
}

// ---------- Tabs ----------
function showView(view) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.view === view));
  }
  state.view = view;
  for (const v of ["list", "pantry", "recipes"]) $(`#view-${v}`).hidden = view !== v;
  history.replaceState(null, "", view === "list" ? location.pathname : `#${view}`);
  if (view === "recipes") guard(loadRecipes);
}

// ---------- Recipes ----------
async function loadRecipes() {
  const note = $("#recipes-note");
  const empty = $("#recipes-empty");
  if (!$("#recipes").childElementCount) note.textContent = "Loading recipes…";
  const data = await api("GET", `/api/recipes?max_missing=${state.maxMissing}`);

  const open = new Set([...document.querySelectorAll("#recipes details[open]")].map((d) => d.dataset.id));
  $("#recipes").replaceChildren(...data.recipes.map((r) => renderRecipe(r, open.has(r.id))));

  note.textContent = data.catalog_size
    ? `Matching your ${data.pantry_size} pantry items against ${data.catalog_size} recipes from TheMealDB. ` +
      `Assumes you always have ${data.staples.filter((s) => !s.includes(" ")).join(", ")}.`
    : "";
  empty.hidden = data.recipes.length > 0;
  empty.textContent =
    data.error && !data.catalog_size ? data.error :
    data.pantry_size === 0 ? "Your pantry is empty. Scan or add what you have and recipes will show up here." :
    "No recipes match. Try allowing more missing ingredients.";
}

function renderRecipe(r, isOpen) {
  const missing = r.ingredients.filter((i) => !i.have && !i.staple);
  const score = el("div", { class: "score" },
    el("span", { class: "ok" }, `Have ${r.have}`),
    missing.length
      ? el("span", { class: "need" }, ` · Need ${missing.map((i) => i.name).join(", ")}`)
      : el("span", { class: "ok" }, " · You have everything"));

  const ingredients = el("ul", { class: "ingredients" }, r.ingredients.map((i) => {
    const kind = i.have ? "have" : i.staple ? "staple" : "missing";
    return el("li", { class: kind },
      el("span", { class: "mark", "aria-label": kind }, i.have ? "✓" : i.staple ? "·" : "✗"),
      el("span", {}, i.name,
        i.have && i.pantry_item.toLowerCase() !== i.name.toLowerCase()
          ? el("span", { class: "from" }, `from ${i.pantry_item}`) : null),
      el("span", { class: "measure" }, i.measure));
  }));

  const addMissing = missing.length ? el("button", {
    class: "primary",
    onclick: (e) => guard(async () => {
      e.target.disabled = true;
      const onList = new Set(state.items.filter((i) => i.section !== state.pantry).map((i) => i.name.toLowerCase()));
      const toAdd = missing.filter((i) => !onList.has(i.name.toLowerCase()));
      for (const i of toAdd) {
        await api("POST", "/api/items", { name: i.name, quantity: i.measure, section: "auto" });
      }
      await refresh();
      toast(toAdd.length ? `Added ${toAdd.length} item${toAdd.length === 1 ? "" : "s"} to the list`
                         : "Those are already on your list");
    }),
  }, `🛒 Add ${missing.length} missing to list`) : null;

  const thumb = r.thumb ? `${r.thumb}/small` : "";
  return el("details", { class: "recipe", "data-id": r.id, open: isOpen },
    el("summary", {},
      thumb ? el("img", { class: "thumb", src: thumb, alt: "", loading: "lazy" }) : null,
      el("div", {},
        el("div", { class: "title" }, r.name),
        el("div", { class: "meta" }, [r.area, r.category].filter(Boolean).join(" · ")),
        score)),
    el("div", { class: "recipe-body" },
      el("div", { class: "row-actions" }, addMissing),
      el("h3", {}, "Ingredients"),
      ingredients,
      el("h3", {}, "Instructions"),
      el("div", { class: "instructions" }, r.instructions),
      el("div", { class: "links" },
        r.source ? el("a", { href: r.source, target: "_blank", rel: "noopener" }, "Original recipe ↗") : null,
        r.youtube ? el("a", { href: r.youtube, target: "_blank", rel: "noopener" }, "Video ↗") : null)));
}

// ---------- Forms ----------
function wireAddForm(form, fixedSection) {
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const f = new FormData(form);
    guard(async () => {
      await api("POST", "/api/items", {
        name: f.get("name"),
        quantity: f.get("quantity"),
        section: fixedSection || f.get("section"),
      });
      form.reset();
      if (form.section) form.section.value = "auto";
      form.name.focus();
      await refresh();
    });
  });
}

// ---------- Barcode scanning ----------
const scanner = {
  stream: null,
  zxingControls: null,
  running: false,
};

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error("Couldn't load the barcode scanner library"));
    document.head.append(s);
  });
}

async function nativeDetector() {
  if (!("BarcodeDetector" in window)) return null;
  const wanted = ["ean_13", "ean_8", "upc_a", "upc_e"];
  const supported = await BarcodeDetector.getSupportedFormats().catch(() => []);
  const formats = wanted.filter((f) => supported.includes(f));
  return formats.length ? new BarcodeDetector({ formats }) : null;
}

async function startScanner() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    toast("Camera needs HTTPS (or localhost). Type the barcode instead.");
    return;
  }
  const dialog = $("#scanner");
  const video = $("#video");
  const status = $("#scan-status");
  status.textContent = "Starting camera…";
  dialog.showModal();
  scanner.running = true;

  try {
    const detector = await nativeDetector();
    if (detector) {
      scanner.stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      });
      video.srcObject = scanner.stream;
      await video.play();
      status.textContent = "Point the camera at a barcode";
      while (scanner.running) {
        const codes = await detector.detect(video).catch(() => []);
        if (codes.length && scanner.running) return onScanned(codes[0].rawValue);
        await new Promise((r) => setTimeout(r, 150));
      }
    } else {
      // iOS Safari & Firefox lack BarcodeDetector; fall back to ZXing.
      if (!window.ZXingBrowser) await loadScript(ZXING_URL);
      const reader = new ZXingBrowser.BrowserMultiFormatReader();
      status.textContent = "Point the camera at a barcode";
      scanner.zxingControls = await reader.decodeFromConstraints(
        { video: { facingMode: "environment" }, audio: false },
        video,
        (result) => { if (result && scanner.running) onScanned(result.getText()); },
      );
      if (!scanner.running) stopScanner(); // closed while starting
    }
  } catch (e) {
    stopScanner();
    toast(e.name === "NotAllowedError" ? "Camera permission was denied" : e.message);
  }
}

function stopScanner() {
  scanner.running = false;
  scanner.zxingControls?.stop();
  scanner.zxingControls = null;
  scanner.stream?.getTracks().forEach((t) => t.stop());
  scanner.stream = null;
  $("#video").srcObject = null;
  if ($("#scanner").open) $("#scanner").close();
}

async function onScanned(code) {
  stopScanner();
  navigator.vibrate?.(60);
  await lookupAndConfirm(code);
}

async function lookupAndConfirm(code) {
  const existing = state.items.find((i) => i.barcode === code && i.section === state.pantry);
  toast(`Looking up ${code}…`);
  let info;
  try {
    info = await api("GET", `/api/barcode/${encodeURIComponent(code)}`);
  } catch (e) {
    info = { found: false, error: e.message };
  }
  const form = $("#confirm-form");
  form.name.value = info.found ? info.name : "";
  form.quantity.value = info.found ? info.quantity || "" : "";
  form.barcode.value = code;
  $("#confirm-source").textContent =
    (info.found ? `Found in ${info.source} · ${code}` :
      info.error ? `Lookup failed (${code}). Enter the name yourself.` :
      `Barcode ${code} isn't in the public databases. Enter the name yourself.`) +
    (existing ? ` · Already in pantry as “${existing.name}”.` : "");
  $("#toast").hidden = true;
  $("#confirm").showModal();
  (info.found ? form.quantity : form.name).focus();
}

$("#confirm").addEventListener("close", () => {
  const action = $("#confirm").returnValue;
  const form = $("#confirm-form");
  if (action !== "add" && action !== "add-next") return;
  const name = form.name.value.trim();
  if (!name) return toast("Name is required");
  guard(async () => {
    await api("POST", "/api/items", {
      name,
      quantity: form.quantity.value,
      barcode: form.barcode.value,
      section: state.pantry,
    });
    toast(`Added ${name} to pantry`);
    await refresh();
    if (action === "add-next") startScanner();
  });
});

// ---------- Boot ----------
async function boot() {
  const meta = await api("GET", "/api/sections");
  state.sections = meta.sections;
  state.pantry = meta.pantry;
  sectionOptions($("#add-form").section);

  wireAddForm($("#add-form"));
  wireAddForm($("#pantry-add-form"), state.pantry);

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => showView(tab.dataset.view));
  }
  for (const btn of document.querySelectorAll("[data-missing]")) {
    btn.addEventListener("click", () => {
      state.maxMissing = btn.dataset.missing;
      for (const b of document.querySelectorAll("[data-missing]")) {
        b.setAttribute("aria-pressed", String(b === btn));
      }
      guard(loadRecipes);
    });
  }

  await refresh();
  const hash = location.hash.slice(1);
  showView(["pantry", "recipes"].includes(hash) ? hash : "list");

  $("#clear-checked").addEventListener("click", () => guard(async () => {
    const { deleted } = await api("POST", "/api/items/clear-checked");
    toast(`Removed ${deleted} item${deleted === 1 ? "" : "s"}`);
    await refresh();
  }));

  $("#scan-btn").addEventListener("click", startScanner);
  $("#scan-cancel").addEventListener("click", stopScanner);
  $("#scanner").addEventListener("cancel", stopScanner); // Esc key

  $("#code-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const code = e.target.code.value.trim();
    if (!/^\d{6,14}$/.test(code)) return toast("Barcodes are 6–14 digits");
    e.target.reset();
    lookupAndConfirm(code);
  });

  // Keep multiple phones in sync when the tab regains focus.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || state.editing != null) return;
    guard(refresh);
    if (state.view === "recipes") guard(loadRecipes);
  });
}

guard(boot);
