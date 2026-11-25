#!/usr/bin/env python3
"""Shopping list web app. Python standard library, plus `cryptography` to check 6j0.org sign-in tokens.

    ID_ORIGIN=https://id.6j0.org python3 server.py                     # http://localhost:8000
    ID_ORIGIN=… python3 server.py --host 0.0.0.0 --cert cert.pem --key key.pem   # HTTPS (needed for phone cameras)
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import idtoken
import recipes
from recipes import RecipeError
import sections
from feedback import Feedback, FeedbackError

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
USER_AGENT = "ShoppingList/0.1 (self-hosted grocery list)"
SESSION_DAYS = 30
SESSION_MS = SESSION_DAYS * 86400 * 1000
INVITE_DAYS = 7
INVITE_MS = INVITE_DAYS * 86400 * 1000
DAY_MS = 86400 * 1000
# Daily limits on recipe imports (each a paid Claude API call) and generations (each ties up the recipe generator for
# a while), per account and for everyone together: kind -> (per account, everyone).
RECIPE_LIMITS = {"import": (20, 200), "generate": (30, 500)}

_db_lock = threading.Lock()
_db = None
_feedback = None

# Sign-in (see main()): people sign in with an identity from a 6j0.org identity server. Each identity gets its own
# list (and pantry), and can share it by invite link.
_auth = {
    "id_origin": "",
    "id_app": "shopping-list",
    "jwks_url": "",
    "admin_sub": "",
    "secure_cookie": "",
}


def _now_ms():
    return int(time.time() * 1000)


def init_db(path):
    global _db
    _db = sqlite3.connect(path, check_same_thread=False)
    _db.row_factory = sqlite3.Row
    _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            quantity     TEXT NOT NULL DEFAULT '',
            section      TEXT NOT NULL,
            section_auto INTEGER NOT NULL DEFAULT 1,
            checked      INTEGER NOT NULL DEFAULT 0,
            barcode      TEXT,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS barcode_cache (
            code       TEXT PRIMARY KEY,
            result     TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        -- One row per 6j0.org identity (sub) that has signed in. guid is a random public ID for the account (never
        -- the sub), which feedback posts are filed under.
        CREATE TABLE IF NOT EXISTS users (
            id      INTEGER PRIMARY KEY,
            sub     TEXT NOT NULL UNIQUE,
            guid    TEXT NOT NULL UNIQUE,
            created INTEGER NOT NULL
        );
        -- The session cookie holds a random token; only its SHA-256 is stored.
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            created    INTEGER NOT NULL
        );
        -- A personal API token, at most one per user, for adding items from outside the app (an iOS Shortcut run
        -- by Siri, say). Only its SHA-256 is stored. It belongs to the user, not the list, so it follows them to
        -- whatever list they're on.
        CREATE TABLE IF NOT EXISTS api_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL UNIQUE,
            created    INTEGER NOT NULL,
            last_used  INTEGER NOT NULL DEFAULT 0
        );
        -- A list and its pantry. Its members are the users whose list_id points at it; items belong to one list.
        CREATE TABLE IF NOT EXISTS lists (
            id      INTEGER PRIMARY KEY,
            created INTEGER NOT NULL
        );
        -- Single-use invite links to join a list. Only the code's SHA-256 is stored.
        CREATE TABLE IF NOT EXISTS invites (
            code_hash TEXT PRIMARY KEY,
            list_id   INTEGER NOT NULL,
            created   INTEGER NOT NULL
        );
        -- The recipe book, one for everyone. Each recipe is JSON {title, servings, source, origin, ingredients,
        -- steps} (see clean_recipe). author is the user who added it, who (with the admin) may change it; NULL once
        -- their account is erased.
        CREATE TABLE IF NOT EXISTS recipe_book (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            author  INTEGER,
            data    TEXT NOT NULL,
            created INTEGER NOT NULL,
            updated INTEGER NOT NULL
        );
        -- When each account imported or generated recipes ("import", "generate"), for the daily limits.
        CREATE TABLE IF NOT EXISTS recipe_uses (
            user_id INTEGER NOT NULL,
            kind    TEXT NOT NULL,
            at      INTEGER NOT NULL
        );
        -- The cache of the TheMealDB catalog the app used before the recipe book.
        DROP TABLE IF EXISTS recipes;
        DROP TABLE IF EXISTS meta;
        """
    )
    # Databases from before there was more than one list: their items and users have no list_id yet.
    for table in ("items", "users"):
        columns = {r["name"] for r in _db.execute(f"PRAGMA table_info({table})")}
        if "list_id" not in columns:
            _db.execute(f"ALTER TABLE {table} ADD COLUMN list_id INTEGER")
    # Each person's settings (see SETTINGS), as JSON. Databases from before there were settings lack the column.
    if "settings" not in {r["name"] for r in _db.execute("PRAGMA table_info(users)")}:
        _db.execute("ALTER TABLE users ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'")
    _db.execute("CREATE INDEX IF NOT EXISTS items_list ON items (list_id)")
    _db.commit()


def claim_unowned(subs):
    """Upgrade a database from when there was one list, shared by ALLOWED_SUBS: put the items that belong to no list,
    and those identities, on one list. Identities that already have a list keep it. Returns a line to log, or None."""
    subs = sorted(subs)
    with _db_lock:
        orphans = _db.execute("SELECT COUNT(*) FROM items WHERE list_id IS NULL").fetchone()[0]
        if not subs:
            if orphans:
                return (f"{orphans} items from before lists were per person belong to no list. Set ALLOWED_SUBS "
                        "to the identities that shared it, and restart, to give it back to them.")
            return None
        rows = _db.execute(
            f"SELECT sub, list_id FROM users WHERE sub IN ({','.join('?' * len(subs))})", subs
        ).fetchall()
        has_list = {r["sub"]: r["list_id"] for r in rows if r["list_id"] is not None}
        homeless = [s for s in subs if s not in has_list]
        if not orphans and not homeless:
            return None
        target = next(iter(has_list.values()), None) or _new_list()
        _db.execute("UPDATE items SET list_id = ? WHERE list_id IS NULL", (target,))
        for sub in homeless:
            if not _db.execute("UPDATE users SET list_id = ? WHERE sub = ?", (target, sub)).rowcount:
                _db.execute(
                    "INSERT INTO users (sub, guid, created, list_id) VALUES (?, ?, ?, ?)",
                    (sub, secrets.token_hex(16), _now_ms(), target),
                )
        _db.commit()
    return f"ALLOWED_SUBS: put {orphans} unowned {'item' if orphans == 1 else 'items'} and {len(homeless)} {'identity' if len(homeless) == 1 else 'identities'} on list {target}."


