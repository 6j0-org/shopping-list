import { mountFeedback } from "./feedback.js";
import { finishIdSignIn, hasIdCallback, startIdSignIn } from "./signin.js";
import { startHotDogMan } from "./hotdog.js";

const ZXING_URL = "https://cdn.jsdelivr.net/npm/@zxing/browser@0.1.5/umd/zxing-browser.min.js";

const state = {
  items: [], sections: [], pantry: "Pantry", editing: null, maxMissing: "", recipeEditing: false, view: "list", me: null,
  ingredients: new Map(), // folded name -> a name the list already uses (its recipes' ingredients and its items)
  search: { list: "", pantry: "" }, // what's typed in each page's search box
  settings: {}, // which optional controls show; see Settings
};
const VIEWS = ["list", "pantry", "recipes", "settings", "feedback"];
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
      if (typed) save(typed);
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

// ---------- Settings ----------
// Optional controls, all off to start (except Hot Dog Man), saved on the server per person (they come with /api/me). Each one shows the
// elements marked .opt-<name> (see style.css). The code behind them stays; turning one off only hides it. Quantity
// and aisle only come off the add forms, to keep adding simple: editing an item always has both.
const SETTINGS = { search: "show-search", quantity: "show-quantity", aisle: "show-aisle", barcodeTyping: "show-barcode-typing" };

function applySettings(settings) {
  state.settings = settings;
  for (const box of document.querySelectorAll("[data-setting]")) box.checked = !!settings[box.dataset.setting];
  $("#hotdogs-setting").hidden = !settings.metHotDogMan;
  for (const [key, cls] of Object.entries(SETTINGS)) document.documentElement.classList.toggle(cls, !!settings[key]);
  // A search that's hidden shouldn't keep filtering.
  if (!settings.search && (state.search.list || state.search.pantry)) {
    state.search = { list: "", pantry: "" };
    $("#list-search").value = $("#pantry-search").value = "";
    render();
  }
}

async function saveSettings(change) {
  const before = state.settings;
  applySettings({ ...before, ...change }); // show it now; the server's answer settles it
  try {
    applySettings(await api("PUT", "/api/settings", change));
  } catch (e) {
    applySettings(before);
    toast(`Couldn't save that setting: ${e.message}`);
  }
}

