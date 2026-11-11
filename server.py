#!/usr/bin/env python3
"""Shopping list web app. Python standard library only.

    python3 server.py                     # http://localhost:8000
    python3 server.py --host 0.0.0.0 --cert cert.pem --key key.pem   # HTTPS (needed for phone cameras)
"""

import argparse
import json
import os
import re
import sqlite3
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import recipes
import sections

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
USER_AGENT = "ShoppingList/0.1 (self-hosted grocery list)"

_db_lock = threading.Lock()
_db = None
_catalog = None


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
        """
    )
    _db.commit()


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


class BadRequest(Exception):
    pass


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

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 10_000:
            raise BadRequest("request too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise BadRequest("invalid JSON")
        if not isinstance(data, dict):
            raise BadRequest("expected a JSON object")
        return data

    def _path(self):
        return urllib.parse.urlparse(self.path).path

    def _dispatch(self, handler):
        try:
            handler()
        except BadRequest as e:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})

    def do_GET(self):
        path = self._path()
        if path == "/api/items":
            return self._send_json(HTTPStatus.OK, list_items())
        if path == "/api/sections":
            return self._send_json(
                HTTPStatus.OK, {"sections": sections.SECTIONS, "pantry": sections.PANTRY}
            )
        if path == "/api/recipes":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            raw = q.get("max_missing", [""])[0]
            max_missing = int(raw) if raw.isdigit() else None
            return self._send_json(HTTPStatus.OK, recipe_matches(max_missing))
        if path.startswith("/api/classify"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send_json(HTTPStatus.OK, {"section": sections.classify(q.get("name", [""])[0])})
        m = BARCODE_PATH.match(path)
        if m:
            return self._send_json(HTTPStatus.OK, lookup_barcode(m.group(1)))
        if path.startswith("/api/"):
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        return super().do_GET()

    def do_POST(self):
        def run():
            path = self._path()
            if path == "/api/items":
                return self._send_json(HTTPStatus.CREATED, create_item(self._read_json()))
            if path == "/api/items/clear-checked":
                return self._send_json(HTTPStatus.OK, {"deleted": clear_checked()})
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        self._dispatch(run)

    def do_PUT(self):
        def run():
            m = ITEM_PATH.match(self._path())
            if not m:
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            item = update_item(int(m.group(1)), self._read_json())
            if not item:
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "no such item"})
            self._send_json(HTTPStatus.OK, item)

        self._dispatch(run)

    do_PATCH = do_PUT

    def do_DELETE(self):
        m = ITEM_PATH.match(self._path())
        if not m:
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        if not delete_item(int(m.group(1))):
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "no such item"})
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Every option can also be set with an environment variable (for containers).
    env = os.environ.get
    ap.add_argument("--host", default=env("SHOPPING_HOST", "127.0.0.1"), help="bind address (0.0.0.0 for your LAN) [SHOPPING_HOST]")
    ap.add_argument("--port", type=int, default=int(env("SHOPPING_PORT", "8000")), help="[SHOPPING_PORT]")
    ap.add_argument("--db", default=env("SHOPPING_DB", str(ROOT / "shopping.db")), help="SQLite file [SHOPPING_DB]")
    ap.add_argument("--cert", default=env("SHOPPING_CERT"), help="TLS certificate (PEM) to serve HTTPS [SHOPPING_CERT]")
    ap.add_argument("--key", default=env("SHOPPING_KEY"), help="TLS private key (PEM) [SHOPPING_KEY]")
    args = ap.parse_args()

    global _catalog
    init_db(args.db)
    _catalog = recipes.Catalog(_db, _db_lock, USER_AGENT)
    _catalog.ensure_fresh(background=True)  # download recipes without delaying startup
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    scheme = "http"
    if args.cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    print(f"Shopping list running at {scheme}://{args.host}:{args.port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