# --- Accounts and sessions ------------------------------------------------


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _new_list():
    """Make an empty list. The caller holds _db_lock and commits."""
    return _db.execute("INSERT INTO lists (created) VALUES (?)", (_now_ms(),)).lastrowid


def _drop_list(list_id):
    """Delete a list nobody is on any more, with its items and invites. The caller holds _db_lock and commits."""
    _db.execute("DELETE FROM items WHERE list_id = ?", (list_id,))
    _db.execute("DELETE FROM invites WHERE list_id = ?", (list_id,))
    _db.execute("DELETE FROM lists WHERE id = ?", (list_id,))


def _member_count(list_id):
    return _db.execute("SELECT COUNT(*) FROM users WHERE list_id = ?", (list_id,)).fetchone()[0]


def account_for(sub):
    """Find or create the account for a verified 6j0.org identity, with a new, empty list of its own if it has none.
    Returns its users.id."""
    with _db_lock:
        row = _db.execute("SELECT id, list_id FROM users WHERE sub = ?", (sub,)).fetchone()
        if row and row["list_id"] is not None:
            return row["id"]
        list_id = _new_list()
        if row:
            _db.execute("UPDATE users SET list_id = ? WHERE id = ?", (list_id, row["id"]))
            user_id = row["id"]
        else:
            user_id = _db.execute(
                "INSERT INTO users (sub, guid, created, list_id) VALUES (?, ?, ?, ?)",
                (sub, secrets.token_hex(16), _now_ms(), list_id),
            ).lastrowid
        _db.commit()
        return user_id


def new_session_cookie(user_id):
    token = secrets.token_urlsafe(32)
    now = _now_ms()
    with _db_lock:
        _db.execute("DELETE FROM sessions WHERE created <= ?", (now - SESSION_MS,))
        _db.execute(
            "INSERT INTO sessions (token_hash, user_id, created) VALUES (?, ?, ?)",
            (_token_hash(token), user_id, now),
        )
        _db.commit()
    return f"sid={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_DAYS * 86400}{_auth['secure_cookie']}"


def session_user(token):
    """The signed-in account for a session token, as {id, sub, guid, list_id}, or None."""
    if not token:
        return None
    with _db_lock:
        row = _db.execute(
            """SELECT u.id, u.sub, u.guid, u.list_id FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.created > ?""",
            (_token_hash(token), _now_ms() - SESSION_MS),
        ).fetchone()
    if not row:
        return None
    user = dict(row)
    if user["list_id"] is None:  # signed in before lists were per person, and not in ALLOWED_SUBS
        account_for(user["sub"])
        return session_user(token)
    return user


def end_session(token):
    with _db_lock:
        _db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
        _db.commit()


def erase_account(sub):
    """The identity was deleted: erase its account, sessions, and feedback, and its list if nobody else is on it.
    Recipes it added stay in the recipe book, without an author."""
    with _db_lock:
        row = _db.execute("SELECT id, guid, list_id FROM users WHERE sub = ?", (sub,)).fetchone()
        if not row:
            return
        _db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        _db.execute("DELETE FROM api_tokens WHERE user_id = ?", (row["id"],))
        _db.execute("DELETE FROM recipe_uses WHERE user_id = ?", (row["id"],))
        _db.execute("UPDATE recipe_book SET author = NULL WHERE author = ?", (row["id"],))
        _db.execute("DELETE FROM users WHERE id = ?", (row["id"],))
        if row["list_id"] is not None and _member_count(row["list_id"]) == 0:
            _drop_list(row["list_id"])
        _db.commit()
    _feedback.erase_user(row["guid"])


# --- Settings -------------------------------------------------------------
# Which optional controls the app shows (the Settings tab). Per person, not per list, so people sharing a list can
# set them differently. Each name maps to its default.

SETTINGS = {
    "search": False,
    "quantity": False,
    "aisle": False,
    "barcodeTyping": False,
    "hotDogs": True,  # Hot Dog Man may show up; turned off, he never does
    "metHotDogMan": False,  # has clicked him away once, which is when the hotDogs setting appears
}


def get_settings(user):
    with _db_lock:
        row = _db.execute("SELECT settings FROM users WHERE id = ?", (user["id"],)).fetchone()
    try:
        saved = json.loads(row["settings"]) if row else {}
    except ValueError:
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    return {k: saved[k] if isinstance(saved.get(k), bool) else default for k, default in SETTINGS.items()}


def update_settings(user, data):
    """Change any of SETTINGS (each true or false); the rest stay. Returns them all."""
    for k, v in data.items():
        if k not in SETTINGS:
            raise BadRequest(f"unknown setting: {k}")
        if not isinstance(v, bool):
            raise BadRequest(f"{k} must be true or false")
    settings = {**get_settings(user), **data}
    with _db_lock:
        _db.execute("UPDATE users SET settings = ? WHERE id = ?", (json.dumps(settings), user["id"]))
        _db.commit()
    return settings


# --- API tokens (Siri and other shortcuts) --------------------------------
# A token can only add items to its owner's list (POST /api/shortcut/items). Making a new one replaces the old one.

API_TOKEN_PREFIX = "sl_"


def api_token_info(user):
    with _db_lock:
        row = _db.execute("SELECT created, last_used FROM api_tokens WHERE user_id = ?", (user["id"],)).fetchone()
    return {"token": None, "created": row["created"], "lastUsed": row["last_used"]} if row else {"token": None}