function wireSettings() {
  for (const box of document.querySelectorAll("[data-setting]")) {
    box.addEventListener("change", () => saveSettings({ [box.dataset.setting]: box.checked }));
  }
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
  $("#siri-btn").hidden = true;
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

// ---------- Siri (a personal API token for an iOS Shortcut; see /api/token in server.py) ----------
const day = (ms) => new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

function renderSiri({ token, created, lastUsed }) {
  $("#siri-status").textContent = created
    ? `You have a token, made ${day(created)}. ${lastUsed ? `Last used ${day(lastUsed)}.` : "Not used yet."}`
    : "You don't have a token.";
  $("#siri-make").textContent = created ? "Make a new token" : "Make a token";
  $("#siri-revoke").hidden = !created;
  $("#siri-new").hidden = !token;
  $("#siri-token").textContent = token ? `Bearer ${token}` : "";
}

async function openSiri() {
  $("#siri-url").textContent = `${location.origin}/api/shortcut/items`;
  renderSiri(await api("GET", "/api/token"));
  $("#siri").showModal();
}

function wireSiri() {
  $("#siri-btn").addEventListener("click", () => guard(openSiri));
  $("#siri-close").addEventListener("click", () => $("#siri").close());
  $("#siri-make").addEventListener("click", () => guard(async () => {
    if (!$("#siri-revoke").hidden &&
        !confirm("Make a new token? The old one stops working, so shortcuts using it will need the new one.")) return;
    renderSiri(await api("POST", "/api/token"));
  }));
  $("#siri-revoke").addEventListener("click", () => guard(async () => {
    if (!confirm("Turn off the token? Shortcuts using it stop working.")) return;
    await api("DELETE", "/api/token");
    renderSiri({});
    toast("Turned off the token");
  }));
  for (const [button, code, what] of [["#siri-copy-token", "#siri-token", "token"], ["#siri-copy-url", "#siri-url", "URL"]]) {
    $(button).addEventListener("click", () => guard(async () => {
      await navigator.clipboard.writeText($(code).textContent);
      toast(`Copied the ${what}`);
    }));
  }
  // Items added from a shortcut while the app was open show up when the dialog closes.
  $("#siri").addEventListener("close", () => guard(refresh));
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
// The recipe book everyone shares. A recipe is written by hand, or imported from a link or pasted text (the server has
// Claude read it). Only the person who added a recipe (and the admin) can edit or delete it. Each one shows which
// ingredients your pantry covers.
async function loadRecipes() {
  if (state.recipeEditing) return; // don't throw away an open editor
  const note = $("#recipes-note");
  const empty = $("#recipes-empty");
  if (!$("#recipes").childElementCount) note.textContent = "Loading recipes…";
  const data = await api("GET", `/api/recipes?max_missing=${state.maxMissing}`);
  if (state.recipeEditing) return;

  $("#import-form").hidden = !data.canImport;
  state.staples = new Set(data.staples.map(fold));
  const open = new Set([...document.querySelectorAll("#recipes [data-id][open]")].map((d) => d.dataset.id));
  $("#recipes").replaceChildren(...data.recipes.map((r) => renderRecipe(r, open.has(String(r.id)))));

  note.textContent = data.count
    ? `${data.recipes.length} of ${data.count} recipe${data.count === 1 ? "" : "s"}, checked against your ${data.pantry_size} pantry items. ` +
      `Assumes you always have ${data.staples.filter((s) => !s.includes(" ")).join(", ")}.`
    : "";
  empty.hidden = data.recipes.length > 0;
  empty.textContent = data.count === 0
    ? "The recipe book is empty. Add a recipe above."
    : "No recipes match. Try allowing more missing ingredients.";
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}

function renderRecipe(r, isOpen) {
  const missing = r.ingredients.filter((i) => !i.have && !i.staple);
  const score = el("div", { class: "score" },
    el("span", { class: "ok" }, `Have ${r.have}`),
    missing.length
      ? el("span", { class: "need" }, ` · Need ${[...new Set(missing.map((i) => i.name))].join(", ")}`)
      : el("span", { class: "ok" }, " · You have everything"));

  const ingredients = el("ul", { class: "ingredients" }, r.ingredients.map((i) => {
    const kind = i.have ? "have" : i.staple ? "staple" : "missing";
    return el("li", { class: kind },
      el("span", { class: "mark", "aria-label": kind }, i.have ? "✓" : i.staple ? "·" : "✗"),
      el("span", {}, i.name,
        i.note ? el("span", { class: "note" }, `, ${i.note}`) : null,
        i.have && i.pantry_item.toLowerCase() !== i.name.toLowerCase()
          ? el("span", { class: "from" }, `from ${i.pantry_item}`) : null),
      el("span", { class: "measure" }, i.quantity));
  }));

  // Puts each missing ingredient on the shopping list once, with the recipe's quantity, skipping any already there.
  const addMissing = missing.length ? el("button", {
    class: "primary",
    onclick: (e) => guard(async () => {
      e.target.disabled = true;
      try {
        const onList = new Set(state.items.filter((i) => i.section !== state.pantry).map((i) => fold(i.name)));
        const toAdd = missing.filter((i) => {
          if (onList.has(fold(i.name))) return false;
          onList.add(fold(i.name));
          return true;
        });
        for (const i of toAdd) {
          await api("POST", "/api/items", { name: i.name, quantity: i.quantity, section: "auto" });
        }
        await refresh();
        toast(toAdd.length ? `Added ${toAdd.length} item${toAdd.length === 1 ? "" : "s"} to the list`
                           : "Those are already on your list");
      } finally {
        e.target.disabled = false;
      }
    }),
  }, `🛒 Add ${new Set(missing.map((i) => fold(i.name))).size} missing to list`) : null;

  const details = el("details", { class: "recipe", "data-id": r.id, open: isOpen });
  const edit = r.canEdit && el("button", { class: "ghost", onclick: () => {
    if (state.recipeEditing) return;
    details.replaceWith(el("div", { class: "recipe", "data-id": r.id, open: true }, recipeEditor(r, {
      save: (data) => api("PUT", `/api/recipes/${r.id}`, data),
      done: () => guard(loadRecipes),
    })));
  } }, "Edit");
  const remove = r.canEdit && el("button", { class: "ghost danger", onclick: () => guard(async () => {
    if (!confirm(`Delete “${r.title}” from the recipe book?`)) return;
    await api("DELETE", `/api/recipes/${r.id}`);
    toast(`Deleted ${r.title}`);
    await loadRecipes();
  }) }, "Delete");

  const host = hostOf(r.source);
  details.append(
    el("summary", {},
      el("div", {},
        el("div", { class: "title" }, r.title),
        el("div", { class: "meta" }, [r.servings && `Serves ${r.servings}`, host, r.origin === "generated" && "Generated",
          r.mine && "Added by you"].filter(Boolean).join(" · ")),
        score)),
    el("div", { class: "recipe-body" },
      el("div", { class: "row-actions" }, addMissing, edit || null, remove || null),
      r.origin === "generated"
        ? el("p", { class: "muted small" }, "A recipe model wrote this. Check amounts, times, and temperatures before you cook it.")
        : null,
      el("h3", {}, "Ingredients"),
      ingredients,
      r.steps.length ? el("h3", {}, "Steps") : null,
      r.steps.length ? el("ol", { class: "steps" }, r.steps.map((s) => el("li", {}, s))) : null,
      r.source ? el("div", { class: "links" },
        el("a", { href: r.source, target: "_blank", rel: "noopener" }, `Original recipe${host ? ` on ${host}` : ""} ↗`)) : null));
  return details;
}

// A form for writing or changing a recipe. save(data) sends it; done() runs after saving or cancelling.
function recipeEditor(r, { save, done }) {
  state.recipeEditing = true;
  const close = () => { state.recipeEditing = false; done(); };
  const rows = el("div", { class: "ing-rows" });
  const addRow = (i = { quantity: "", name: "", note: "" }) => {
    const row = el("div", { class: "ing-row" },
      el("input", { name: "quantity", value: i.quantity, maxlength: 100, placeholder: "Amount", "aria-label": "Amount" }),
      el("input", { name: "name", value: i.name, maxlength: 200, placeholder: "Ingredient", "aria-label": "Ingredient" }),
      el("input", { name: "note", value: i.note, maxlength: 200, placeholder: "Note (chopped…)", "aria-label": "Note" }),
      el("button", { type: "button", class: "small ghost", "aria-label": "Remove ingredient", onclick: () => row.remove() }, "✕"));
    rows.append(row);
    return row;
  };
  for (const i of r.ingredients) addRow(i);
  if (!r.ingredients.length) addRow();

  const form = el("form", {
    class: "recipe-edit",
    autocomplete: "off",
    onsubmit: (e) => {
      e.preventDefault();
      const data = {
        title: form.elements.title.value,
        servings: form.servings.value,
        source: form.source.value,
        origin: r.origin || "hand",
        ingredients: [...rows.children].map((row) => ({
          quantity: row.querySelector("[name=quantity]").value,
          name: row.querySelector("[name=name]").value,
          note: row.querySelector("[name=note]").value,
        })),
        steps: form.steps.value.split("\n").map((s) => s.replace(/^\s*\d+[.)]\s*/, "").trim()).filter(Boolean),
      };
      guard(async () => {
        await save(data);
        close();
      });
    },
    onkeydown: (e) => { if (e.key === "Escape") close(); },
  },
    el("label", {}, "Title", el("input", { name: "title", value: r.title, required: true, maxlength: 200 })),
    el("label", {}, "Serves", el("input", { name: "servings", value: r.servings, maxlength: 100, placeholder: "4" })),
    el("label", {}, "Link to the original",
      el("input", { name: "source", type: "url", value: r.source, maxlength: 2000, placeholder: "https://…" })),
    el("h3", {}, "Ingredients"),
    rows,
    el("button", { type: "button", class: "small", onclick: () => addRow().querySelector("input").focus() }, "+ Ingredient"),
    el("h3", {}, "Steps"),
    el("label", { class: "muted small" }, "One step per line (blank lines are skipped)",
      el("textarea", { name: "steps", rows: 10 }, r.steps.join("\n\n"))),
    el("div", { class: "row-actions" },
      el("button", { type: "button", class: "ghost", onclick: close }, "Cancel"),
      el("button", { type: "submit", class: "primary" }, "Save")));
  queueMicrotask(() => form.elements.title.focus());
  return form;
}

// Opens the editor for a new, blank recipe.
function writeRecipe() {
  if (state.recipeEditing) return;
  const recipe = { title: "", servings: "", source: "", origin: "hand", ingredients: [], steps: [] };
  $("#recipe-new").replaceChildren(el("div", { class: "card" },
    recipeEditor(recipe, {
      save: async (data) => {
        const saved = await api("POST", "/api/recipes", data);
        toast(`Added ${saved.title}`);
      },
      done: () => { $("#recipe-new").replaceChildren(); guard(loadRecipes); },
    })));
  $("#recipe-new").scrollIntoView({ block: "start", behavior: "smooth" });
}

// Pasted text that's just a link is fetched by the server; anything else is read as the recipe itself.
function wireRecipes() {
  const form = $("#import-form");
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const value = form.source.value.trim();
    if (!value) return;
    const body = /^https?:\/\/\S+$/i.test(value) ? { url: value } : { text: value };
    const button = form.querySelector("[type=submit]");
    guard(async () => {
      button.disabled = true;
      button.textContent = "Reading the recipe…";
      try {
        const recipe = await api("POST", "/api/recipes/import", body);
        form.reset();
        toast(`Added ${recipe.title}`);
        await loadRecipes();
        const added = document.querySelector(`#recipes details[data-id="${recipe.id}"]`);
        if (added) { added.open = true; added.scrollIntoView({ block: "start", behavior: "smooth" }); }
      } finally {
        button.disabled = false;
        button.textContent = "Import recipe";
      }
    });
  });
  $("#write-recipe").addEventListener("click", () => writeRecipe());
}

