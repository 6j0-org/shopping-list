import { mountFeedback } from "./feedback.js";
import { finishIdSignIn, hasIdCallback, startIdSignIn } from "./signin.js";
import { startHotDogMan } from "./hotdog.js";

const ZXING_URL = "https://cdn.jsdelivr.net/npm/@zxing/browser@0.1.5/umd/zxing-browser.min.js";

const state = {
  items: [], sections: [], pantry: "Pantry", editing: null, maxMissing: "2", view: "list", me: null,
  ingredients: new Map(), // folded name -> TheMealDB ingredient name, for pantry items typed by hand
  search: { list: "", pantry: "" }, // what's typed in each page's search box
};
const VIEWS = ["list", "pantry", "recipes", "feedback"];
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
  if (res.status === 401) showSignIn("Your session ended. Please sign in again.");
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

// Items whose name contains every word typed in the search box (ignoring case and punctuation), in any order.
function searchItems(items, query) {
  const words = fold(query).split(" ").filter(Boolean);
  if (!words.length) return items;
  return items.filter((i) => { const name = fold(i.name); return words.every((w) => name.includes(w)); });
}

// The empty-page message: the usual one, or "nothing matches" while searching.
function showEmpty(id, all, shown, query, usual) {
  const p = $(id);
  p.hidden = shown.length > 0;
  p.textContent = all.length ? `Nothing matches “${query.trim()}”.` : usual;
}

function render() {
  const allList = state.items.filter((i) => i.section !== state.pantry);
  const allPantry = state.items.filter((i) => i.section === state.pantry);
  const listItems = searchItems(allList, state.search.list);
  const pantryItems = searchItems(allPantry, state.search.pantry);

  const list = $("#list");
  list.replaceChildren(
    ...[...groupBySection(listItems)].map(([section, items]) =>
      el("div", { class: "group" },
        el("h2", {}, section),
        el("ul", { class: "items" }, items.map((i) => renderItem(i, false)))),
    ),
  );
  showEmpty("#list-empty", allList, listItems, state.search.list, "Nothing on the list. Add something above.");
  $("#clear-checked").hidden = !allList.some((i) => i.checked); // removes every checked item, not only those shown

  const pantry = $("#pantry");
  pantry.replaceChildren(
    pantryItems.length
      ? el("div", { class: "group" },
          el("ul", { class: "items" }, pantryItems.map((i) => renderItem(i, true))))
      : "",
  );
  showEmpty("#pantry-empty", allPantry, pantryItems, state.search.pantry, "Your pantry is empty. Scan what you have.");
}