def new_api_token(user):
    """Make the user a new token, replacing any old one. The token itself is only ever shown this once."""
    token = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = _now_ms()
    with _db_lock:
        _db.execute("DELETE FROM api_tokens WHERE user_id = ?", (user["id"],))
        _db.execute(
            "INSERT INTO api_tokens (token_hash, user_id, created) VALUES (?, ?, ?)",
            (_token_hash(token), user["id"], now),
        )
        _db.commit()
    return {"token": token, "created": now, "lastUsed": 0}


def revoke_api_token(user):
    with _db_lock:
        _db.execute("DELETE FROM api_tokens WHERE user_id = ?", (user["id"],))
        _db.commit()


def api_token_user(token):
    """The account an API token belongs to, as {id, sub, guid, list_id}, or None. Notes when it was used."""
    if not token or not token.startswith(API_TOKEN_PREFIX):
        return None
    with _db_lock:
        row = _db.execute(
            """SELECT u.id, u.sub, u.guid, u.list_id FROM api_tokens t JOIN users u ON u.id = t.user_id
               WHERE t.token_hash = ?""",
            (_token_hash(token),),
        ).fetchone()
        if row:
            _db.execute("UPDATE api_tokens SET last_used = ? WHERE token_hash = ?", (_now_ms(), _token_hash(token)))
            _db.commit()
    if not row:
        return None
    if row["list_id"] is None:  # signed in before lists were per person (see session_user)
        account_for(row["sub"])
        return api_token_user(token)
    return dict(row)


def _fold(name):
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def known_names(list_id):
    """The names a list already uses, for suggesting and spelling item names: the recipe book's ingredients and the
    items on its list and in its pantry. Sorted, without case-only duplicates (a recipe's spelling wins)."""
    with _db_lock:
        books = [r[0] for r in _db.execute("SELECT data FROM recipe_book")]
        items = [r[0] for r in _db.execute("SELECT name FROM items WHERE list_id = ?", (list_id,))]
    names = [i["name"] for data in books for i in json.loads(data)["ingredients"]] + items
    unique = {}
    for n in names:
        unique.setdefault(n.lower(), n)
    return sorted(unique.values(), key=str.lower)


def canonical_name(list_id, name):
    """Dictation says "peanut butter." for "Peanut Butter": drop the trailing period, and use the spelling the list
    already uses (known_names), ignoring case and punctuation. Otherwise the name as given."""
    name = name.strip().rstrip(".").strip()
    folded = _fold(name)
    return next((n for n in known_names(list_id) if _fold(n) == folded), name)


def shortcut_add(user, data):
    """Add an item from a shortcut. The reply's "message" is meant to be spoken back."""
    name = canonical_name(user["list_id"], _clean_text(data, "name", required=True))
    if not name:
        raise BadRequest("name is required")
    item, _ = create_item(user["list_id"], {"name": name, "quantity": _clean_text(data, "quantity")})
    return {"item": item, "message": f"Added {item['name']} to the shopping list."}


# --- Sharing a list -------------------------------------------------------
# Everyone on a list can do everything with it: invite people, and take people off it. Someone taken off a list (or
# who leaves) starts over with a new, empty list.


def list_info(user):
    """The people on the user's list. Each is shown by the account's random public ID, never its sub."""
    with _db_lock:
        rows = _db.execute("SELECT guid FROM users WHERE list_id = ? ORDER BY created, id", (user["list_id"],)).fetchall()
    return {"members": [{"id": r["guid"], "you": r["guid"] == user["guid"]} for r in rows]}


def create_invite(user):
    code = secrets.token_urlsafe(18)
    now = _now_ms()
    with _db_lock:
        _db.execute("DELETE FROM invites WHERE created <= ?", (now - INVITE_MS,))
        _db.execute(
            "INSERT INTO invites (code_hash, list_id, created) VALUES (?, ?, ?)",
            (_token_hash(code), user["list_id"], now),
        )
        _db.commit()
    return {"code": code, "days": INVITE_DAYS}


def join_list(user, code):
    """Accept an invite. If nobody else was on the user's old list, its items come along and the old list is
    deleted; otherwise the old list stays with the people still on it."""
    if not isinstance(code, str) or not code:
        raise BadRequest("code is required")
    with _db_lock:
        invite = _db.execute(
            "SELECT list_id FROM invites WHERE code_hash = ? AND created > ?",
            (_token_hash(code), _now_ms() - INVITE_MS),
        ).fetchone()
        if not invite:
            raise HttpError(HTTPStatus.NOT_FOUND, "That invite link has expired or was already used. Ask for a new one.")
        new, old = invite["list_id"], user["list_id"]
        if new == old:
            raise BadRequest("You're already on this list.")
        _db.execute("DELETE FROM invites WHERE code_hash = ?", (_token_hash(code),))
        _db.execute("UPDATE users SET list_id = ? WHERE id = ?", (new, user["id"]))
        moved = 0
        if _member_count(old) == 0:
            moved = _db.execute("UPDATE items SET list_id = ? WHERE list_id = ?", (new, old)).rowcount
            _drop_list(old)
        _db.commit()
    return {**list_info({**user, "list_id": new}), "moved": moved}


def remove_member(user, guid):
    """Take someone (or, with your own ID, yourself) off the user's list. They get a new, empty list."""
    with _db_lock:
        target = _db.execute(
            "SELECT id FROM users WHERE guid = ? AND list_id = ?", (guid, user["list_id"])
        ).fetchone()
        if not target:
            raise HttpError(HTTPStatus.NOT_FOUND, "That person isn't on this list.")
        if _member_count(user["list_id"]) == 1:
            raise BadRequest("You're the only one on this list.")
        new = _new_list()
        _db.execute("UPDATE users SET list_id = ? WHERE id = ?", (new, target["id"]))
        _db.commit()
    return list_info({**user, "list_id": new} if target["id"] == user["id"] else user)


