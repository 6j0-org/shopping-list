"""feedback.py on its own: posting and its limits, who sees and deletes what, threads (replies and closing), erasing
a user, and the JSON Lines file (kept across restarts, compacted so deleted text leaves the disk, and a line cut short
by a crash). A port of test/unit/feedback.mjs from the Node apps."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feedback import Feedback, FeedbackError  # noqa: E402

DAY = 24 * 3600 * 1000
ANN, BOB, ADMIN = {"user": "ann"}, {"user": "bob"}, {"user": "dev", "admin": True}


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.file = self.dir / "sub" / "feedback.jsonl"
        self.clock = 1_800_000_000_000

    def tearDown(self):
        self.tmp.cleanup()

    def open(self, **opts):
        args = {"file": self.file, "now": lambda: self.clock, "per_user_per_day": 3, "per_day": 7, **opts}
        return Feedback(**args)

    def fails(self, fn, status, text=None):
        with self.assertRaises(FeedbackError) as cm:
            fn()
        self.assertEqual(cm.exception.status, status, cm.exception.message)
        if text:
            self.assertIn(text, cm.exception.message)

    def test_everything(self):
        fb = self.open()
        self.assertEqual(fb.list(ANN), {"items": [], "maxLength": 2048, "isAdmin": False})

        # Posting: cleaned up, limited in length, and never showing who wrote it.
        a1 = fb.post(ANN, "  The viewer is great.\r\nCould it zoom?\u0007  ")
        self.assertEqual(a1["body"], "The viewer is great.\nCould it zoom?")
        self.assertEqual(sorted(a1), ["admin", "body", "canDelete", "created", "edited", "id", "mine", "replies", "status", "updated"])
        self.assertEqual([a1["status"], a1["replies"]], ["open", 0])
        self.fails(lambda: fb.post(ANN, "   "), 400, "Write something")
        self.fails(lambda: fb.post(ANN, 42), 400)
        self.fails(lambda: fb.post(ANN, "x" * 2049), 400, "up to 2048")
        self.fails(lambda: fb.post({"user": ""}, "hi"), 401)
        self.fails(lambda: fb.list(None), 401)
        self.clock += 1000
        b1 = fb.post(BOB, "x" * 2048)
        self.assertEqual(len(b1["body"]), 2048, "exactly the limit is fine")

        # Everyone sees every post, newest first, and only their own are marked as theirs.
        seen = fb.list(ANN)["items"]
        self.assertEqual([i["id"] for i in seen], [b1["id"], a1["id"]])
        self.assertEqual([[i["mine"], i["canDelete"]] for i in seen], [[False, False], [True, True]])
        self.assertNotIn("bob", json.dumps(fb.list(ANN)), "authors' IDs are never returned")

        # Deleting: your own, or anything as an admin.
        self.fails(lambda: fb.remove(ANN, b1["id"]), 403)
        self.fails(lambda: fb.remove(ANN, "nope"), 404)
        fb.remove(ANN, a1["id"])
        self.assertEqual([i["id"] for i in fb.list(BOB)["items"]], [b1["id"]])
        d1 = fb.post(ADMIN, "Thanks, zoom is coming.")
        self.assertTrue(d1["admin"], "an admin's posts are labeled")
        self.assertTrue(fb.list(BOB)["items"][0]["admin"])
        self.assertTrue(next(i for i in fb.list(ADMIN)["items"] if i["id"] == b1["id"])["canDelete"])
        self.assertTrue(fb.list(ADMIN)["isAdmin"])

        # Limits: 3 per account a day, deleted posts included; 7 from everyone a day (admins count, but aren't
        # limited); a day later it resets.
        fb.post(ANN, "two")
        fb.post(ANN, "three")
        self.fails(lambda: fb.post(ANN, "four"), 429, "3 times a day")
        fb.post(BOB, "b2")
        fb.post({"user": "cat"}, "c1")
        self.fails(lambda: fb.post({"user": "dan"}, "d1"), 429, "lot of feedback")
        fb.post(ADMIN, "admins aren't limited")
        self.clock += DAY
        fb.post(ANN, "a new day")

        # Threads: everyone who can see one replies (replies count toward the limits); only admins close or reopen.
        self.clock += 1000
        eve, fay = {"user": "eve"}, {"user": "fay"}
        t1 = fb.post(eve, "Dark mode, please.")
        self.fails(lambda: fb.reply(eve, "nope", "hi"), 404)
        th = fb.reply(ADMIN, t1["id"], "Which screens?")
        self.assertEqual([th["canReply"], th["canClose"], len(th["messages"]), th["messages"][0]["admin"], th["messages"][0]["mine"]],
                         [True, True, 1, True, True])
        self.clock += 1000
        th = fb.reply(eve, t1["id"], "  All of them.  ")
        self.assertEqual([[m["body"], m["admin"], m["mine"], m["op"]] for m in th["messages"]],
                         [["Which screens?", True, False, False], ["All of them.", False, True, True]])
        self.assertEqual([th["canReply"], th["canClose"], th["replies"]], [True, False, 2])
        self.assertNotIn('"eve"', json.dumps(th))
        self.assertNotIn('"dev"', json.dumps(th))
        self.assertNotIn("author", th, "anonymous without author_key")
        self.clock += 1000
        th = fb.reply(fay, t1["id"], "+1, and the map too.")
        self.assertEqual([th["messages"][2]["op"], th["messages"][2]["mine"], th["canClose"]], [False, True, False])
        self.assertEqual(len(fb.thread(BOB, t1["id"])["messages"]), 3, "everyone can read it")
        self.assertEqual(fb.list(BOB)["items"][0]["id"], t1["id"], "the most recently active thread comes first")
        self.fails(lambda: fb.set_status(eve, t1["id"], "done"), 403, "Only the developers")
        self.fails(lambda: fb.set_status(ADMIN, t1["id"], "maybe"), 400)
        self.assertEqual(fb.done_count("eve"), 0)
        self.assertEqual(fb.set_status(ADMIN, t1["id"], "done")["status"], "done")
        self.assertEqual(fb.done_count("eve"), 1)
        fb.set_status(ADMIN, t1["id"], "wontdo")
        self.assertEqual(fb.done_count("eve"), 0)
        fb.set_status(ADMIN, t1["id"], "open")
        fb.set_status(ADMIN, t1["id"], "done")
        self.assertEqual(fb.author_of(t1["id"]), "eve")
        self.assertIsNone(fb.author_of("nope"))
        fb.reply(eve, t1["id"], "Thanks!")
        self.fails(lambda: fb.reply(eve, t1["id"], "x" * 2049), 400)
        # eve's post and two replies count toward her limit of 3.
        self.fails(lambda: fb.post(eve, "more"), 429)
        self.fails(lambda: fb.reply(eve, t1["id"], "more"), 429)

        # Editing: your own post or reply only; it says when, and counts toward the limits.
        fay_reply = th["messages"][2]["id"]
        self.fails(lambda: fb.edit(BOB, t1["id"], "mine now"), 403, "only edit what you wrote")
        self.fails(lambda: fb.edit(ADMIN, t1["id"], "dev edit", fay_reply), 403)
        self.fails(lambda: fb.edit(fay, t1["id"], "x", "nope"), 404, "reply wasn't found")
        self.fails(lambda: fb.edit(fay, t1["id"], "   ", fay_reply), 400)
        self.clock += 1000
        th = fb.edit(fay, t1["id"], "+1, and the map, too.", fay_reply)
        self.assertEqual([th["messages"][2]["body"], th["messages"][2]["edited"]], ["+1, and the map, too.", self.clock])
        self.assertEqual(th["edited"], 0, "the post itself isn't edited")
        self.fails(lambda: fb.edit(eve, t1["id"], "Dark mode, please!"), 429)
        thread1 = fb.thread(ADMIN, t1["id"])

        # The file: one JSON object per line, posts and deletions.
        lines = [json.loads(line) for line in self.file.read_text().strip().split("\n")]
        self.assertTrue(any(line.get("delete") == a1["id"] for line in lines), "a deletion is appended")
        self.assertTrue(any(line.get("id") == a1["id"] for line in lines), "the post stays until the next start")
        before = fb.list(ADMIN)["items"]

        # Restart: the same posts, and the deleted one's text is gone from the disk.
        fb = self.open()
        self.assertEqual(fb.list(ADMIN)["items"], before, "everything is still there after a restart")
        self.assertEqual(fb.thread(ADMIN, t1["id"]), thread1, "replies, edits, and status too")
        text = self.file.read_text()
        self.assertNotIn('"edit"', text, "edits are folded in when compacted")
        self.assertEqual(fb.done_count("eve"), 1)
        self.assertNotIn("Could it zoom", text, "deleted text is compacted away")
        self.assertNotIn('"delete"', text)
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)

        # A line cut short by a crash: skipped, the original kept, and the next post starts on its own line.
        with open(self.file, "a") as f:
            f.write('{"id":"half","user":"ann","crea')
        fb = self.open()
        self.assertEqual(fb.list(ADMIN)["items"], before)
        self.assertEqual(len([f for f in os.listdir(self.file.parent) if ".unreadable-" in f]), 1, "the original is kept")
        fb.post(BOB, "after the crash")
        self.assertEqual(self.open().list(ADMIN)["items"][0]["body"], "after the crash")

        # Deleting a thread deletes its replies, from the disk too at the next start.
        fb.remove(ADMIN, t1["id"])
        self.fails(lambda: fb.thread(ADMIN, t1["id"]), 404)
        self.assertEqual(fb.done_count("eve"), 0)
        fb = self.open()
        self.assertNotIn("All of them.", self.file.read_text(), "a deleted thread's replies are compacted away")

        # The daily limit counts posts from before a restart ("a new day" above).
        fb = self.open()
        fb.post(ANN, "two today")
        fb.post(ANN, "three today")
        self.fails(lambda: fb.post(ANN, "four today"), 429)

    def test_author_key(self):
        fb = self.open(file=self.dir / "authors.jsonl", author_key=lambda u: "icon-ann" if u == "ann" else None)
        k1 = fb.post(ANN, "hello")
        self.assertEqual(k1["author"], "icon-ann")
        k2 = fb.reply(BOB, k1["id"], "hi")
        self.assertEqual([k2["author"], k2["messages"][0]["author"]], ["icon-ann", None])
        self.assertEqual(fb.list(BOB)["items"][0]["author"], "icon-ann")
        k3 = fb.edit(ANN, k1["id"], "hello!")
        self.assertEqual([k3["body"], k3["edited"] > 0, fb.list(BOB)["items"][0]["body"]], ["hello!", True, "hello!"])

    def test_own_scope(self):
        fb = self.open(scope="own", file=self.dir / "own.jsonl")
        o1 = fb.post(ANN, "mine")
        fb.post(BOB, "his")
        self.assertEqual([i["body"] for i in fb.list(ANN)["items"]], ["mine"])
        self.fails(lambda: fb.remove(BOB, o1["id"]), 404, "wasn't found")
        self.fails(lambda: fb.reply(BOB, o1["id"], "hi"), 404, "wasn't found")
        self.assertEqual(len(fb.list(ADMIN)["items"]), 2)

    def test_erase_user(self):
        efile = self.dir / "erase.jsonl"
        ef = self.open(file=efile, per_user_per_day=100, per_day=100)
        ann_post, bob_post = ef.post(ANN, "ann's idea"), ef.post(BOB, "bob's idea")
        ef.reply(BOB, ann_post["id"], "bob on ann's")
        ef.reply(ANN, bob_post["id"], "ann on bob's")
        ef.reply(BOB, bob_post["id"], "bob on bob's")
        ef.erase_user("ann")

        def check(f):
            self.assertEqual([i["body"] for i in f.list(BOB)["items"]], ["bob's idea"])
            self.assertEqual([m["body"] for m in f.thread(BOB, bob_post["id"])["messages"]], ["bob on bob's"])
            self.fails(lambda: f.thread(BOB, ann_post["id"]), 404)

        check(ef)
        ef = self.open(file=efile, per_user_per_day=100, per_day=100)
        check(ef)
        text = efile.read_text()
        self.assertNotIn("ann", text, "nothing of ann's is left on disk")

    def test_utf16_length_and_lone_surrogates(self):
        fb = self.open()
        # An emoji is two UTF-16 code units, as the browser's maxlength counts it.
        self.assertTrue(fb.post(ANN, "\U0001F955" * 1024))
        self.fails(lambda: fb.post(BOB, "\U0001F955" * 1024 + "x"), 400, "this is 2049")
        # A lone surrogate (valid in JSON, not in UTF-8) is stored as the replacement character.
        p = fb.post({"user": "cat"}, json.loads('"a\\ud800b"'))
        self.assertEqual(p["body"], "a�b")
        self.assertEqual(self.open().list(ANN)["items"][0]["body"], "a�b")

    def test_reads_a_file_written_by_the_node_version(self):
        # Records as feedback.js writes them: a post with a reply, a status change, an edit, and a deleted post.
        self.file.parent.mkdir(parents=True)
        self.file.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in [
            {"id": "p1", "user": "u1", "created": 1, "body": "first"},
            {"reply": "p1", "id": "r1", "user": "u2", "created": 2, "body": "a reply", "admin": True},
            {"status": "p1", "value": "done", "by": "u2", "at": 3},
            {"edit": "r1", "body": "a better reply", "by": "u2", "at": 4},
            {"id": "p2", "user": "u1", "created": 5, "body": "gone soon"},
            {"delete": "p2", "by": "u1", "at": 6},
        ]))
        fb = self.open()
        t = fb.thread({"user": "u1"}, "p1")
        self.assertEqual([t["status"], t["mine"], t["messages"][0]["body"], t["messages"][0]["edited"], t["messages"][0]["admin"]],
                         ["done", True, "a better reply", 4, True])
        self.assertEqual([i["id"] for i in fb.list({"user": "u1"})["items"]], ["p1"])
        self.assertEqual([json.loads(line) for line in self.file.read_text().splitlines()], [
            {"id": "p1", "user": "u1", "created": 1, "body": "first", "status": "done", "closed": 3},
            {"reply": "p1", "id": "r1", "user": "u2", "created": 2, "body": "a better reply", "admin": True, "edited": 4},
        ])


if __name__ == "__main__":
    unittest.main()
