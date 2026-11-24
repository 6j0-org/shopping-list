"""Feedback: a page where signed-in people read everyone's feedback and post their own.

A Python port of feedback.js from the 6j0.org apps (bored, glaze-journal), with the same behavior, limits, messages,
and file format, so a feedback.jsonl file reads the same in either. Pair it with static/feedback.js, which draws the
page (it's the same file the Node apps use, unchanged).

Each post is a thread, like a chat: everyone who can see it can reply, and admins (developers) close it as done or
won't do (or reopen it). Self-contained and app-agnostic (standard library only, no knowledge of the app's accounts,
routing, or database), so it can be copied into other Python apps.

Storage is one JSON Lines file: one JSON object per line, appended to and never edited in place, so a crash can't
damage earlier entries and the file reads fine with tail, grep, or jq.
  {"id":"…","user":"…","created":1763600000000,"body":"…"}          a post ("admin":true if an admin wrote it)
  {"reply":"<post id>","id":"…","user":"…","created":…,"body":"…"}   a reply in a post's thread (likewise "admin")
  {"status":"<post id>","value":"done","by":"…","at":…}              closed as "done" or "wontdo", or reopened ("open")
  {"edit":"<post or reply id>","body":"…","by":"…","at":…}           a post or reply edited by its author
  {"delete":"<post id>","by":"…","at":1763600500000}                 a post deleted, with its thread
  {"erase":"<user>","at":…}                                          everything a user wrote deleted (their account was)
The whole file is read at startup and kept in memory. At startup the file is also compacted (rewritten without
deleted posts and their replies, then renamed into place) when anything was deleted or edited, so deleted text doesn't
stay on disk past the next restart. Compacted posts carry their status as "status" and "closed" fields, and edited
posts and replies their newest text, with "edited" saying when. Only one process may write to the file; within it,
every call takes a lock, so a threaded server can share one Feedback.

Who's asking is passed to each call as {"user": ..., "admin": ...}: user is any stable, opaque ID for the account
(never shown to anyone), and admin says whether this account may close any thread and delete anyone's posts (and gets
its posts labeled). Posts and replies are anonymous unless the app passes author_key: a function from a user ID to
what the page may show of the author, which is then returned as each post's and reply's "author".

Usage:
  feedback = Feedback("data/feedback.jsonl")
  feedback.list(who)                  → {items: [item], maxLength, isAdmin}, the most recently active first, where
                                        item is {id, body, created, edited, updated, admin, mine, canDelete, status,
                                        replies} (edited: when it was last edited, or 0)
  feedback.post(who, body)            → the new item
  feedback.thread(who, id)            → {...item, messages: [{id, body, created, edited, admin, mine, op}], canReply, canClose}
  feedback.reply(who, id, body)       → the thread
  feedback.set_status(who, id, s)     → the thread ("open", "done", or "wontdo"; admins only)
  feedback.edit(who, id, body, reply_id=None)  → the thread, with the post (or that reply) changed; its author only
  feedback.remove(who, id)
  feedback.erase_user(user)           deletes every post (with its thread) and reply this account wrote
  feedback.done_count(user)           → how many of this account's posts admins closed as done
  feedback.author_of(id)              → the post's author's user ID (for the app's own use)
Bad input and limits raise FeedbackError with an HTTP status and a message fit to show people.
"""

import json
import os
import re
import secrets
import shutil
import threading
import time
from collections import deque
from functools import wraps

DAY = 24 * 3600 * 1000
STATUSES = ("open", "done", "wontdo")

_CONTROL = re.compile("[\u0000-\u0008\u000b-\u001f\u007f]")
_SURROGATE = re.compile("[\ud800-\udfff]")
_EDGE_SPACE = re.compile(r"^[\s﻿]+|[\s﻿]+$")


class FeedbackError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _now_ms():
    return int(time.time() * 1000)