def feedback_who(user):
    return {"user": user["guid"], "admin": bool(_auth["admin_sub"]) and user["sub"] == _auth["admin_sub"]}


def row_to_dict(row):
    d = dict(row)
    d["section_auto"] = bool(d["section_auto"])
    d["checked"] = bool(d["checked"])
    return d


def list_items(list_id):
    with _db_lock:
        rows = _db.execute("SELECT * FROM items WHERE list_id = ?", (list_id,)).fetchall()
    items = [row_to_dict(r) for r in rows]
    items.sort(
        key=lambda i: (
            sections.SECTION_ORDER.get(i["section"], len(sections.SECTION_ORDER)),
            i["checked"],
            -i["id"] if i["section"] == sections.PANTRY else 0,  # pantry: newest first, to spot a bad scan
            i["name"].lower(),
        )
    )
    return items


def get_item(list_id, item_id):
    with _db_lock:
        row = _db.execute("SELECT * FROM items WHERE id = ? AND list_id = ?", (item_id, list_id)).fetchone()
    return row_to_dict(row) if row else None


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class BadRequest(HttpError):
    def __init__(self, message):
        super().__init__(HTTPStatus.BAD_REQUEST, message)


def _clean_section(value, name):
    """Return (section, auto). Empty/'auto' section means classify from name."""
    if value in (None, "", "auto"):
        return sections.classify(name), True
    if value not in sections.ALL_SECTIONS:
        raise BadRequest(f"unknown section: {value}")
    return value, False