// ---------- Adding items by hand ----------
// Names are added as typed.
const fold = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

// save(name) adds the item with the form's other fields.
function wireAddForm(form, save) {
  const add = (name) => guard(async () => {
    await save(name);
    form.reset();
    if (form.section) form.section.value = "auto";
    form.name.focus();
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const typed = form.name.value.trim();
    if (typed) add(typed);
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
  hideScanCard();
  dialog.showModal();
  holdScannerUpright();
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
  releaseScannerUpright();
}

// The scanner stays where it was when the phone is turned sideways, like a camera app. Where the browser can lock the
// screen (an installed app or full screen on Android), it does. Elsewhere the page still turns, so the dialog is turned
// back by the same angle, and the video is turned forward inside it so the picture still lines up with the world.
function screenAngle() {
  const a = screen.orientation?.angle ?? window.orientation ?? 0;
  return ((a % 360) + 360) % 360;
}

function orientScanner() {
  const dialog = $("#scanner");
  const a = dialog.open ? screenAngle() : 0;
  dialog.style.setProperty("--turn", `${a}deg`);
  dialog.classList.toggle("turned", a !== 0);
  dialog.classList.toggle("sideways", a === 90 || a === 270);
}

function holdScannerUpright() {
  screen.orientation?.lock?.("portrait").catch(() => {});
  orientScanner();
  screen.orientation?.addEventListener("change", orientScanner);
  window.addEventListener("orientationchange", orientScanner);
}

function releaseScannerUpright() {
  try { screen.orientation?.unlock?.(); } catch {}
  screen.orientation?.removeEventListener("change", orientScanner);
  window.removeEventListener("orientationchange", orientScanner);
  orientScanner();
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

// Resolves once the item is saved, with the item, a message saying what happened, and the item as it was before (null
// for a new one). A barcode already in the pantry isn't added again: the server raises that item's quantity instead.
// The list reloads in the background, so a beep doesn't wait.
async function addToPantry(name, quantity, barcode) {
  const prior = state.items.find((i) => i.barcode === barcode && i.section === state.pantry);
  const before = prior ? { ...prior } : null;
  const item = await api("POST", "/api/items", { name, quantity, barcode, section: state.pantry });
  guard(refresh);
  const message = before ? `${item.name} is now ${item.quantity || "1"}` : `Added ${item.name}`;
  return { item, before, message };
}

// "Quantity" is free text. These step the count at its front, the way the server's _one_more does:
// "" → "2", "3" → "4", "400 g" → "2 × 400 g", and back down again. Null when it can't go lower.
const COUNT_OF = /^(\d+)(?:\s*[×x]\s*(.+))?$/;
function oneMore(q) {
  const m = q.trim().match(COUNT_OF);
  if (!m) return q.trim() ? `2 × ${q.trim()}` : "2";
  const n = Number(m[1]) + 1;
  return m[2] ? `${n} × ${m[2]}` : String(n);
}
function oneLess(q) {
  const m = q.trim().match(COUNT_OF);
  if (!m || Number(m[1]) <= 1) return null;
  const n = Number(m[1]) - 1;
  if (m[2]) return n === 1 ? m[2] : `${n} × ${m[2]}`;
  return String(n);
}

// The card under the camera for the item the last scan added: change its quantity, or take the scan back if it was
// the wrong item. Taking it back deletes a new item, and puts an item that was already there back to its old quantity.
function showScanCard({ item, before }) {
  const card = $("#scan-card");
  const status = $("#scan-status");
  const qty = el("input", { value: item.quantity, maxlength: 200, placeholder: "1", "aria-label": `Quantity of ${item.name}` });
  const minus = el("button", { type: "button", class: "ghost", "aria-label": "One less" }, "−");
  const sync = () => { minus.disabled = oneLess(qty.value) === null; };
  const save = (value) => guard(async () => {
    qty.value = value;
    sync();
    item = await api("PUT", `/api/items/${item.id}`, { quantity: value });
    status.textContent = `${item.name}: ${item.quantity || "1"}`;
    await refresh();
  });
  minus.onclick = () => { const v = oneLess(qty.value); if (v !== null) save(v); };
  qty.onchange = () => save(qty.value);
  qty.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); qty.blur(); } };
  const undo = el("button", {
    type: "button", class: "ghost danger",
    onclick: () => guard(async () => {
      if (before) await api("PUT", `/api/items/${item.id}`, { quantity: before.quantity });
      else await api("DELETE", `/api/items/${item.id}`);
      hideScanCard();
      status.textContent = before ? `${item.name} is back to ${before.quantity || "1"}. Scan the next item.`
                                  : `Removed ${item.name}. Scan the next item.`;
      await refresh();
    }),
  }, before ? "Undo" : "Delete");
  card.replaceChildren(
    el("div", { class: "name" }, item.name),
    undo,
    el("div", { class: "stepper" },
      minus,
      qty,
      el("button", { type: "button", class: "ghost", "aria-label": "One more", onclick: () => save(oneMore(qty.value)) }, "+")),
  );
  sync();
  card.hidden = false;
}