def _utf16_len(text):
    """Length in UTF-16 code units, as the browser's maxlength (and JavaScript's .length) counts it."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def _dumps(record):
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False)


def _locked(fn):
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class Feedback:
    def __init__(
        self,
        file,
        max_length=2048,       # characters per post or reply (UTF-16 code units, as the browser's maxlength counts them)
        per_user_per_day=100,  # posts, replies, and edits per account in any 24 hours, deleted ones included
        per_day=500,           # from everyone together in any 24 hours, so many accounts can't fill the disk
        list_limit=200,        # most recently active posts returned by list()
        scope="all",           # "all": everyone sees every post; "own": people see only their own (admins see all)
        author_key=None,       # (user ID) → what to show of the author, as "author"; None keeps posts anonymous
        now=_now_ms,
    ):
        if not file:
            raise ValueError("Feedback: file is required")
        self.file = str(file)
        self.max_length = max_length
        self.per_user_per_day = per_user_per_day
        self.per_day = per_day
        self.list_limit = list_limit
        self.scope = scope
        self.author_key = author_key
        self.now = now
        self._lock = threading.RLock()
        # id → post, in the order posted. In memory, each post also has status, closed (when it was last closed or
        # reopened, or 0), updated (its last reply or status change), and replies.
        self._posts = {}
        self._reply_of = {}  # reply id → its post
        self._recent = deque()  # (user, created) of posts, replies, and edits in the last day, for the limits
        os.makedirs(os.path.dirname(os.path.abspath(self.file)), exist_ok=True)
        self._load()

    # ---- loading and compacting ----

    def _remember(self, user, created):
        if isinstance(created, (int, float)) and self.now() - created < DAY:
            self._recent.append((user, created))

    def _erase(self, user):
        """Everything one user wrote: their posts, with their threads, and their replies in everyone else's."""
        for pid, p in list(self._posts.items()):
            if p.get("user") == user:
                del self._posts[pid]
                for r in p["replies"]:
                    self._reply_of.pop(r["id"], None)
                continue
            kept = []
            for r in p["replies"]:
                if r.get("user") == user:
                    self._reply_of.pop(r["id"], None)
                else:
                    kept.append(r)
            p["replies"] = kept

    def _load(self):
        if not os.path.exists(self.file):
            return
        deletions = skipped = 0
        with open(self.file, encoding="utf-8", errors="replace") as f:
            content = f.read()
        for line in content.split("\n"):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:  # e.g. a line cut short by a crash
                skipped += 1
                continue
            if not isinstance(r, dict):
                skipped += 1
                continue
            s = lambda k: isinstance(r.get(k), str)  # noqa: E731
            if s("delete"):
                self._posts.pop(r["delete"], None)
                deletions += 1
            elif s("erase"):
                self._erase(r["erase"])
                deletions += 1
            elif s("edit") and s("body"):
                p = self._posts.get(r["edit"])
                parent = self._reply_of.get(r["edit"])
                x = p or (next((y for y in parent["replies"] if y["id"] == r["edit"]), None) if parent else None)
                if x is not None and (p is not None or parent["id"] in self._posts):
                    x["body"] = r["body"]
                    x["edited"] = r.get("at")
                deletions += 1  # folded into the post or reply when compacted
                self._remember(r.get("by"), r.get("at"))
            elif s("status") and r.get("value") in STATUSES:
                p = self._posts.get(r["status"])
                if p is not None:
                    p["status"] = r["value"]
                    p["closed"] = r.get("at") or 0
                    p["updated"] = max(p["updated"], p["closed"])
            elif s("reply") and s("id") and s("body"):
                p = self._posts.get(r["reply"])
                if p is not None:
                    rest = {k: v for k, v in r.items() if k != "reply"}
                    p["replies"].append(rest)
                    self._reply_of[r["id"]] = p
                    p["updated"] = max(p["updated"], r.get("created") or 0)
                else:
                    deletions += 1  # its post was deleted, so leave it out of the compacted file
                self._remember(r.get("user"), r.get("created"))
            elif s("id") and s("body"):
                status = r["status"] if r.get("status") in STATUSES else "open"
                closed = r.get("closed") or 0
                self._posts[r["id"]] = {
                    **r, "status": status, "closed": closed,
                    "updated": max(r.get("created") or 0, closed), "replies": [],
                }
                self._remember(r.get("user"), r.get("created"))
            else:
                skipped += 1
        self._recent = deque(sorted(self._recent, key=lambda e: e[1]))
        # Unreadable lines are left out of the compacted file (which also ends a line cut short, so the next append
        # starts on a line of its own), with the original kept next to it.
        if skipped:
            keep = f"{self.file}.unreadable-{self.now()}"
            shutil.copyfile(self.file, keep)
            print(f"feedback: skipped {skipped} unreadable line{'' if skipped == 1 else 's'} in {self.file}; "
                  f"the original is in {keep}", flush=True)
        if deletions or skipped:
            lines = []
            for p in self._posts.values():
                base = {k: v for k, v in p.items() if k not in ("status", "closed", "updated", "replies")}
                if p["status"] == "open" and not p["closed"]:
                    lines.append(base)
                else:
                    lines.append({**base, "status": p["status"], "closed": p["closed"]})
                for r in p["replies"]:
                    lines.append({"reply": p["id"], **r})
            tmp = f"{self.file}.{os.getpid()}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(_dumps(line) + "\n" for line in lines))
            os.replace(tmp, self.file)

    def _append(self, record):
        fd = os.open(self.file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (_dumps(record) + "\n").encode("utf-8"))
        finally:
            os.close(fd)

    # ---- views ----

    def _visible(self, p, who):
        return bool(who.get("admin")) or self.scope == "all" or p["user"] == who["user"]

    def _author(self, user):
        return {"author": self.author_key(user)} if self.author_key else {}

    def _view(self, p, who):
        return {
            "id": p["id"], "body": p["body"], "created": p["created"], "edited": p.get("edited") or 0,
            "updated": p["updated"], "admin": bool(p.get("admin")),
            "mine": p["user"] == who["user"], "canDelete": bool(who.get("admin")) or p["user"] == who["user"],
            "status": p["status"], "replies": len(p["replies"]), **self._author(p["user"]),
        }

    def _thread_view(self, p, who):
        return {
            **self._view(p, who),
            "messages": [
                {
                    "id": r["id"], "body": r["body"], "created": r["created"], "edited": r.get("edited") or 0,
                    "admin": bool(r.get("admin")), "mine": r["user"] == who["user"], "op": r["user"] == p["user"],
                    **self._author(r["user"]),
                }
                for r in p["replies"]
            ],
            "canReply": True,  # everyone who can see a thread can reply
            "canClose": bool(who.get("admin")),
        }

    # ---- checks ----

    @staticmethod
    def _check(who):
        if not isinstance(who, dict) or not isinstance(who.get("user"), str) or not who["user"]:
            raise FeedbackError(401, "Please sign in.")

    def _find(self, who, pid):
        p = self._posts.get(pid) if isinstance(pid, str) else None
        if p is None or not self._visible(p, who):
            raise FeedbackError(404, "That feedback wasn't found.")
        return p

    def _clean(self, body):
        if not isinstance(body, str):
            raise FeedbackError(400, "Feedback must be text.")
        # Keep line breaks and tabs; drop other control characters. Lone surrogates (which JSON can carry but UTF-8
        # can't store) become the replacement character.
        text = _SURROGATE.sub("�", body)
        text = _CONTROL.sub("", re.sub(r"\r\n?", "\n", text))
        text = _EDGE_SPACE.sub("", text)
        if not text:
            raise FeedbackError(400, "Write something first.")
        n = _utf16_len(text)
        if n > self.max_length:
            raise FeedbackError(400, f"Feedback can be up to {self.max_length} characters; this is {n}.")
        return text

    def _limit(self, who, t):
        """The daily limits, for posts, replies, and edits alike. Admins count toward everyone's total, but aren't limited."""
        while self._recent and t - self._recent[0][1] >= DAY:
            self._recent.popleft()
        if who.get("admin"):
            return
        if sum(1 for u, _ in self._recent if u == who["user"]) >= self.per_user_per_day:
            raise FeedbackError(429, f"You can post up to {self.per_user_per_day} times a day. Please try again tomorrow.")
        if len(self._recent) >= self.per_day:
            raise FeedbackError(429, "There's been a lot of feedback today. Please try again tomorrow.")

    @staticmethod
    def _new_id():
        return secrets.token_urlsafe(9)

    # ---- the API ----

    @_locked
    def list(self, who):
        self._check(who)
        shown = [p for p in reversed(self._posts.values()) if self._visible(p, who)]  # ties: newest first
        shown.sort(key=lambda p: -p["updated"])
        items = [self._view(p, who) for p in shown[: self.list_limit]]
        return {"items": items, "maxLength": self.max_length, "isAdmin": bool(who.get("admin"))}

    @_locked
    def post(self, who, body):
        self._check(who)
        text, t = self._clean(body), self.now()
        self._limit(who, t)
        p = {"id": self._new_id(), "user": who["user"], "created": t, "body": text,
             **({"admin": True} if who.get("admin") else {})}
        self._append(p)
        self._posts[p["id"]] = {**p, "status": "open", "closed": 0, "updated": t, "replies": []}
        self._recent.append((p["user"], t))
        return self._view(self._posts[p["id"]], who)

    @_locked
    def thread(self, who, pid):
        self._check(who)
        return self._thread_view(self._find(who, pid), who)

    @_locked
    def reply(self, who, pid, body):
        self._check(who)
        p = self._find(who, pid)
        text, t = self._clean(body), self.now()
        self._limit(who, t)
        r = {"id": self._new_id(), "user": who["user"], "created": t, "body": text,
             **({"admin": True} if who.get("admin") else {})}
        self._append({"reply": p["id"], **r})
        p["replies"].append(r)
        self._reply_of[r["id"]] = p
        p["updated"] = t
        self._recent.append((r["user"], t))
        return self._thread_view(p, who)

    @_locked
    def set_status(self, who, pid, value):
        self._check(who)
        p = self._find(who, pid)
        if not who.get("admin"):
            raise FeedbackError(403, "Only the developers can close feedback.")
        if value not in STATUSES:
            raise FeedbackError(400, f"Status must be one of: {', '.join(STATUSES)}.")
        if p["status"] != value:
            t = self.now()
            self._append({"status": p["id"], "value": value, "by": who["user"], "at": t})
            p["status"], p["closed"], p["updated"] = value, t, t
        return self._thread_view(p, who)

    @_locked
    def edit(self, who, pid, body, reply_id=None):
        self._check(who)
        p = self._find(who, pid)
        x = p if reply_id is None else next((r for r in p["replies"] if r["id"] == reply_id), None)
        if x is None:
            raise FeedbackError(404, "That reply wasn't found.")
        if x["user"] != who["user"]:
            raise FeedbackError(403, "You can only edit what you wrote.")
        text, t = self._clean(body), self.now()
        if text == x["body"]:
            return self._thread_view(p, who)
        self._limit(who, t)
        self._append({"edit": x["id"], "body": text, "by": who["user"], "at": t})
        x["body"], x["edited"] = text, t
        self._recent.append((who["user"], t))
        return self._thread_view(p, who)

    @_locked
    def remove(self, who, pid):
        self._check(who)
        p = self._find(who, pid)
        if not who.get("admin") and p["user"] != who["user"]:
            raise FeedbackError(403, "You can only delete your own feedback.")
        self._append({"delete": pid, "by": who["user"], "at": self.now()})
        del self._posts[pid]

    @_locked
    def erase_user(self, user):
        if not isinstance(user, str) or not user:
            return
        self._append({"erase": user, "at": self.now()})
        self._erase(user)

    @_locked
    def done_count(self, user):
        return sum(1 for p in self._posts.values() if p["user"] == user and p["status"] == "done")

    @_locked
    def author_of(self, pid):
        p = self._posts.get(pid)
        return p["user"] if p else None