function wireSearch(input, key) {
  input.addEventListener("input", () => { state.search[key] = input.value; render(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && input.value) { e.preventDefault(); input.value = ""; state.search[key] = ""; render(); }
    if (e.key === "Enter") input.blur(); // closes the phone keyboard to show the results
  });
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
  const panel = el("div", { class: "suggest", role: "group", "aria-live": "polite", hidden: true });
  const save = (name) => guard(async () => {
    await api("PUT", `/api/items/${item.id}`, {
      name,
      quantity: form.quantity.value,
      section: form.section.value,
    });
    state.editing = null;
    await refresh();
  });
  const form = el("form", {
    class: "edit-form",
    autocomplete: "off",
    onsubmit: (e) => {
      e.preventDefault();
      const typed = form.name.value.trim();
      if (!typed) return;
      // Only a new name is matched, so changing the quantity of a scanned "Brand Product" doesn't ask about it.
      if (typed === item.name) return save(typed);
      matchName(typed, panel, save, "Save");
    },
    onkeydown: (e) => { if (e.key === "Escape") cancel(); },
  },
    el("input", {
      name: "name", value: item.name, required: true, maxlength: 200, "aria-label": "Item name", list: "ingredients",
      oninput: () => hidePanel(panel),
    }),
    el("input", { name: "quantity", value: item.quantity, maxlength: 200, placeholder: "Quantity", "aria-label": "Quantity" }),
    select,
    panel,
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
  $("#feedback-btn").setAttribute("aria-pressed", String(view === "feedback"));
  state.view = view;
  for (const v of VIEWS) $(`#view-${v}`).hidden = view !== v;
  history.replaceState(null, "", view === "list" ? location.pathname : `#${view}`);
  if (view === "recipes") guard(loadRecipes);
  if (view === "feedback") $("#view-feedback").replaceChildren(feedbackElement());
}

// ---------- Sign-in (6j0.org identity; see signin.js) ----------
// Only one screen shows at a time: loading, sign-in, or the app itself.
function showOnly(id) {
  $("#loading").hidden = true;
  for (const v of ["signin", ...VIEWS]) $(`#view-${v}`).hidden = `view-${v}` !== id;
}

function showSignIn(error = "") {
  state.me = null;
  $(".tabs").hidden = true;
  $("#feedback-btn").hidden = true;
  $("#share-btn").hidden = true;
  $("#signout-btn").hidden = true;
  for (const d of document.querySelectorAll("dialog[open]")) d.close();
  stopScanner();
  $("#signin-error").textContent = error;
  $("#signin-error").hidden = !error;
  showOnly("view-signin");
}

async function signIn(button) {
  button.disabled = true;
  try {
    const res = await fetch("/api/config");
    if (!res.ok) throw new Error("Can't reach the shopping list server.");
    await startIdSignIn(await res.json());
  } catch (e) {
    button.disabled = false;
    showSignIn(e.message || "Couldn't start sign-in.");
  }
}

async function signOut() {
  try { await fetch("/api/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); } catch {}
  location.replace(location.pathname);
}

// Who's signed in: { isAdmin }, or null when nobody is.
async function fetchMe() {
  const res = await fetch("/api/me");
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(`Can't reach the shopping list server (HTTP ${res.status}).`);
  return res.json();
}

// ---------- Sharing ----------
// An invite link is /#join=<code>. The code is kept in sessionStorage so it survives the trip to the identity server
// when the person isn't signed in yet.
const JOIN_KEY = "shopping-list:join";

function takeJoinCode() {
  const m = location.hash.match(/^#join=([\w-]+)$/);
  if (m) {
    sessionStorage.setItem(JOIN_KEY, m[1]);
    history.replaceState(null, "", location.pathname + location.search);
  }
}

async function offerJoin() {
  const code = sessionStorage.getItem(JOIN_KEY);
  if (!code) return;
  sessionStorage.removeItem(JOIN_KEY);
  const { members } = await api("GET", "/api/list");
  const dialog = $("#join");
  $("#join-note").textContent = members.length > 1
    ? "You'll leave the list you share now. It stays with the others on it."
    : "Anything on your list and in your pantry now moves to the shared list.";
  dialog.returnValue = "";
  dialog.showModal();
  await new Promise((resolve) => dialog.addEventListener("close", resolve, { once: true }));
  if (dialog.returnValue !== "join") return;
  const res = await api("POST", "/api/list/join", { code });
  toast(res.moved ? `You're on the shared list now, with your ${res.moved} items` : "You're on the shared list now");
  await refresh();
  if (state.view === "recipes") await loadRecipes();
}

function renderMembers({ members }) {
  const others = members.filter((m) => !m.you).length;
  $("#members").replaceChildren(...members.map((m) =>
    el("li", {},
      el("span", {}, m.you ? "You" : "Someone you invited"),
      m.you ? null : el("button", {
        class: "small ghost",
        onclick: () => guard(async () => {
          if (!confirm("Take this person off the list? They'll start over with an empty list.")) return;
          renderMembers(await api("DELETE", `/api/list/members/${m.id}`));
        }),
      }, "Remove"))));
  const me = members.find((m) => m.you);
  const leave = $("#leave-btn");
  leave.hidden = others === 0;
  leave.onclick = () => guard(async () => {
    if (!confirm("Leave this list? You'll start over with an empty list, and the others keep this one.")) return;
    renderMembers(await api("DELETE", `/api/list/members/${me.id}`));
    $("#share").close();
    toast("You left the list");
    await refresh();
  });
}

async function openShare() {
  $("#invite").hidden = true;
  renderMembers(await api("GET", "/api/list"));
  $("#share").showModal();
}

function wireShare() {
  $("#share-btn").addEventListener("click", () => guard(openShare));
  $("#share-close").addEventListener("click", () => $("#share").close());
  $("#invite-btn").addEventListener("click", () => guard(async () => {
    const { code, days } = await api("POST", "/api/list/invites");
    $("#invite-link").textContent = `${location.origin}${location.pathname}#join=${code}`;
    $("#invite-days").textContent = days;
    $("#invite").hidden = false;
  }));
  $("#copy-invite").addEventListener("click", () => guard(async () => {
    await navigator.clipboard.writeText($("#invite-link").textContent);
    toast("Copied the invite link");
  }));
  // Someone else's change shows up when the dialog closes.
  $("#share").addEventListener("close", () => guard(refresh));
}

// ---------- Feedback (feedback.js draws the page; the server's feedback.py stores it) ----------
// Drawn once and kept, so a half-written post survives switching tabs.
let feedbackPage = null; // { el, page }
function feedbackElement() {
  if (!feedbackPage) {
    const el = document.createElement("div");
    const page = mountFeedback(el, { buttonClass: "small", primaryClass: "primary", actionClass: "small" });
    feedbackPage = { el, page };
  } else {
    feedbackPage.page.refresh();
  }
  return feedbackPage.el;
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

// ---------- Adding items by hand ----------
// Names typed into the shopping list or the pantry are matched against TheMealDB's ingredient list (the one recipes
// use), so they say "Peanut Butter" and not "pnut butter", and recipes find them. The list fills the name fields'
// suggestions as you type. A name that isn't on it gets the closest few to pick from, or can be added as typed
// (paper towels aren't an ingredient).
const fold = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

async function loadIngredients() {
  const { ingredients } = await api("GET", "/api/ingredients");
  state.ingredients = new Map(ingredients.map((n) => [fold(n), n]));
  $("#ingredients").replaceChildren(...ingredients.map((n) => el("option", { value: n })));
}

function editDistance(a, b) {
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
    prev = cur;
  }
  return prev[b.length];
}

// The ingredients closest to a typed name: ones containing every typed word first (shortest first), then typos.
function closestIngredients(name, limit = 3) {
  const typed = fold(name);
  const words = typed.split(" ").filter(Boolean);
  const scored = [];
  for (const [key, display] of state.ingredients) {
    const keyWords = key.split(" ");
    const hasAll = words.every((w) => keyWords.some((k) => k.startsWith(w)));
    const similarity = 1 - editDistance(typed, key) / Math.max(typed.length, key.length);
    if (hasAll) scored.push({ display, score: 2 - key.length / 100 });
    else if (similarity >= 0.6) scored.push({ display, score: similarity });
  }
  return scored.sort((a, b) => b.score - a.score).slice(0, limit).map((s) => s.display);
}

const hidePanel = (panel) => { panel.hidden = true; panel.replaceChildren(); };

// Calls save(name) with the list's spelling when the typed name is on it (or when the list didn't load, as typed, so
// nothing is blocked). Otherwise fills panel with the closest names and "<verb> anyway", each of which calls save.
function matchName(typed, panel, save, verb = "Add") {
  if (!state.ingredients.size) return save(typed);
  const exact = state.ingredients.get(fold(typed));
  if (exact) return save(exact);
  const close = closestIngredients(typed);
  panel.replaceChildren(
    el("p", {}, close.length ? `“${typed}” isn't in the food list. Did you mean:` : `“${typed}” isn't in the food list.`),
    ...close.map((n) => el("button", { type: "button", class: "small", onclick: () => save(n) }, n)),
    el("button", { type: "button", class: "small ghost", onclick: () => save(typed) }, `${verb} “${typed}” anyway`),
  );
  panel.hidden = false;
  panel.querySelector("button").focus();
}

// save(name) adds the item with the form's other fields; panel shows the "isn't in the food list" choices.
function wireAddForm(form, panel, save) {
  form.name.addEventListener("input", () => hidePanel(panel));

  const add = (name) => guard(async () => {
    await save(name);
    hidePanel(panel);
    form.reset();
    if (form.section) form.section.value = "auto";
    form.name.focus();
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const typed = form.name.value.trim();
    if (typed) matchName(typed, panel, add);
  });
}

// ---------- Barcode scanning ----------
const scanner = {
  stream: null,
  zxingControls: null,
  running: false,
  busy: false, // a barcode is being looked up and added
  last: null, // { code, at }: the last barcode handled, to skip it while it's still in view
  unknown: new Set(), // barcodes not in the databases, this time the scanner was open: skipped from then on
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

// Scanning keeps the camera on: each barcode found in the databases goes straight into the pantry with a beep, and
// scanning carries on. A barcode the databases don't know gets a low beep and is skipped: the person sets that item
// aside and adds it by hand after closing the scanner.
const SAME_CODE_MS = 1000; // ignore the same barcode for this long after handling it, while it's still in view

async function startScanner() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    toast("Camera needs HTTPS (or localhost). Type the barcode instead.");
    return;
  }
  unlockAudio(); // must happen during the tap that opened the scanner
  const dialog = $("#scanner");
  const video = $("#video");
  const status = $("#scan-status");
  status.textContent = "Starting camera…";
  dialog.showModal();
  Object.assign(scanner, { running: true, busy: false, last: null, unknown: new Set() });

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
        if (codes.length && scanner.running) await onScanned(codes[0].rawValue);
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

// ---------- Beeps ----------
// The last step for each scan: a good beep once the item is saved, or a bad beep once the barcode turns out to be
// unknown (or saving fails). Square waves at full volume, in the 1–3 kHz range phone speakers play loudest, so they
// carry in a kitchen or a store. Browsers only allow sound after a tap, so the audio context is made when the scanner
// opens, and resumed before each beep in case the phone suspended it.
let audio = null;
function unlockAudio() {
  try {
    audio ||= new AudioContext();
    if (audio.state !== "running") audio.resume().catch(() => {});
  } catch { audio = null; }
}

const BEEPS = {
  good: [{ hz: 2400, ms: 160 }],                                   // one short, high beep
  bad: [{ hz: 600, ms: 220 }, { gap: 90 }, { hz: 600, ms: 220 }],  // two longer, low ones
};

async function beep(ok = true) {
  navigator.vibrate?.(ok ? 80 : [200, 90, 200]);
  if (!audio) return;
  if (audio.state !== "running") await audio.resume().catch(() => {});
  let t = audio.currentTime + 0.01;
  for (const { hz, ms, gap } of BEEPS[ok ? "good" : "bad"]) {
    if (gap) { t += gap / 1000; continue; }
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.type = "square";
    osc.frequency.value = hz;
    const end = t + ms / 1000;
    gain.gain.setValueAtTime(0, t);
    gain.gain.linearRampToValueAtTime(0.6, t + 0.005); // short ramps, so it doesn't click
    gain.gain.setValueAtTime(0.6, end - 0.01);
    gain.gain.linearRampToValueAtTime(0, end);
    osc.connect(gain).connect(audio.destination);
    osc.start(t);
    osc.stop(end);
    t = end;
  }
}

// ---------- Adding scanned items ----------
async function lookupBarcode(code) {
  try {
    return await api("GET", `/api/barcode/${encodeURIComponent(code)}`);
  } catch (e) {
    return { found: false, error: e.message };
  }
}

// Resolves once the item is saved. The list reloads in the background, so a beep doesn't wait for it.
async function addToPantry(name, quantity, barcode) {
  await api("POST", "/api/items", { name, quantity, barcode, section: state.pantry });
  guard(refresh);
}

// One barcode from the camera. Called for every frame that has one, so it skips while the last one is still being
// handled, and skips the same barcode for a second after (SAME_CODE_MS).
async function onScanned(code) {
  const now = Date.now();
  if (scanner.busy || scanner.unknown.has(code) || (scanner.last?.code === code && now - scanner.last.at < SAME_CODE_MS)) return;
  scanner.busy = true;
  const status = $("#scan-status");
  try {
    status.textContent = `Looking up ${code}…`;
    const info = await lookupBarcode(code);
    if (!scanner.running) return;
    if (!info.found) {
      beep(false);
      if (info.error) {
        status.textContent = `Couldn't look up ${code}. Try again, or set it aside to add by hand.`;
      } else {
        scanner.unknown.add(code); // a lookup that failed may work next time; a miss won't
        status.textContent = `Not found. Set it aside to add by hand, and scan the next item.`;
      }
      return;
    }
    try {
      await addToPantry(info.name, info.quantity || "", code);
    } catch (e) {
      beep(false);
      status.textContent = `Couldn't add ${info.name}: ${e.message}`;
      return;
    }
    beep();
    status.textContent = `Added ${info.name}. Scan the next item.`;
  } finally {
    scanner.last = { code, at: Date.now() };
    scanner.busy = false;
  }
}

// A barcode typed in by hand: add it if the databases know it, otherwise ask for a name.
async function lookupTyped(code) {
  toast(`Looking up ${code}…`);
  const info = await lookupBarcode(code);
  if (!info.found) return askForName(code, info);
  await guard(async () => {
    await addToPantry(info.name, info.quantity || "", code);
    toast(`Added ${info.name} to pantry`);
  });
}

// A typed barcode isn't in the public databases (or the lookup failed): ask for the name.
function askForName(code, info) {
  const existing = state.items.find((i) => i.barcode === code && i.section === state.pantry);
  const form = $("#confirm-form");
  form.name.value = "";
  form.quantity.value = "";
  form.barcode.value = code;
  $("#confirm-source").textContent =
    (info.error ? `Lookup failed (${code}). Enter the name yourself.`
                : `Barcode ${code} isn't in the public databases. Enter the name yourself.`) +
    (existing ? ` · Already in pantry as “${existing.name}”.` : "");
  $("#toast").hidden = true;
  $("#confirm").showModal();
  form.name.focus();
}

$("#confirm").addEventListener("close", () => {
  const action = $("#confirm").returnValue;
  const form = $("#confirm-form");
  if (action !== "add") return;
  const name = form.name.value.trim();
  if (!name) return toast("Name is required");
  guard(async () => {
    await addToPantry(name, form.quantity.value, form.barcode.value);
    toast(`Added ${name} to pantry`);
  });
});

// ---------- Boot ----------
async function boot() {
  $("#signin-btn").addEventListener("click", (e) => signIn(e.currentTarget));
  $("#signout-btn").addEventListener("click", signOut);
  $("#feedback-btn").addEventListener("click", () => showView("feedback"));
  wireShare();
  takeJoinCode();

  let signInError = "";
  if (hasIdCallback()) {
    try { await finishIdSignIn(); } catch (e) { signInError = e.message; }
  }
  const me = await fetchMe();
  if (!me) return showSignIn(signInError);
  if (signInError) toast(signInError); // e.g. a stale sign-in link, while an earlier session is still good
  state.me = me;
  $("#signout-btn").hidden = false;
  $("#share-btn").hidden = false;
  $(".tabs").hidden = false;
  $("#feedback-btn").hidden = false;
  await startList();
  await offerJoin();
}

async function startList() {
  const meta = await api("GET", "/api/sections");
  state.sections = meta.sections;
  state.pantry = meta.pantry;
  sectionOptions($("#add-form").section);

  wireSearch($("#list-search"), "list");
  wireSearch($("#pantry-search"), "pantry");
  wireAddForm($("#add-form"), $("#list-suggest"), async (name) => {
    const form = $("#add-form");
    await api("POST", "/api/items", { name, quantity: form.quantity.value, section: form.section.value });
    await refresh();
  });
  wireAddForm($("#pantry-add-form"), $("#pantry-suggest"), async (name) => {
    await addToPantry(name, $("#pantry-add-form").quantity.value);
    toast(`Added ${name} to pantry`);
  });
  loadIngredients().catch(() => {}); // suggestions are a nice-to-have; adding works without them

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
  $("#loading").hidden = true;
  showView(VIEWS.includes(hash) ? hash : "list");

  $("#clear-checked").addEventListener("click", () => guard(async () => {
    const { deleted } = await api("POST", "/api/items/clear-checked");
    toast(`Removed ${deleted} item${deleted === 1 ? "" : "s"}`);
    await refresh();
  }));

  $("#scan-btn").addEventListener("click", startScanner);
  $("#scan-cancel").addEventListener("click", stopScanner);
  $("#scanner").addEventListener("cancel", stopScanner); // Esc key
  // After scanning, point at the form for adding the set-aside items by hand.
  $("#scanner").addEventListener("close", () => {
    const n = scanner.unknown.size;
    if (!n || !state.me) return;
    toast(`${n} item${n === 1 ? " wasn't" : "s weren't"} found. Add ${n === 1 ? "it" : "them"} by hand here.`);
    $("#pantry-add-form").name.focus();
  });

  $("#code-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const code = e.target.code.value.trim();
    if (!/^\d{6,14}$/.test(code)) return toast("Barcodes are 6–14 digits");
    e.target.reset();
    lookupTyped(code);
  });

  // Keep multiple phones in sync when the tab regains focus.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || state.editing != null || !state.me) return;
    guard(refresh);
    if (state.view === "recipes") guard(loadRecipes);
    if (state.view === "feedback") feedbackPage?.page.refresh();
  });
}

startHotDogMan();
guard(boot);