def _clean_text(data, key, required=False, limit=200):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise BadRequest(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise BadRequest(f"{key} is required")
    if len(value) > limit:
        raise BadRequest(f"{key} is too long (max {limit})")
    return value


COUNT_OF = re.compile(r"^(\d+)(?:\s*[×x]\s*(.+))?$")


def _one_more(quantity):
    """The quantity after one more of the same thing: "" → "2", "3" → "4", "400 g" → "2 × 400 g",
    "2 × 400 g" → "3 × 400 g". Quantity is free text, so anything else gets "2 × " in front."""
    m = COUNT_OF.match(quantity)
    if not m:
        return f"2 × {quantity}" if quantity else "2"
    count, rest = int(m.group(1)) + 1, m.group(2)
    return f"{count} × {rest}" if rest else str(count)


def create_item(list_id, data):
    """Returns (item, created). A barcode already in the pantry isn't added again: its quantity goes up by one."""
    name = _clean_text(data, "name", required=True)
    quantity = _clean_text(data, "quantity")
    barcode = _clean_text(data, "barcode", limit=64) or None
    section, auto = _clean_section(data.get("section"), name)
    with _db_lock:
        row = None
        if barcode and section == sections.PANTRY:
            row = _db.execute(
                "SELECT id, quantity FROM items WHERE list_id = ? AND section = ? AND barcode = ? ORDER BY id LIMIT 1",
                (list_id, section, barcode),
            ).fetchone()
        if row:
            item_id = row["id"]
            _db.execute(
                "UPDATE items SET quantity = ?, updated_at = datetime('now') WHERE id = ?",
                (_one_more(row["quantity"]), item_id),
            )
        else:
            item_id = _db.execute(
                "INSERT INTO items (name, quantity, section, section_auto, barcode, list_id) VALUES (?, ?, ?, ?, ?, ?)",
                (name, quantity, section, int(auto), barcode, list_id),
            ).lastrowid
        _db.commit()
    return get_item(list_id, item_id), not row


def update_item(list_id, item_id, data):
    item = get_item(list_id, item_id)
    if not item:
        return None
    name = _clean_text(data, "name", required=True) if "name" in data else item["name"]
    quantity = _clean_text(data, "quantity") if "quantity" in data else item["quantity"]
    checked = bool(data["checked"]) if "checked" in data else item["checked"]
    if "section" in data:
        section, auto = _clean_section(data["section"], name)
    elif item["section_auto"]:
        section, auto = sections.classify(name), True  # re-guess on rename
    else:
        section, auto = item["section"], False
    with _db_lock:
        _db.execute(
            """UPDATE items SET name = ?, quantity = ?, section = ?, section_auto = ?,
               checked = ?, updated_at = datetime('now') WHERE id = ? AND list_id = ?""",
            (name, quantity, section, int(auto), int(checked), item_id, list_id),
        )
        _db.commit()
    return get_item(list_id, item_id)


def delete_item(list_id, item_id):
    with _db_lock:
        cur = _db.execute("DELETE FROM items WHERE id = ? AND list_id = ?", (item_id, list_id))
        _db.commit()
    return cur.rowcount > 0


def clear_checked(list_id):
    with _db_lock:
        cur = _db.execute(
            "DELETE FROM items WHERE list_id = ? AND checked = 1 AND section != ?", (list_id, sections.PANTRY)
        )
        _db.commit()
    return cur.rowcount


# --- Barcode lookup -------------------------------------------------------


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.load(resp)


def _lookup_open_food_facts(code):
    fields = "product_name,product_name_en,generic_name,brands,quantity,categories_tags"
    # UPC-A (12 digits) is often stored as EAN-13 with a leading zero.
    candidates = [code, "0" + code] if len(code) == 12 else [code]
    for candidate in candidates:
        url = f"https://world.openfoodfacts.org/api/v2/product/{candidate}.json?fields={fields}"
        data = _fetch_json(url)
        if data.get("status") == 1:
            break
    else:
        return None
    p = data.get("product", {})
    name = p.get("product_name_en") or p.get("product_name") or p.get("generic_name")
    if not name:
        return None
    brand = (p.get("brands") or "").split(",")[0].strip()
    if brand and brand.lower() not in name.lower():
        name = f"{brand} {name}"
    return {
        "name": name.strip(),
        "quantity": (p.get("quantity") or "").strip(),
        "section_hint": sections.classify_from_tags(p.get("categories_tags") or []),
        "source": "Open Food Facts",
    }


def _lookup_upcitemdb(code):
    # Free trial endpoint: no key, ~100 lookups/day per IP. Good for non-food items.
    data = _fetch_json(f"https://api.upcitemdb.com/prod/trial/lookup?upc={code}")
    items = data.get("items") or []
    if not items or not items[0].get("title"):
        return None
    return {
        "name": items[0]["title"].strip(),
        "quantity": (items[0].get("size") or "").strip(),
        "section_hint": None,
        "source": "UPCitemdb",
    }


def lookup_barcode(code):
    with _db_lock:
        row = _db.execute("SELECT result FROM barcode_cache WHERE code = ?", (code,)).fetchone()
    if row:
        return json.loads(row["result"])

    result, errors = None, []
    for fn in (_lookup_open_food_facts, _lookup_upcitemdb):
        try:
            result = fn(code)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            errors.append(f"{fn.__name__}: {e}")
            continue
        if result:
            break

    if result is None and errors and len(errors) == 2:
        # Both lookups failed (network trouble); don't cache the miss.
        return {"found": False, "code": code, "error": "lookup failed: " + "; ".join(errors)}

    result = {"found": bool(result), "code": code, **(result or {})}
    with _db_lock:
        _db.execute(
            "INSERT OR REPLACE INTO barcode_cache (code, result) VALUES (?, ?)",
            (code, json.dumps(result)),
        )
        _db.commit()
    return result


# --- Recipes --------------------------------------------------------------
# One recipe book for everyone. A recipe is written by hand, read from a link or pasted text by Claude
# (recipes.import_recipe), or picked from the generator's suggestions (recipes.generate) and saved. Everyone can see
# every recipe; only the person who added it, and the admin, can change or delete it. Which ingredients you have
# comes from your own list's pantry.

ORIGINS = ("hand", "link", "generated")


def _clean_list(data, key, limit):
    value = data.get(key, [])
    if not isinstance(value, list):
        raise BadRequest(f"{key} must be a list")
    if len(value) > limit:
        raise BadRequest(f"too many {key} (max {limit})")
    return value


def clean_recipe(data):
    """{title, servings, source, origin, ingredients: [{quantity, name, note}], steps: [str]}, checked. Blank
    ingredients and steps are dropped."""
    ingredients = []
    for i in _clean_list(data, "ingredients", 100):
        if not isinstance(i, dict):
            raise BadRequest("each ingredient must be an object")
        ing = {"quantity": _clean_text(i, "quantity", limit=100), "name": _clean_text(i, "name"),
               "note": _clean_text(i, "note")}
        if ing["name"]:
            ingredients.append(ing)
        elif ing["quantity"] or ing["note"]:
            raise BadRequest("each ingredient needs a name")
    steps = []
    for step in _clean_list(data, "steps", 100):
        step = _clean_text({"step": step}, "step", limit=2000)
        if step:
            steps.append(step)
    source = _clean_text(data, "source", limit=2000)
    if source:
        try:
            source = recipes.clean_url(source)
        except RecipeError:
            raise BadRequest("source must be a web link (https://…)")
    origin = data.get("origin") or "hand"
    if origin not in ORIGINS:
        raise BadRequest(f"origin must be one of {', '.join(ORIGINS)}")
    return {
        "title": _clean_text(data, "title", required=True),
        "servings": _clean_text(data, "servings", limit=100),
        "source": source,
        "origin": origin,
        "ingredients": ingredients,
        "steps": steps,
    }


def _may_change(user, author):
    return author == user["id"] or feedback_who(user)["admin"]


def _recipe_out(user, row):
    """A stored recipe as the API shows it. The author's account isn't shown, only whether this user may change it."""
    return {**json.loads(row["data"]), "id": row["id"], "mine": row["author"] == user["id"],
            "canEdit": _may_change(user, row["author"])}


def list_recipes(user):
    with _db_lock:
        rows = _db.execute("SELECT id, author, data FROM recipe_book").fetchall()
    return [_recipe_out(user, r) for r in rows]


def get_recipe(user, recipe_id):
    with _db_lock:
        row = _db.execute("SELECT id, author, data FROM recipe_book WHERE id = ?", (recipe_id,)).fetchone()
    return _recipe_out(user, row) if row else None


def create_recipe(user, data):
    recipe = clean_recipe(data)
    now = _now_ms()
    with _db_lock:
        recipe_id = _db.execute(
            "INSERT INTO recipe_book (author, data, created, updated) VALUES (?, ?, ?, ?)",
            (user["id"], json.dumps(recipe), now, now),
        ).lastrowid
        _db.commit()
    return {**recipe, "id": recipe_id, "mine": True, "canEdit": True}


def _author_check(user, recipe_id):
    """Raise unless the recipe exists and the user may change it. The caller holds _db_lock."""
    row = _db.execute("SELECT author FROM recipe_book WHERE id = ?", (recipe_id,)).fetchone()
    if not row:
        raise HttpError(HTTPStatus.NOT_FOUND, "no such recipe")
    if not _may_change(user, row["author"]):
        raise HttpError(HTTPStatus.FORBIDDEN, "Only the person who added this recipe can change it.")


def update_recipe(user, recipe_id, data):
    recipe = clean_recipe(data)
    with _db_lock:
        _author_check(user, recipe_id)
        _db.execute("UPDATE recipe_book SET data = ?, updated = ? WHERE id = ?",
                    (json.dumps(recipe), _now_ms(), recipe_id))
        _db.commit()
    return get_recipe(user, recipe_id)


def delete_recipe(user, recipe_id):
    with _db_lock:
        _author_check(user, recipe_id)
        _db.execute("DELETE FROM recipe_book WHERE id = ?", (recipe_id,))
        _db.commit()


def _count_use(user, kind):
    """Note an import or a generation, or refuse it when it's over the daily limits (RECIPE_LIMITS)."""
    per_account, per_day = RECIPE_LIMITS[kind]
    verb = "imported" if kind == "import" else "asked for meal ideas"
    now = _now_ms()
    with _db_lock:
        _db.execute("DELETE FROM recipe_uses WHERE at <= ?", (now - DAY_MS,))
        mine = _db.execute("SELECT COUNT(*) FROM recipe_uses WHERE user_id = ? AND kind = ?",
                           (user["id"], kind)).fetchone()[0]
        everyone = _db.execute("SELECT COUNT(*) FROM recipe_uses WHERE kind = ?", (kind,)).fetchone()[0]
        if mine >= per_account:
            raise HttpError(HTTPStatus.TOO_MANY_REQUESTS,
                            f"You've {verb} {per_account} times today. Try again tomorrow, or write a recipe in by hand.")
        if everyone >= per_day:
            raise HttpError(HTTPStatus.TOO_MANY_REQUESTS,
                            "The app has done all it can of that today. Try again tomorrow, or write a recipe in by hand.")
        _db.execute("INSERT INTO recipe_uses (user_id, kind, at) VALUES (?, ?, ?)", (user["id"], kind, now))
        _db.commit()


def import_recipe(user, data):
    """Read a recipe from {url} or {text} with Claude and add it to the recipe book."""
    if not recipes.can_import():
        raise HttpError(HTTPStatus.SERVICE_UNAVAILABLE, "Importing recipes isn't set up on this server.")
    url = _clean_text(data, "url", limit=2000)
    text = _clean_text(data, "text", limit=recipes.MAX_TEXT_CHARS)
    if not url and not text:
        raise BadRequest("Paste a link to a recipe, or the recipe's text.")
    if url:
        recipes.clean_url(url)  # a bad link doesn't count against the limit
    _count_use(user, "import")
    return create_recipe(user, recipes.import_recipe(url=url, text=text))


def generate_recipes(user, data):
    """Meal ideas from {ingredients: [name]}: a few recipe drafts, not stored. Saving one is a POST /api/recipes."""
    if not recipes.can_generate():
        raise HttpError(HTTPStatus.SERVICE_UNAVAILABLE, "Meal ideas aren't set up on this server.")
    names = []
    for name in _clean_list(data, "ingredients", recipes.MAX_GENERATE_INGREDIENTS):
        name = _clean_text({"name": name}, "name", limit=100)
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)
    if not names:
        raise BadRequest("Pick at least one ingredient.")
    _count_use(user, "generate")
    return {"recipes": recipes.generate(names)}


