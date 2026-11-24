"""The server end to end, in-process, against a fake identity server: sign-in, sessions, the ALLOWED_SUBS allowlist,
feedback for anyone signed in, identity deletion, cross-site refusal, and sign-out."""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import idtoken  # noqa: E402
import server  # noqa: E402
from feedback import Feedback  # noqa: E402
from tests.fakeid import FakeIdServer  # noqa: E402

APP = "shopping-list"
NONCE = "n" * 32


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.id = FakeIdServer()
        server._auth.update(
            id_origin=cls.id.origin, id_app=APP, jwks_url=cls.id.jwks_url,
            allowed_subs=frozenset({"owner-sub", "partner-sub"}), admin_sub="owner-sub", secure_cookie="",
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
        for f in ("/feedback.js", "/signin.js", "/app.js"):
            self.assertEqual(self.request("GET", f)[0], 200, f)
        self.assertEqual(self.request("GET", "/api/config")[1], {"idOrigin": self.id.origin, "idApp": APP})

    def test_api_needs_a_session(self):
        for method, path in [("GET", "/api/items"), ("GET", "/api/me"), ("GET", "/api/feedback"),
                             ("POST", "/api/items"), ("DELETE", "/api/items/1")]:
            with self.subTest(path=path):
                status, body, _ = self.request(method, path, {} if method == "POST" else None)
                self.assertEqual(status, 401)
        self.assertEqual(self.request("GET", "/api/items", sid="not-a-session")[0], 401)

    def test_allowed_identity_uses_the_shared_list(self):
        owner, body = self.sign_in("owner-sub")
        self.assertEqual(body, {"allowed": True})
        self.assertEqual(self.request("GET", "/api/me", sid=owner)[1], {"allowed": True, "id": "owner-sub", "isAdmin": True})
        status, item, _ = self.request("POST", "/api/items", {"name": "Bananas", "quantity": "6"}, sid=owner)
        self.assertEqual(status, 201)
        partner, _ = self.sign_in("partner-sub")
        names = [i["name"] for i in self.request("GET", "/api/items", sid=partner)[1]]
        self.assertIn("Bananas", names, "both allowed identities share one list")
        self.assertEqual(self.request("PATCH", f"/api/items/{item['id']}", {"checked": True}, sid=partner)[1]["checked"], True)
        self.assertEqual(self.request("DELETE", f"/api/items/{item['id']}", sid=partner)[0], 204)
        self.assertEqual(self.request("GET", "/api/sections", sid=partner)[0], 200)

    def test_other_identities_get_feedback_only(self):
        stranger, body = self.sign_in("stranger-sub")
        self.assertEqual(body, {"allowed": False})
        self.assertEqual(self.request("GET", "/api/me", sid=stranger)[1], {"allowed": False, "id": "stranger-sub", "isAdmin": False})
        for method, path in [("GET", "/api/items"), ("GET", "/api/sections"), ("GET", "/api/barcode/012345678905"),
                             ("POST", "/api/items"), ("POST", "/api/items/clear-checked"), ("PUT", "/api/items/1")]:
            with self.subTest(path=path):
                status, body, _ = self.request(method, path, {"name": "x"} if method != "GET" else None, sid=stranger)
                self.assertEqual(status, 403)
                self.assertIn("private", body["error"])
        status, post, _ = self.request("POST", "/api/feedback", {"body": "Can I join?"}, sid=stranger)
        self.assertEqual(status, 201)
        self.assertTrue(post["mine"])

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
        # Unknown identities are fine too, so the identity server stops retrying.
        self.assertEqual(self.request("POST", "/api/id/deleted", {"token": self.id.deletion_token("never-here", APP)})[0], 200)

    def test_sign_out(self):
        sid, _ = self.sign_in("owner-sub")
        status, _, res = self.request("POST", "/api/logout", {}, sid=sid)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", res.getheader("Set-Cookie"))
        self.assertEqual(self.request("GET", "/api/me", sid=sid)[0], 401)

    def test_sessions_are_stored_hashed(self):
        sid, _ = self.sign_in("owner-sub")
        with server._db_lock:
            hashes = [r[0] for r in server._db.execute("SELECT token_hash FROM sessions")]
        self.assertNotIn(sid, hashes)
        self.assertIn(server._token_hash(sid), hashes)


class StartupTest(unittest.TestCase):
    def test_id_origin_is_required(self):
        env = {k: v for k, v in os.environ.items() if k != "ID_ORIGIN"}
        res = subprocess.run([sys.executable, str(ROOT / "server.py"), "--db", ":memory:"],
                             capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(res.returncode, 2)
        self.assertIn("ID_ORIGIN is required", res.stderr)


if __name__ == "__main__":
    unittest.main()
