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
import sections
from feedback import Feedback, FeedbackError

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
USER_AGENT = "ShoppingList/0.1 (self-hosted grocery list)"
SESSION_DAYS = 30
SESSION_MS = SESSION_DAYS * 86400 * 1000

_db_lock = threading.Lock()
_db = None
_catalog = None
_feedback = None

# Sign-in (see main()): people sign in with an identity from a 6j0.org identity server. The list is shared by the
# identities in allowed_subs; any signed-in identity can use the feedback page.
_auth = {
    "id_origin": "",
    "id_app": "shopping-list",
    "jwks_url": "",
    "allowed_subs": frozenset(),
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
        """
    )
    _db.commit()


# --- Accounts and sessions ------------------------------------------------


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def account_for(sub):
    """Find or create the account for a verified 6j0.org identity. Returns its users.id."""
    with _db_lock:
        row = _db.execute("SELECT id FROM users WHERE sub = ?", (sub,)).fetchone()
        if row:
            return row["id"]
        cur = _db.execute(
            "INSERT INTO users (sub, guid, created) VALUES (?, ?, ?)",
            (sub, secrets.token_hex(16), _now_ms()),
        )
        _db.commit()
        return cur.lastrowid


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
    """The signed-in account for a session token, as {id, sub, guid}, or None."""
    if not token:
        return None
    with _db_lock:
        row = _db.execute(
            """SELECT u.id, u.sub, u.guid FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.created > ?""",
            (_token_hash(token), _now_ms() - SESSION_MS),
        ).fetchone()
    return dict(row) if row else None


def end_session(token):
    with _db_lock:
        _db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
        _db.commit()


def erase_account(sub):
    """The identity was deleted: erase its account, sessions, and feedback. The list itself is shared, so it stays."""
    with _db_lock:
        row = _db.execute("SELECT id, guid FROM users WHERE sub = ?", (sub,)).fetchone()
        if not row:
            return
        _db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        _db.execute("DELETE FROM users WHERE id = ?", (row["id"],))
        _db.commit()
    _feedback.erase_user(row["guid"])


def can_use_list(user):
    return user["sub"] in _auth["allowed_subs"]


def feedback_who(user):
    return {"user": user["guid"], "admin": bool(_auth["admin_sub"]) and user["sub"] == _auth["admin_sub"]}


def row_to_dict(row):
    d = dict(row)
    d["section_auto"] = bool(d["section_auto"])
    d["checked"] = bool(d["checked"])
    return d


def list_items():
    with _db_lock:
        rows = _db.execute("SELECT * FROM items").fetchall()
    items = [row_to_dict(r) for r in rows]
    items.sort(
        key=lambda i: (
            sections.SECTION_ORDER.get(i["section"], len(sections.SECTION_ORDER)),
            i["checked"],
            i["name"].lower(),
        )
    )
    return items


def get_item(item_id):
    with _db_lock:
        row = _db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
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


def create_item(data):
    name = _clean_text(data, "name", required=True)
    quantity = _clean_text(data, "quantity")
    barcode = _clean_text(data, "barcode", limit=64) or None
    section, auto = _clean_section(data.get("section"), name)
    with _db_lock:
        cur = _db.execute(
            "INSERT INTO items (name, quantity, section, section_auto, barcode) VALUES (?, ?, ?, ?, ?)",
            (name, quantity, section, int(auto), barcode),
        )
        _db.commit()
        item_id = cur.lastrowid
    return get_item(item_id)


def update_item(item_id, data):
    item = get_item(item_id)
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
               checked = ?, updated_at = datetime('now') WHERE id = ?""",
            (name, quantity, section, int(auto), int(checked), item_id),
        )
        _db.commit()
    return get_item(item_id)


def delete_item(item_id):
    with _db_lock:
        cur = _db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        _db.commit()
    return cur.rowcount > 0


def clear_checked():
    with _db_lock:
        cur = _db.execute("DELETE FROM items WHERE checked = 1 AND section != ?", (sections.PANTRY,))
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