def recipe_book(user, max_missing):
    book = list_recipes(user)
    with _db_lock:
        names = [r[0] for r in _db.execute(
            "SELECT name FROM items WHERE list_id = ? AND section = ?", (user["list_id"], sections.PANTRY)
        )]
    return {
        "recipes": recipes.match(book, names, max_missing=max_missing),
        "count": len(book),
        "pantry_size": len(names),
        "staples": sorted(recipes.STAPLES),
        "canImport": recipes.can_import(),
        "canGenerate": recipes.can_generate(),
    }


# --- HTTP -----------------------------------------------------------------

RECIPE_BYTES = 400_000  # a recipe, or a recipe's pasted text
ITEM_PATH = re.compile(r"^/api/items/(\d+)$")
BARCODE_PATH = re.compile(r"^/api/barcode/(\d{6,14})$")
MEMBER_PATH = re.compile(r"^/api/list/members/([0-9a-f]{32})$")
RECIPE_PATH = re.compile(r"^/api/recipes/(\d+)$")
FEEDBACK_PATH = re.compile(r"^/api/feedback/([A-Za-z0-9_-]{1,32})$")
FEEDBACK_REPLY_PATH = re.compile(r"^/api/feedback/([A-Za-z0-9_-]{1,32})/replies/([A-Za-z0-9_-]{1,32})$")
FEEDBACK_ACTION_PATH = re.compile(r"^/api/feedback/([A-Za-z0-9_-]{1,32})/(replies|status)$")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/"):
            return
        super().log_message(fmt, *args)

    def end_headers(self):
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_json(self, status, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status=HTTPStatus.NO_CONTENT):
        self.send_response(status)
        self.end_headers()

    def _read_json(self, limit=16_000):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise BadRequest("request too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BadRequest("invalid JSON")
        if not isinstance(data, dict):
            raise BadRequest("expected a JSON object")
        return data

    def _path(self):
        return urllib.parse.urlparse(self.path).path

    def _session_token(self):
        try:
            morsel = SimpleCookie(self.headers.get("Cookie") or "").get("sid")
        except CookieError:
            return None
        return morsel.value if morsel else None

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    do_PATCH = do_PUT

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        try:
            self._route(method)
        except HttpError as e:
            self._send_json(e.status, {"error": e.message})
        except FeedbackError as e:
            self._send_json(e.status, {"error": e.message})
        except RecipeError as e:
            self._send_json(e.status, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Something went wrong on the server."})

    def _route(self, method):
        path = self._path()

        # The page and its scripts load without a session; it shows a sign-in screen when the API says 401.
        if not path.startswith("/api/"):
            if method != "GET":
                raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
            return super().do_GET()

        # ---- adding items with an API token (an iOS Shortcut run by Siri, say). Nothing else takes a token. It
        # ignores the session cookie, so the cross-site check below, which protects the cookie, doesn't apply. ----
        if path == "/api/shortcut/items":
            return self._route_shortcut(method)

        # State changes only from this app's own pages. (The session cookie is SameSite=Lax as well.)
        site = self.headers.get("Sec-Fetch-Site")
        if method != "GET" and site and site not in ("same-origin", "none"):
            raise HttpError(HTTPStatus.FORBIDDEN, "Cross-site requests aren't allowed.")

        # ---- sign-in (no session needed) ----
        if path == "/api/config" and method == "GET":
            return self._send_json(HTTPStatus.OK, {"idOrigin": _auth["id_origin"], "idApp": _auth["id_app"]})

        # The browser came back from ID_ORIGIN/authorize with a signed token for the nonce it made.
        if path == "/api/id/login" and method == "POST":
            body = self._read_json()
            try:
                claims = idtoken.verify_id_token(
                    body.get("token"), issuer=_auth["id_origin"], audience=_auth["id_app"],
                    nonce=body.get("nonce"), jwks_url=_auth["jwks_url"],
                )
            except idtoken.IdTokenError as e:
                raise HttpError(HTTPStatus.UNAUTHORIZED, str(e))
            except idtoken.JwksError as e:
                print(e, file=sys.stderr)
                raise HttpError(HTTPStatus.BAD_GATEWAY, "Couldn't reach the 6j0.org identity server. Please try again.")
            user_id = account_for(claims["sub"])
            return self._send_json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": new_session_cookie(user_id)})

        # The identity server says an identity was deleted. Answers 200 also when there's no such account, so the
        # identity server stops retrying.
        if path == "/api/id/deleted" and method == "POST":
            body = self._read_json()
            try:
                claims = idtoken.verify_deletion_token(
                    body.get("token"), issuer=_auth["id_origin"], audience=_auth["id_app"], jwks_url=_auth["jwks_url"],
                )
            except idtoken.IdTokenError as e:
                raise HttpError(HTTPStatus.UNAUTHORIZED, str(e))
            except idtoken.JwksError as e:
                print(e, file=sys.stderr)
                raise HttpError(HTTPStatus.BAD_GATEWAY, "Couldn't load the identity server's keys.")
            erase_account(claims["sub"])
            return self._send_json(HTTPStatus.OK, {"ok": True})

        if path == "/api/logout" and method == "POST":
            token = self._session_token()
            if token:
                end_session(token)
            return self._send_json(
                HTTPStatus.OK, {"ok": True},
                {"Set-Cookie": f"sid=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0{_auth['secure_cookie']}"},
            )

        # ---- everything below needs a session ----
        user = session_user(self._session_token())
        if user is None:
            raise HttpError(HTTPStatus.UNAUTHORIZED, "Please sign in.")

        if path == "/api/me" and method == "GET":
            return self._send_json(HTTPStatus.OK, {"isAdmin": feedback_who(user)["admin"], "settings": get_settings(user)})
        if path == "/api/settings" and method in ("PUT", "PATCH"):
            return self._send_json(HTTPStatus.OK, update_settings(user, self._read_json()))

        if path.startswith("/api/feedback"):
            return self._route_feedback(method, path, feedback_who(user))

        # ---- the user's API token ----
        if path == "/api/token":
            if method == "GET":
                return self._send_json(HTTPStatus.OK, api_token_info(user))
            if method == "POST":
                return self._send_json(HTTPStatus.CREATED, new_api_token(user))
            if method == "DELETE":
                revoke_api_token(user)
                return self._send_empty()

        # ---- sharing the list ----
        if path == "/api/list" and method == "GET":
            return self._send_json(HTTPStatus.OK, list_info(user))
        if path == "/api/list/invites" and method == "POST":
            return self._send_json(HTTPStatus.CREATED, create_invite(user))
        if path == "/api/list/join" and method == "POST":
            return self._send_json(HTTPStatus.OK, join_list(user, self._read_json().get("code")))
        m = MEMBER_PATH.match(path)
        if m and method == "DELETE":
            return self._send_json(HTTPStatus.OK, remove_member(user, m.group(1)))

        # ---- the user's list ----
        list_id = user["list_id"]
        if method == "GET":
            return self._route_list_get(user, path)
        if method == "POST":
            if path == "/api/items":
                item, created = create_item(list_id, self._read_json())
                return self._send_json(HTTPStatus.CREATED if created else HTTPStatus.OK, item)
            if path == "/api/items/clear-checked":
                return self._send_json(HTTPStatus.OK, {"deleted": clear_checked(list_id)})
            if path == "/api/recipes":
                return self._send_json(HTTPStatus.CREATED, create_recipe(user, self._read_json(RECIPE_BYTES)))
            if path == "/api/recipes/import":
                return self._send_json(HTTPStatus.CREATED, import_recipe(user, self._read_json(RECIPE_BYTES)))
            if path == "/api/recipes/generate":
                return self._send_json(HTTPStatus.OK, generate_recipes(user, self._read_json()))
        m = RECIPE_PATH.match(path)
        if m and method == "PUT":
            return self._send_json(HTTPStatus.OK, update_recipe(user, int(m.group(1)), self._read_json(RECIPE_BYTES)))
        if m and method == "DELETE":
            delete_recipe(user, int(m.group(1)))
            return self._send_empty()
        m = ITEM_PATH.match(path)
        if m and method == "PUT":
            item = update_item(list_id, int(m.group(1)), self._read_json())
            if not item:
                raise HttpError(HTTPStatus.NOT_FOUND, "no such item")
            return self._send_json(HTTPStatus.OK, item)
        if m and method == "DELETE":
            if not delete_item(list_id, int(m.group(1))):
                raise HttpError(HTTPStatus.NOT_FOUND, "no such item")
            return self._send_empty()
        raise HttpError(HTTPStatus.NOT_FOUND, "not found")

    def _route_shortcut(self, method):
        """POST {"name", "quantity"?} with "Authorization: Bearer <token>". Errors carry a "message" too, so a
        shortcut that speaks "message" says what went wrong."""
        try:
            if method != "POST":
                raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
            scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
            user = api_token_user(token.strip()) if scheme.lower() == "bearer" else None
            if user is None:
                raise HttpError(
                    HTTPStatus.UNAUTHORIZED,
                    "The shopping list didn't accept this shortcut's token. Make a new one in the app, under Siri.",
                )
            return self._send_json(HTTPStatus.CREATED, shortcut_add(user, self._read_json()))
        except HttpError as e:
            return self._send_json(e.status, {"error": e.message, "message": e.message})

    def _route_list_get(self, user, path):
        list_id = user["list_id"]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/api/items":
            return self._send_json(HTTPStatus.OK, list_items(list_id))
        if path == "/api/sections":
            return self._send_json(HTTPStatus.OK, {"sections": sections.SECTIONS, "pantry": sections.PANTRY})
        if path == "/api/ingredients":
            return self._send_json(HTTPStatus.OK, {"ingredients": known_names(list_id)})
        if path == "/api/recipes":
            raw = query.get("max_missing", [""])[0]
            max_missing = int(raw) if raw.isdigit() else None
            return self._send_json(HTTPStatus.OK, recipe_book(user, max_missing))
        m = RECIPE_PATH.match(path)
        if m:
            recipe = get_recipe(user, int(m.group(1)))
            if not recipe:
                raise HttpError(HTTPStatus.NOT_FOUND, "no such recipe")
            return self._send_json(HTTPStatus.OK, recipe)
        if path.startswith("/api/classify"):
            return self._send_json(HTTPStatus.OK, {"section": sections.classify(query.get("name", [""])[0])})
        m = BARCODE_PATH.match(path)
        if m:
            return self._send_json(HTTPStatus.OK, lookup_barcode(m.group(1)))
        raise HttpError(HTTPStatus.NOT_FOUND, "not found")

    def _route_feedback(self, method, path, who):
        if path == "/api/feedback" and method == "GET":
            return self._send_json(HTTPStatus.OK, _feedback.list(who))
        if path == "/api/feedback" and method == "POST":
            return self._send_json(HTTPStatus.CREATED, _feedback.post(who, self._read_json().get("body")))
        m = FEEDBACK_PATH.match(path)
        if m and method == "GET":
            return self._send_json(HTTPStatus.OK, _feedback.thread(who, m.group(1)))
        if m and method == "DELETE":
            _feedback.remove(who, m.group(1))
            return self._send_empty()
        if m and method == "PUT":  # editing your own post
            return self._send_json(HTTPStatus.OK, _feedback.edit(who, m.group(1), self._read_json().get("body")))
        m = FEEDBACK_REPLY_PATH.match(path)
        if m and method == "PUT":  # editing one of your replies
            body = self._read_json().get("body")
            return self._send_json(HTTPStatus.OK, _feedback.edit(who, m.group(1), body, m.group(2)))
        m = FEEDBACK_ACTION_PATH.match(path)
        if m and method == "POST":
            body = self._read_json()
            if m.group(2) == "replies":
                return self._send_json(HTTPStatus.CREATED, _feedback.reply(who, m.group(1), body.get("body")))
            return self._send_json(HTTPStatus.OK, _feedback.set_status(who, m.group(1), body.get("status")))
        raise HttpError(HTTPStatus.NOT_FOUND, "not found")


def _split_subs(value):
    return frozenset(s for s in re.split(r"[\s,]+", value or "") if s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Every option can also be set with an environment variable (for containers). The sign-in ones use the same
    # names as the other 6j0.org apps.
    env = os.environ.get
    ap.add_argument("--host", default=env("SHOPPING_HOST", "127.0.0.1"), help="bind address (0.0.0.0 for your LAN) [SHOPPING_HOST]")
    ap.add_argument("--port", type=int, default=int(env("SHOPPING_PORT", "8000")), help="[SHOPPING_PORT]")
    ap.add_argument("--db", default=env("SHOPPING_DB", str(ROOT / "shopping.db")), help="SQLite file [SHOPPING_DB]")
    ap.add_argument("--feedback", default=env("SHOPPING_FEEDBACK"), help="feedback JSON Lines file (default: feedback.jsonl next to the database) [SHOPPING_FEEDBACK]")
    ap.add_argument("--cert", default=env("SHOPPING_CERT"), help="TLS certificate (PEM) to serve HTTPS [SHOPPING_CERT]")
    ap.add_argument("--key", default=env("SHOPPING_KEY"), help="TLS private key (PEM) [SHOPPING_KEY]")
    ap.add_argument("--id-origin", default=env("ID_ORIGIN"), help="the 6j0.org identity server, e.g. https://id.6j0.org (required) [ID_ORIGIN]")
    ap.add_argument("--id-app", default=env("ID_APP", "shopping-list"), help="this app's name in the identity server's APPS [ID_APP]")
    ap.add_argument("--id-jwks-url", default=env("ID_JWKS_URL"), help="where to fetch the identity server's public keys (default: ID_ORIGIN/.well-known/jwks.json) [ID_JWKS_URL]")
    ap.add_argument("--allowed-subs", default=env("ALLOWED_SUBS", ""), help="upgrading from one shared list: the 6j0.org identities (subs), comma- or space-separated, that shared it [ALLOWED_SUBS]")
    ap.add_argument("--admin-sub", default=env("ADMIN_SUB", ""), help="the 6j0.org identity that can close and delete anyone's feedback [ADMIN_SUB]")
    ap.add_argument("--origin", default=env("ORIGIN", ""), help="this app's public origin, e.g. https://shopping.6j0.org; https: turns on Secure cookies [ORIGIN]")
    args = ap.parse_args()

    origin = (args.id_origin or "").rstrip("/")
    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path or parsed.query:
        ap.error("--id-origin / ID_ORIGIN is required, e.g. ID_ORIGIN=https://id.6j0.org")
    _auth.update(
        id_origin=origin,
        id_app=args.id_app,
        jwks_url=args.id_jwks_url or f"{origin}/.well-known/jwks.json",
        admin_sub=args.admin_sub.strip(),
        secure_cookie="; Secure" if args.origin.startswith("https:") else "",
    )
    global _feedback
    init_db(args.db)
    note = claim_unowned(_split_subs(args.allowed_subs))
    if note:
        print(note)
    _feedback = Feedback(args.feedback or str(Path(args.db).resolve().parent / "feedback.jsonl"))
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    scheme = "http"
    if args.cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    if not recipes.can_import():
        print("ANTHROPIC_API_KEY isn't set, so recipes can't be imported from links.")
    if not recipes.can_generate():
        print("RECIPEGEN_URL isn't set, so the app can't suggest meals.")
    print(f"Shopping list running at {scheme}://{args.host}:{args.port}/  (sign-in: {origin} as \"{args.id_app}\")")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