function hideScanCard() {
  $("#scan-card").hidden = true;
  $("#scan-card").replaceChildren();
}

// One barcode from the camera. Called for every frame that has one, so it skips while the last one is still being
// handled, and skips the same barcode for a second after (SAME_CODE_MS).
async function onScanned(code) {
  const now = Date.now();
  if (scanner.busy || scanner.unknown.has(code) || (scanner.last?.code === code && now - scanner.last.at < SAME_CODE_MS)) return;
  scanner.busy = true;
  const status = $("#scan-status");
  let added;
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
      added = await addToPantry(info.name, info.quantity || "", code);
    } catch (e) {
      beep(false);
      status.textContent = `Couldn't add ${info.name}: ${e.message}`;
      return;
    }
    beep();
    status.textContent = `${added.message}. Scan the next item.`;
    showScanCard(added);
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
    toast(`${(await addToPantry(info.name, info.quantity || "", code)).message} in the pantry`);
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
    toast(`${await addToPantry(name, form.quantity.value, form.barcode.value)} in the pantry`);
  });
});

// ---------- Boot ----------
async function boot() {
  $("#signin-btn").addEventListener("click", (e) => signIn(e.currentTarget));
  $("#signout-btn").addEventListener("click", signOut);
  $("#feedback-btn").addEventListener("click", () => showView("feedback"));
  wireShare();
  wireSiri();
  wireSettings();
  takeJoinCode();

  let signInError = "";
  if (hasIdCallback()) {
    try { await finishIdSignIn(); } catch (e) { signInError = e.message; }
  }
  const me = await fetchMe();
  if (!me) return showSignIn(signInError);
  if (signInError) toast(signInError); // e.g. a stale sign-in link, while an earlier session is still good
  state.me = me;
  applySettings(me.settings || {});
  $("#signout-btn").hidden = false;
  $("#share-btn").hidden = false;
  $("#siri-btn").hidden = false;
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
  wireAddForm($("#add-form"), async (name) => {
    const form = $("#add-form");
    await api("POST", "/api/items", { name, quantity: form.quantity.value, section: form.section.value });
    await refresh();
  });
  wireAddForm($("#pantry-add-form"), async (name) => {
    await addToPantry(name, $("#pantry-add-form").quantity.value);
    toast(`Added ${name} to pantry`);
  });
  wireRecipes();

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

startHotDogMan({
  allowed: () => state.settings.hotDogs !== false, // on until someone signed in turns it off
  // The first time someone signed in sends him away, the "I like hot dogs" setting appears.
  onDismiss: () => { if (state.me && !state.settings.metHotDogMan) saveSettings({ metHotDogMan: true }); },
});
guard(boot);