def recipe_matches(max_missing):
    _catalog.ensure_fresh(background=True)
    all_recipes = _catalog.recipes()
    with _db_lock:
        names = [r[0] for r in _db.execute("SELECT name FROM items WHERE section = ?", (sections.PANTRY,))]
    return {
        "recipes": recipes.match(all_recipes, names, max_missing=max_missing),
        "catalog_size": len(all_recipes),
        "pantry_size": len(names),
        "staples": sorted(recipes.STAPLES),
        "error": _catalog.error,
    }


# --- HTTP -----------------------------------------------------------------

ITEM_PATH = re.compile(r"^/api/items/(\d+)$")
BARCODE_PATH = re.compile(r"^/api/barcode/(\d{6,14})$")
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

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 16_000:
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
            allowed = claims["sub"] in _auth["allowed_subs"]
            if not allowed:
                print(f"sign-in: 6j0.org identity {claims['sub']} isn't in ALLOWED_SUBS, so it can only use feedback")
            return self._send_json(HTTPStatus.OK, {"allowed": allowed}, {"Set-Cookie": new_session_cookie(user_id)})

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

        # id is the identity's sub, shown to people who aren't on the list yet so they can ask to be added.
        if path == "/api/me" and method == "GET":
            who = feedback_who(user)
            return self._send_json(HTTPStatus.OK, {"allowed": can_use_list(user), "id": user["sub"], "isAdmin": who["admin"]})

        # ---- feedback: anyone signed in ----
        if path.startswith("/api/feedback"):
            return self._route_feedback(method, path, feedback_who(user))

        # ---- the list: only identities in ALLOWED_SUBS ----
        if not can_use_list(user):
            raise HttpError(HTTPStatus.FORBIDDEN, "This shopping list is private. Ask its owner to add your 6j0.org ID.")

        if method == "GET":
            return self._route_list_get(path)
        if method == "POST":
            if path == "/api/items":
                return self._send_json(HTTPStatus.CREATED, create_item(self._read_json()))
            if path == "/api/items/clear-checked":
                return self._send_json(HTTPStatus.OK, {"deleted": clear_checked()})
        m = ITEM_PATH.match(path)
        if m and method == "PUT":
            item = update_item(int(m.group(1)), self._read_json())
            if not item:
                raise HttpError(HTTPStatus.NOT_FOUND, "no such item")
            return self._send_json(HTTPStatus.OK, item)
        if m and method == "DELETE":
            if not delete_item(int(m.group(1))):
                raise HttpError(HTTPStatus.NOT_FOUND, "no such item")
            return self._send_empty()
        raise HttpError(HTTPStatus.NOT_FOUND, "not found")

    def _route_list_get(self, path):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/api/items":
            return self._send_json(HTTPStatus.OK, list_items())
        if path == "/api/sections":
            return self._send_json(HTTPStatus.OK, {"sections": sections.SECTIONS, "pantry": sections.PANTRY})
        if path == "/api/recipes":
            raw = query.get("max_missing", [""])[0]
            max_missing = int(raw) if raw.isdigit() else None
            return self._send_json(HTTPStatus.OK, recipe_matches(max_missing))
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
    ap.add_argument("--allowed-subs", default=env("ALLOWED_SUBS", ""), help="6j0.org identities (subs), comma- or space-separated, that share the list [ALLOWED_SUBS]")
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
        allowed_subs=_split_subs(args.allowed_subs),
        admin_sub=args.admin_sub.strip(),
        secure_cookie="; Secure" if args.origin.startswith("https:") else "",
    )
    if not _auth["allowed_subs"]:
        print("ALLOWED_SUBS is empty, so nobody can use the list yet. Sign in to see your 6j0.org ID, then add it.")

    global _catalog, _feedback
    init_db(args.db)
    _feedback = Feedback(args.feedback or str(Path(args.db).resolve().parent / "feedback.jsonl"))
    _catalog = recipes.Catalog(_db, _db_lock, USER_AGENT)
    _catalog.ensure_fresh(background=True)  # download recipes without delaying startup
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    scheme = "http"
    if args.cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    print(f"Shopping list running at {scheme}://{args.host}:{args.port}/  (sign-in: {origin} as \"{args.id_app}\")")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
