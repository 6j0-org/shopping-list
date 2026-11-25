"""The server end to end, in-process, against a fake identity server: sign-in, sessions, a list per person, sharing a
list by invite, upgrading from one shared list, feedback, identity deletion, cross-site refusal, and sign-out."""

import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import idtoken  # noqa: E402
import recipes  # noqa: E402
import server  # noqa: E402
from feedback import Feedback  # noqa: E402
from tests.fakeid import FakeIdServer  # noqa: E402

APP = "shopping-list"
NONCE = "n" * 32
DEFAULTS = {"search": False, "quantity": False, "aisle": False, "barcodeTyping": False, "hotDogs": True,
       "metHotDogMan": False}


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.id = FakeIdServer()
        server._auth.update(
            id_origin=cls.id.origin, id_app=APP, jwks_url=cls.id.jwks_url,
            admin_sub="owner-sub", secure_cookie="",
        )
        server.init_db(os.path.join(cls.tmp.name, "shopping.db"))
        cls.feedback_file = Path(cls.tmp.name, "feedback.jsonl")
        server._feedback = Feedback(cls.feedback_file)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.id.close()
        cls.tmp.cleanup()

    def setUp(self):
        idtoken._keys = {}
        idtoken._fetched_at = 0.0
        with server._db_lock:  # the recipe book is shared by everyone, so each test starts with an empty one
            server._db.execute("DELETE FROM recipe_book")
            server._db.execute("DELETE FROM recipe_uses")
            server._db.commit()

    def request(self, method, path, body=None, sid=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if sid:
            h["Cookie"] = f"sid={sid}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode(errors="replace")
        return res.status, payload, res

    def sign_in(self, sub):
        token = self.id.sign_in_token(sub, APP, NONCE)
        status, body, res = self.request("POST", "/api/id/login", {"token": token, "nonce": NONCE})
        self.assertEqual(status, 200, body)
        cookie = res.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        return cookie.split(";")[0].split("=", 1)[1], body

    def test_page_loads_without_a_session(self):
        status, body, res = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('type="module" src="app.js"', body)
        for f in ("/feedback.js", "/signin.js", "/app.js", "/hotdog.js"):
            self.assertEqual(self.request("GET", f)[0], 200, f)
        self.assertEqual(self.request("GET", "/api/config")[1], {"idOrigin": self.id.origin, "idApp": APP})

    def test_api_needs_a_session(self):
        for method, path in [("GET", "/api/items"), ("GET", "/api/me"), ("GET", "/api/feedback"),
                             ("POST", "/api/items"), ("DELETE", "/api/items/1")]:
            with self.subTest(path=path):
                status, body, _ = self.request(method, path, {} if method == "POST" else None)
                self.assertEqual(status, 401)
        self.assertEqual(self.request("GET", "/api/items", sid="not-a-session")[0], 401)

    def names(self, sid, pantry=False):
        items = self.request("GET", "/api/items", sid=sid)[1]
        return sorted(i["name"] for i in items if (i["section"] == "Pantry") == pantry)

    def add(self, sid, name, **extra):
        status, item, _ = self.request("POST", "/api/items", {"name": name, **extra}, sid=sid)
        self.assertEqual(status, 201, item)
        return item

    def test_scanning_a_barcode_again_raises_its_quantity(self):
        sid, _ = self.sign_in("scan-sub")
        scan = {"barcode": "0123456789012", "section": "Pantry"}
        first = self.add(sid, "Beans", quantity="400 g", **scan)
        for want in ("2 × 400 g", "3 × 400 g"):
            status, item, _ = self.request("POST", "/api/items", {"name": "Beans", "quantity": "400 g", **scan}, sid=sid)
            self.assertEqual((status, item["id"], item["quantity"]), (200, first["id"], want))
        self.assertEqual(self.names(sid, pantry=True), ["Beans"])
        self.add(sid, "Beans", **scan | {"section": "auto"})  # the shopping list isn't merged
        self.add(sid, "Rice", barcode="999999", section="Pantry")
        status, rice, _ = self.request("POST", "/api/items", {"name": "Rice", "barcode": "999999", "section": "Pantry"}, sid=sid)
        self.assertEqual((status, rice["quantity"]), (200, "2"))

    def test_pantry_lists_newest_first(self):
        sid, _ = self.sign_in("newest-sub")
        for name in ("Apples", "Zucchini", "Milk"):
            self.add(sid, name, section="Pantry")
        self.add(sid, "Bread")
        status, items, _ = self.request("GET", "/api/items", sid=sid)
        self.assertEqual(status, 200)
        pantry = [i["name"] for i in items if i["section"] == "Pantry"]
        self.assertEqual(pantry, ["Milk", "Zucchini", "Apples"])

    def invite(self, sid):
        status, body, _ = self.request("POST", "/api/list/invites", {}, sid=sid)
        self.assertEqual((status, body["days"]), (201, 7))
        return body["code"]

    def test_everyone_starts_with_their_own_empty_list(self):
        alice, body = self.sign_in("alice-sub")
        self.assertEqual(body, {"ok": True})
        self.assertEqual(self.request("GET", "/api/me", sid=alice)[1], {"isAdmin": False, "settings": DEFAULTS})
        self.assertEqual(self.names(alice), [])
        self.assertEqual(self.request("GET", "/api/sections", sid=alice)[0], 200)
        item = self.add(alice, "Bananas", quantity="6")
        self.assertEqual(self.request("PATCH", f"/api/items/{item['id']}", {"checked": True}, sid=alice)[1]["checked"], True)

        bob, _ = self.sign_in("bob-sub")
        self.assertEqual(self.names(bob), [], "a new identity gets a new list, not someone else's")
        for method, body in [("PUT", {"name": "Mine now"}), ("DELETE", None)]:
            with self.subTest(method=method):
                self.assertEqual(self.request(method, f"/api/items/{item['id']}", body, sid=bob)[0], 404)
        self.assertEqual(self.request("POST", "/api/items/clear-checked", {}, sid=bob)[1], {"deleted": 0})
        self.assertEqual(self.names(alice), ["Bananas"])
        members = self.request("GET", "/api/list", sid=bob)[1]["members"]
        self.assertEqual(len(members), 1)
        self.assertTrue(members[0]["you"])
        self.assertNotIn("bob-sub", json.dumps(members), "subs are never shown")

        alice_again, _ = self.sign_in("alice-sub")
        self.assertEqual(self.names(alice_again), ["Bananas"], "signing in again keeps your list")

    def add_recipe(self, sid, title="Toast", ingredients=("Bread", "Butter"), **extra):
        body = {"title": title, "ingredients": [{"quantity": "1", "name": n, "note": ""} for n in ingredients],
                "steps": ["Toast it."], **extra}
        status, recipe, _ = self.request("POST", "/api/recipes", body, sid=sid)
        self.assertEqual(status, 201, recipe)
        return recipe

    def test_name_suggestions_are_gone(self):
        sid, _ = self.sign_in("cache-sub")
        self.assertEqual(self.request("GET", "/api/ingredients", sid=sid)[0], 404)

    def test_recipe_book(self):
        sid, _ = self.sign_in("book-sub")
        empty = self.request("GET", "/api/recipes", sid=sid)[1]
        self.assertEqual((empty["recipes"], empty["count"]), ([], 0))
        toast = self.add_recipe(sid, source="https://example.com/toast")
        self.add_recipe(sid, title="Garlic rice", ingredients=["Garlic", "Rice", "garlic cloves", "Salt"])
        self.add(sid, "Sourdough Bread", section="Pantry")
        self.add(sid, "Kirkland Jasmine Rice", section="Pantry")

        book = self.request("GET", "/api/recipes", sid=sid)[1]
        self.assertEqual([(r["title"], r["have"], r["missing"]) for r in book["recipes"]],
                         [("Garlic rice", 1, 1), ("Toast", 1, 1)], "garlic counts once and salt is a staple")
        rice = book["recipes"][0]
        self.assertEqual([(i["name"], i["have"], i["staple"]) for i in rice["ingredients"]],
                         [("Garlic", False, False), ("Rice", True, False), ("garlic cloves", False, False),
                          ("Salt", False, True)])
        self.assertEqual(rice["ingredients"][1]["pantry_item"], "Kirkland Jasmine Rice")
        self.assertEqual(len(self.request("GET", "/api/recipes?max_missing=0", sid=sid)[1]["recipes"]), 0)

        # Editing, and what's refused
        status, changed, _ = self.request("PUT", f"/api/recipes/{toast['id']}",
                                          {**toast, "title": "Buttered toast", "steps": ["Toast.", " ", "Butter."]}, sid=sid)
        self.assertEqual((status, changed["title"], changed["steps"]), (200, "Buttered toast", ["Toast.", "Butter."]))
        self.assertEqual(self.request("GET", f"/api/recipes/{toast['id']}", sid=sid)[1]["title"], "Buttered toast")
        for bad in ({"title": ""}, {"source": "javascript:alert(1)"}, {"ingredients": "eggs"},
                    {"ingredients": [{"quantity": "2", "name": ""}]}, {"steps": [3]}):
            with self.subTest(bad=bad):
                self.assertEqual(self.request("POST", "/api/recipes", {**toast, **bad}, sid=sid)[0], 400)

        self.assertEqual((changed["origin"], changed["mine"], changed["canEdit"]), ("hand", True, True))
        self.assertEqual(self.request("POST", "/api/recipes", {**toast, "origin": "magic"}, sid=sid)[0], 400)

        # Everyone sees it, checked against their own pantry, but only its author (and the admin) can change it
        other, _ = self.sign_in("not-book-sub")
        theirs = self.request("GET", "/api/recipes", sid=other)[1]
        self.assertEqual((theirs["count"], theirs["pantry_size"]), (2, 0))
        seen = self.request("GET", f"/api/recipes/{toast['id']}", sid=other)[1]
        self.assertEqual((seen["title"], seen["mine"], seen["canEdit"]), ("Buttered toast", False, False))
        self.assertNotIn("author", seen)
        for method, body in [("PUT", toast), ("DELETE", None)]:
            with self.subTest(method=method):
                self.assertEqual(self.request(method, f"/api/recipes/{toast['id']}", body, sid=other)[0], 403)
        self.assertEqual(self.request("DELETE", "/api/recipes/999999", sid=other)[0], 404)
        admin, _ = self.sign_in("owner-sub")
        self.assertTrue(self.request("GET", f"/api/recipes/{toast['id']}", sid=admin)[1]["canEdit"])
        self.assertEqual(self.request("PUT", f"/api/recipes/{toast['id']}", {**toast, "title": "Toast"}, sid=admin)[0], 200)

        self.assertEqual(self.request("DELETE", f"/api/recipes/{toast['id']}", sid=sid)[0], 204)
        self.assertEqual(self.request("GET", "/api/recipes", sid=other)[1]["count"], 1)

        # Leaving a list, or the list going away, doesn't take recipes with it
        self.request("POST", "/api/list/join", {"code": self.invite(other)}, sid=sid)
        self.assertEqual(self.request("GET", "/api/recipes", sid=other)[1]["count"], 1)

    def test_recipes_outlive_their_author(self):
        sid, _ = self.sign_in("author-sub")
        recipe = self.add_recipe(sid, title="Soup")
        self.request("POST", "/api/id/deleted", {"token": self.id.deletion_token("author-sub", APP)})
        other, _ = self.sign_in("reader-sub")
        kept = self.request("GET", f"/api/recipes/{recipe['id']}", sid=other)[1]
        self.assertEqual((kept["title"], kept["canEdit"]), ("Soup", False))
        with server._db_lock:
            author = server._db.execute("SELECT author FROM recipe_book WHERE id = ?", (recipe["id"],)).fetchone()[0]
        self.assertIsNone(author)

    def test_generated_recipes_still_save(self):
        """Recipes saved from the removed recipe generator keep their origin, and the endpoint is gone."""
        sid, _ = self.sign_in("meals-sub")
        self.assertEqual(self.add_recipe(sid, title="Chicken and rice", origin="generated")["origin"], "generated")
        self.assertNotIn("canGenerate", self.request("GET", "/api/recipes", sid=sid)[1])
        self.assertEqual(self.request("POST", "/api/recipes/generate", {"ingredients": ["rice"]}, sid=sid)[0], 404)

    def test_importing_a_recipe(self):
        sid, _ = self.sign_in("import-sub")
        read = {"is_recipe": True, "title": "Pancakes", "servings": "4",
                "ingredients": [{"quantity": "1 cup", "name": "all-purpose flour", "note": "sifted"},
                                {"quantity": "", "name": " ", "note": ""}],
                "steps": ["Mix.", "Cook."]}
        page = """<html><head><title>Best Pancakes | Site</title><script type="application/ld+json">
            {"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, {"@type": ["Recipe"], "name": "Pancakes",
             "image": "https://example.com/p.jpg", "recipeIngredient": ["1 cup flour, sifted"]}]}</script></head></html>"""
        sent = []
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), \
                mock.patch.object(recipes, "fetch_page", return_value=page) as fetch, \
                mock.patch.object(recipes, "ask_claude", side_effect=lambda c: sent.append(c) or read):
            status, recipe, _ = self.request("POST", "/api/recipes/import", {"url": "https://example.com/pancakes"}, sid=sid)
            self.assertEqual(status, 201, recipe)
            fetch.assert_called_once_with("https://example.com/pancakes")
            self.assertEqual((recipe["title"], recipe["source"], recipe["origin"], recipe["steps"]),
                             ("Pancakes", "https://example.com/pancakes", "link", ["Mix.", "Cook."]))
            self.assertEqual(recipe["ingredients"], [{"quantity": "1 cup", "name": "all-purpose flour", "note": "sifted"}])
            self.assertIn("recipeIngredient", sent[0])
            self.assertNotIn("p.jpg", sent[0], "photos aren't sent")

            status, recipe, _ = self.request("POST", "/api/recipes/import", {"text": "Pancakes: flour, milk."}, sid=sid)
            self.assertEqual((status, recipe["source"]), (201, ""))
            self.assertTrue(sent[1].endswith("Pancakes: flour, milk."))

            read["is_recipe"] = False
            self.assertEqual(self.request("POST", "/api/recipes/import", {"text": "hello"}, sid=sid)[0], 422)
            for body in ({}, {"url": "file:///etc/passwd"}, {"url": "not a link"}):
                with self.subTest(body=body):
                    self.assertEqual(self.request("POST", "/api/recipes/import", body, sid=sid)[0], 400)
            self.assertEqual(self.request("GET", "/api/recipes", sid=sid)[1]["count"], 2)

            with mock.patch.dict(server.RECIPE_LIMITS, {"import": (3, 200)}):
                status, body, _ = self.request("POST", "/api/recipes/import", {"text": "x"}, sid=sid)
                self.assertEqual(status, 429, body)

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertFalse(self.request("GET", "/api/recipes", sid=sid)[1]["canImport"])
            self.assertEqual(self.request("POST", "/api/recipes/import", {"text": "x"}, sid=sid)[0], 503)

    def test_import_refuses_private_addresses(self):
        # This test server is on 127.0.0.1.
        with self.assertRaises(recipes.RecipeError) as caught:
            recipes.fetch_page(f"http://127.0.0.1:{self.port}/")
        self.assertIn("private address", caught.exception.message)
        with self.assertRaises(recipes.RecipeError):
            recipes.fetch_page(f"http://localhost:{self.port}/")

    def test_sharing_a_list_by_invite(self):
        host, _ = self.sign_in("host-sub")
        self.add(host, "Milk")
        guest, _ = self.sign_in("guest-sub")
        self.add(guest, "Eggs")
        self.add(guest, "Rice", section="Pantry")

        code = self.invite(host)
        status, body, _ = self.request("POST", "/api/list/join", {"code": code}, sid=guest)
        self.assertEqual((status, body["moved"], len(body["members"])), (200, 2, 2))
        self.assertEqual(self.names(host), ["Eggs", "Milk"], "a guest alone on their list brings its items")
        self.assertEqual(self.names(host, pantry=True), ["Rice"])
        self.assertEqual(self.names(guest), ["Eggs", "Milk"])
        egg = next(i for i in self.request("GET", "/api/items", sid=host)[1] if i["name"] == "Eggs")
        self.assertEqual(self.request("DELETE", f"/api/items/{egg['id']}", sid=host)[0], 204)

        self.assertEqual(self.request("POST", "/api/list/join", {"code": code}, sid=guest)[0], 404, "single use")
        self.assertEqual(self.request("POST", "/api/list/join", {"code": self.invite(guest)}, sid=host)[0], 400,
                         "already on it")
        self.assertEqual(self.request("POST", "/api/list/join", {}, sid=guest)[0], 400)

        # A third person joins and then leaves. Leaving someone else's shared list takes nothing with you.
        third, _ = self.sign_in("third-sub")
        self.request("POST", "/api/list/join", {"code": self.invite(host)}, sid=third)
        members = self.request("GET", "/api/list", sid=third)[1]["members"]
        self.assertEqual(len(members), 3)
        me = next(m for m in members if m["you"])
        status, body, _ = self.request("DELETE", f"/api/list/members/{me['id']}", sid=third)
        self.assertEqual((status, len(body["members"])), (200, 1))
        self.assertEqual(self.names(third), [])
        self.assertEqual(self.names(host), ["Milk"])

        # Taking someone off: they start over, and the list stays with the rest.
        guest_id = next(m["id"] for m in self.request("GET", "/api/list", sid=host)[1]["members"] if not m["you"])
        self.assertEqual(self.request("DELETE", f"/api/list/members/{guest_id}", sid=third)[0], 404, "not on your list")
        status, body, _ = self.request("DELETE", f"/api/list/members/{guest_id}", sid=host)
        self.assertEqual((status, len(body["members"])), (200, 1))
        self.assertEqual(self.names(guest), [])
        self.assertEqual(self.names(host), ["Milk"])
        host_id = body["members"][0]["id"]
        self.assertEqual(self.request("DELETE", f"/api/list/members/{host_id}", sid=host)[0], 400, "the only one")

    def test_expired_invites_are_refused(self):
        host, _ = self.sign_in("expiry-host")
        guest, _ = self.sign_in("expiry-guest")
        code = self.invite(host)
        with server._db_lock:
            server._db.execute("UPDATE invites SET created = created - ?", (server.INVITE_MS,))
            server._db.commit()
        status, body, _ = self.request("POST", "/api/list/join", {"code": code}, sid=guest)
        self.assertEqual(status, 404)
        self.assertIn("expired", body["error"])

    def test_cross_site_joins_are_refused(self):
        host, _ = self.sign_in("xs-host")
        guest, _ = self.sign_in("xs-guest")
        status, _, _ = self.request("POST", "/api/list/join", {"code": self.invite(host)}, sid=guest,
                                    headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_feedback_routes(self):
        tester, _ = self.sign_in("tester-sub")
        owner, _ = self.sign_in("owner-sub")
        status, post, _ = self.request("POST", "/api/feedback", {"body": "  Add a dark mode.  "}, sid=tester)
        self.assertEqual((status, post["body"], post["admin"]), (201, "Add a dark mode.", False))
        pid = post["id"]
        self.assertEqual(self.request("POST", "/api/feedback", {"body": "   "}, sid=tester)[0], 400)
        self.assertEqual(self.request("POST", "/api/feedback", {"body": "x" * 2049}, sid=tester)[0], 400)

        listed = self.request("GET", "/api/feedback", sid=owner)[1]
        self.assertTrue(listed["isAdmin"])
        self.assertIn(pid, [i["id"] for i in listed["items"]])
        self.assertNotIn("tester-sub", json.dumps(listed), "subs are never shown")

        status, thread, _ = self.request("POST", f"/api/feedback/{pid}/replies", {"body": "It already follows the system."}, sid=owner)
        self.assertEqual(status, 201)
        reply = thread["messages"][0]
        self.assertTrue(reply["admin"], "ADMIN_SUB's replies are labeled")
        self.assertEqual(self.request("PUT", f"/api/feedback/{pid}", {"body": "Add a darker dark mode."}, sid=tester)[1]["body"],
                         "Add a darker dark mode.")
        self.assertEqual(self.request("PUT", f"/api/feedback/{pid}/replies/{reply['id']}", {"body": "hijack"}, sid=tester)[0], 403)
        self.assertEqual(self.request("PUT", f"/api/feedback/{pid}/replies/{reply['id']}", {"body": "It follows your system setting."}, sid=owner)[0], 200)
        self.assertEqual(self.request("POST", f"/api/feedback/{pid}/status", {"status": "done"}, sid=tester)[0], 403)
        self.assertEqual(self.request("POST", f"/api/feedback/{pid}/status", {"status": "done"}, sid=owner)[1]["status"], "done")
        self.assertEqual(self.request("GET", f"/api/feedback/{pid}", sid=tester)[1]["status"], "done")
        self.assertEqual(self.request("DELETE", f"/api/feedback/{pid}", sid=tester)[0], 204)
        self.assertEqual(self.request("GET", f"/api/feedback/{pid}", sid=tester)[0], 404)
        self.assertEqual(self.request("GET", "/api/feedback/nope/what", sid=tester)[0], 404)

    def test_bad_sign_ins(self):
        def login(body):
            return self.request("POST", "/api/id/login", body)

        self.assertEqual(login({"token": self.id.sign_in_token("s", APP, NONCE), "nonce": "m" * 32})[0], 401)
        self.assertEqual(login({"token": self.id.sign_in_token("s", "glaze-journal", NONCE), "nonce": NONCE})[0], 401)
        self.assertEqual(login({"token": self.id.deletion_token("s", APP), "nonce": NONCE})[0], 401)
        self.assertEqual(login({"token": "garbage", "nonce": NONCE})[0], 401)
        self.assertEqual(login({})[0], 401)
        token = self.id.sign_in_token("replay-sub", APP, NONCE)
        self.assertEqual(login({"token": token, "nonce": NONCE})[0], 200)
        status, body, res = login({"token": token, "nonce": NONCE})
        self.assertEqual((status, res.getheader("Set-Cookie")), (401, None), "a token works once")
        self.assertEqual(self.request("POST", "/api/id/login", raw=b"{not json")[0], 400)
        self.assertEqual(self.request("POST", "/api/id/login", raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.request("POST", "/api/id/login", raw=b"\xff\xfe")[0], 400)

    def test_cross_site_changes_are_refused(self):
        owner, _ = self.sign_in("owner-sub")
        status, body, _ = self.request("POST", "/api/items", {"name": "Evil"}, sid=owner, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/items", {"name": "Fine"}, sid=owner, headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 201)
        token = self.id.sign_in_token("owner-sub", APP, NONCE)
        self.assertEqual(self.request("POST", "/api/id/login", {"token": token, "nonce": NONCE},
                                      headers={"Sec-Fetch-Site": "same-site"})[0], 403)

    def test_identity_deletion(self):
        gone, _ = self.sign_in("deleted-sub")
        status, post, _ = self.request("POST", "/api/feedback", {"body": "Delete me later"}, sid=gone)
        self.assertEqual(status, 201)
        self.add(gone, "Only mine")
        with server._db_lock:
            list_id = server._db.execute("SELECT list_id FROM users WHERE sub = 'deleted-sub'").fetchone()[0]
        # A sign-in token can't stand in for a deletion notice, and a bad one is refused.
        self.assertEqual(self.request("POST", "/api/id/deleted", {"token": self.id.sign_in_token("deleted-sub", APP, NONCE)})[0], 401)
        self.assertEqual(self.request("POST", "/api/id/deleted", {"token": "nope"})[0], 401)
        self.assertEqual(self.request("GET", "/api/me", sid=gone)[0], 200, "still signed in")

        status, body, _ = self.request("POST", "/api/id/deleted", {"token": self.id.deletion_token("deleted-sub", APP)})
        self.assertEqual((status, body), (200, {"ok": True}))
        self.assertEqual(self.request("GET", "/api/me", sid=gone)[0], 401, "its sessions end")
        owner, _ = self.sign_in("owner-sub")
        self.assertEqual(self.request("GET", f"/api/feedback/{post['id']}", sid=owner)[0], 404, "its feedback is erased")
        self.assertIn('"erase"', self.feedback_file.read_text())
        with server._db_lock:
            left = server._db.execute("SELECT COUNT(*) FROM items WHERE list_id = ?", (list_id,)).fetchone()[0]
        self.assertEqual(left, 0, "a list nobody else is on goes too")
        # Unknown identities are fine too, so the identity server stops retrying.
        self.assertEqual(self.request("POST", "/api/id/deleted", {"token": self.id.deletion_token("never-here", APP)})[0], 200)

    def test_sign_out(self):
        sid, _ = self.sign_in("owner-sub")
        status, _, res = self.request("POST", "/api/logout", {}, sid=sid)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", res.getheader("Set-Cookie"))
        self.assertEqual(self.request("GET", "/api/me", sid=sid)[0], 401)

    def test_settings(self):
        sid, _ = self.sign_in("settings-sub")
        self.assertEqual(self.request("PUT", "/api/settings", {"search": True})[0], 401)
        status, body, _ = self.request("PUT", "/api/settings", {"search": True, "quantity": True}, sid=sid)
        self.assertEqual((status, body), (200, {**DEFAULTS, "search": True, "quantity": True}))
        status, body, _ = self.request("PATCH", "/api/settings", {"quantity": False}, sid=sid)
        self.assertEqual(body, {**DEFAULTS, "search": True}, "a change leaves the other settings alone")
        again, _ = self.sign_in("settings-sub")
        self.assertEqual(self.request("GET", "/api/me", sid=again)[1]["settings"], {**DEFAULTS, "search": True},
                         "they follow the account to another session")
        # Hot Dog Man starts on, and turning him off sticks.
        status, body, _ = self.request("PUT", "/api/settings", {"metHotDogMan": True, "hotDogs": False}, sid=sid)
        self.assertEqual(body, {**DEFAULTS, "search": True, "metHotDogMan": True, "hotDogs": False})
        self.request("PUT", "/api/settings", {"metHotDogMan": False, "hotDogs": True}, sid=sid)
        for bad in ({"colour": True}, {"search": "yes"}, {"search": 1}, {"hotDogs": None}):
            with self.subTest(body=bad):
                self.assertEqual(self.request("PUT", "/api/settings", bad, sid=sid)[0], 400)

        # Per person, even on a shared list.
        host, _ = self.sign_in("settings-host")
        self.request("POST", "/api/list/join", {"code": self.invite(host)}, sid=sid)
        self.assertEqual(self.request("GET", "/api/me", sid=host)[1]["settings"], DEFAULTS)
        self.assertEqual(self.request("GET", "/api/me", sid=sid)[1]["settings"], {**DEFAULTS, "search": True})

    def shortcut(self, token, body, **headers):
        auth = {"Authorization": f"Bearer {token}"} if token else {}
        return self.request("POST", "/api/shortcut/items", body, headers={**auth, **headers})

    def test_api_tokens_for_siri(self):
        sid, _ = self.sign_in("siri-sub")
        self.assertEqual(self.request("GET", "/api/token", sid=sid)[1], {"token": None})
        self.assertEqual(self.request("POST", "/api/token", {})[0], 401, "making a token needs a session")
        status, made, _ = self.request("POST", "/api/token", {}, sid=sid)
        self.assertEqual((status, made["lastUsed"]), (201, 0))
        token = made["token"]
        self.assertTrue(token.startswith("sl_"))
        info = self.request("GET", "/api/token", sid=sid)[1]
        self.assertEqual((info["token"], info["created"], info["lastUsed"]), (None, made["created"], 0),
                         "the token is only shown when it's made")
        with server._db_lock:
            stored = [r[0] for r in server._db.execute("SELECT token_hash FROM api_tokens")]
        self.assertNotIn(token, stored, "stored hashed")

        # Adding: dictation's trailing period goes, and a name takes the spelling the list already uses.
        self.add_recipe(sid, ingredients=["Peanut Butter"])
        status, body, _ = self.shortcut(token, {"name": "peanut butter."}, **{"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["message"], "Added Peanut Butter to the shopping list.")
        self.assertEqual((body["item"]["name"], body["item"]["section"] != "Pantry"), ("Peanut Butter", True))
        self.assertEqual(self.shortcut(token, {"name": "Paper towels", "quantity": "2"})[1]["item"]["quantity"], "2")
        self.assertEqual(self.names(sid), ["Paper towels", "Peanut Butter"])
        self.assertGreater(self.request("GET", "/api/token", sid=sid)[1]["lastUsed"], 0)

        status, body, _ = self.shortcut(token, {"name": " . "})
        self.assertEqual((status, body["message"]), (400, "name is required"), "errors carry a message to speak")
        self.assertEqual(self.request("GET", "/api/shortcut/items", headers={"Authorization": f"Bearer {token}"})[0], 405)

        # A token is good for adding items and nothing else; a session isn't good for the shortcut route.
        for method, path in [("GET", "/api/items"), ("GET", "/api/token"), ("POST", "/api/token")]:
            with self.subTest(path=path):
                res = self.request(method, path, {} if method == "POST" else None,
                                   headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(res[0], 401)
        self.assertEqual(self.request("POST", "/api/shortcut/items", {"name": "x"}, sid=sid)[0], 401)
        for bad in (None, "nope", "sl_nope", token + "x"):
            with self.subTest(token=bad):
                status, body, _ = self.shortcut(bad, {"name": "Eggs"})
                self.assertEqual(status, 401)
                self.assertIn("token", body["message"])
        self.assertEqual(self.request("POST", "/api/shortcut/items", {"name": "x"},
                                      headers={"Authorization": f"Basic {token}"})[0], 401)

        # The token follows its owner to a shared list.
        host, _ = self.sign_in("siri-host")
        self.request("POST", "/api/list/join", {"code": self.invite(host)}, sid=sid)
        self.shortcut(token, {"name": "Milk"})
        self.assertIn("Milk", self.names(host))

        # A new token replaces the old one; turning it off stops it.
        newer = self.request("POST", "/api/token", {}, sid=sid)[1]["token"]
        self.assertEqual(self.shortcut(token, {"name": "Eggs"})[0], 401, "the old token stops working")
        self.assertEqual(self.shortcut(newer, {"name": "Eggs"})[0], 201)
        self.assertEqual(self.request("DELETE", "/api/token", sid=sid)[0], 204)
        self.assertEqual(self.shortcut(newer, {"name": "Eggs"})[0], 401)
        self.assertEqual(self.request("GET", "/api/token", sid=sid)[1], {"token": None})

    def test_identity_deletion_ends_api_tokens(self):
        sid, _ = self.sign_in("siri-gone")
        token = self.request("POST", "/api/token", {}, sid=sid)[1]["token"]
        self.request("POST", "/api/id/deleted", {"token": self.id.deletion_token("siri-gone", APP)})
        self.assertEqual(self.shortcut(token, {"name": "Eggs"})[0], 401)

    def test_sessions_are_stored_hashed(self):
        sid, _ = self.sign_in("owner-sub")
        with server._db_lock:
            hashes = [r[0] for r in server._db.execute("SELECT token_hash FROM sessions")]
        self.assertNotIn(sid, hashes)
        self.assertIn(server._token_hash(sid), hashes)


class UpgradeTest(unittest.TestCase):
    """A database from when there was one list, shared by ALLOWED_SUBS."""

    def test_unowned_items_go_to_allowed_subs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    quantity TEXT NOT NULL DEFAULT '', section TEXT NOT NULL, section_auto INTEGER NOT NULL DEFAULT 1,
                    checked INTEGER NOT NULL DEFAULT 0, barcode TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));
                CREATE TABLE users (id INTEGER PRIMARY KEY, sub TEXT NOT NULL UNIQUE, guid TEXT NOT NULL UNIQUE,
                    created INTEGER NOT NULL);
                INSERT INTO items (name, section) VALUES ('Coffee', 'Pantry'), ('Bread', 'Bakery');
                INSERT INTO users (sub, guid, created) VALUES ('signed-in-before', 'g1', 1), ('outsider', 'g2', 1);
            """)
            old.commit()
            old.close()
            saved = server._db
            try:
                server.init_db(path)
                self.assertIn("belong to no list", server.claim_unowned(frozenset()), "nothing moves without ALLOWED_SUBS")
                self.assertIn("2 unowned items and 2 identities",
                              server.claim_unowned(frozenset({"signed-in-before", "not-yet"})))
                self.assertIsNone(server.claim_unowned(frozenset({"signed-in-before", "not-yet"})), "runs once")
                rows = dict(server._db.execute("SELECT sub, list_id FROM users").fetchall())
                self.assertEqual(rows["signed-in-before"], rows["not-yet"])
                self.assertIsNone(rows["outsider"])
                listed = server.list_items(rows["not-yet"])
                self.assertEqual(sorted(i["name"] for i in listed), ["Bread", "Coffee"])
                # Someone who isn't in ALLOWED_SUBS gets a new list of their own.
                server.account_for("outsider")
                outsider = server._db.execute("SELECT list_id FROM users WHERE sub = 'outsider'").fetchone()[0]
                self.assertNotEqual(outsider, rows["not-yet"])
                self.assertEqual(server.list_items(outsider), [])
            finally:
                server._db.close()
                server._db = saved


class StartupTest(unittest.TestCase):
    def test_id_origin_is_required(self):
        env = {k: v for k, v in os.environ.items() if k != "ID_ORIGIN"}
        res = subprocess.run([sys.executable, str(ROOT / "server.py"), "--db", ":memory:"],
                             capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(res.returncode, 2)
        self.assertIn("ID_ORIGIN is required", res.stderr)


if __name__ == "__main__":
    unittest.main()
