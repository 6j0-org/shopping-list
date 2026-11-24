"""idtoken.py: sign-in tokens and deletion notices from a 6j0.org identity server, and everything that must make
them fail."""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import idtoken  # noqa: E402
from tests.fakeid import FakeIdServer, enc  # noqa: E402

APP = "shopping-list"
NONCE = "n" * 32


class IdTokenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.id = FakeIdServer()

    @classmethod
    def tearDownClass(cls):
        cls.id.close()

    def setUp(self):
        # Each test starts with no cached keys, so the JWKS refetch throttle doesn't carry over.
        idtoken._keys = {}
        idtoken._fetched_at = 0.0

    def verify(self, token, nonce=NONCE, audience=APP, issuer=None):
        return idtoken.verify_id_token(token, issuer=issuer or self.id.origin, audience=audience, nonce=nonce,
                                       jwks_url=self.id.jwks_url)

    def deleted(self, token):
        return idtoken.verify_deletion_token(token, issuer=self.id.origin, audience=APP, jwks_url=self.id.jwks_url)

    def fails(self, fn, text):
        with self.assertRaises(idtoken.IdTokenError) as cm:
            fn()
        self.assertIn(text, str(cm.exception))

    def test_good_token_once(self):
        token = self.id.sign_in_token("sub-1", APP, NONCE)
        self.assertEqual(self.verify(token)["sub"], "sub-1")
        self.fails(lambda: self.verify(token), "already used")

    def test_keys_are_cached(self):
        before = self.id.jwks_requests
        self.verify(self.id.sign_in_token("a", APP, NONCE))
        self.verify(self.id.sign_in_token("b", APP, NONCE))
        self.assertEqual(self.id.jwks_requests - before, 1)

    def test_claims_must_match(self):
        tok = lambda **kw: self.id.sign_in_token("sub-1", APP, NONCE, **kw)  # noqa: E731
        self.fails(lambda: self.verify(tok(), nonce="m" * 32), "does not match this sign-in")
        self.fails(lambda: self.verify(tok(), nonce=None), "does not match this sign-in")
        self.fails(lambda: self.verify(tok(), audience="glaze-journal"), "different app")
        self.fails(lambda: self.verify(tok(), issuer="https://id.example"), "different identity server")
        self.fails(lambda: self.verify(tok(iss="https://id.example")), "different identity server")
        with_sub = lambda sub: self.id.sign({"iss": self.id.origin, "sub": sub, "aud": APP, "nonce": NONCE})  # noqa: E731
        self.fails(lambda: self.verify(with_sub("")), "subject")
        self.fails(lambda: self.verify(with_sub("x" * 65)), "subject")
        self.fails(lambda: self.verify(with_sub(["array"])), "subject")
        self.fails(lambda: self.verify(tok(jti="")), "Bad sign-in token")
        now = int(time.time())
        self.fails(lambda: self.verify(self.id.sign({"iss": self.id.origin, "sub": "s", "aud": APP, "nonce": NONCE}, now=now - 600)), "expired")
        self.fails(lambda: self.verify(self.id.sign({"iss": self.id.origin, "sub": "s", "aud": APP, "nonce": NONCE}, now=now + 600)), "future")
        self.fails(lambda: self.verify(tok(exp="soon")), "expired")
        self.fails(lambda: self.verify(tok(exp=True)), "expired")

    def test_signature_and_format(self):
        good = self.id.sign_in_token("sub-1", APP, NONCE)
        h, b, s = good.split(".")
        forged_body = enc({"iss": self.id.origin, "sub": "someone-else", "aud": APP, "nonce": NONCE,
                           "iat": int(time.time()), "exp": int(time.time()) + 60, "jti": "x"})
        self.fails(lambda: self.verify(f"{h}.{forged_body}.{s}"), "signature check failed")
        other = self.id.sign({"iss": self.id.origin, "sub": "s", "aud": APP, "nonce": NONCE}, key=Ed25519PrivateKey.generate())
        self.fails(lambda: self.verify(other), "signature check failed")
        self.fails(lambda: self.verify(self.id.sign({"sub": "s"}, head={"alg": "none", "kid": self.id.kid})), "algorithm")
        self.fails(lambda: self.verify(self.id.sign({"sub": "s"}, head={"alg": "EdDSA", "kid": "nope"})), "unknown key")
        for bad in [None, 42, "", "a.b", "a.b.c.d", "a..c", "!!!.e30.sig", "e30.e30.%%%", "x" * 5000, f"{h}.{b}.{s[:-4]}"]:
            with self.subTest(bad=bad):
                self.assertRaises(idtoken.IdTokenError, self.verify, bad)

    def test_deletion_notices(self):
        sign_in = self.id.sign_in_token("sub-1", APP, NONCE)
        notice = self.id.deletion_token("sub-1", APP)
        self.fails(lambda: self.deleted(sign_in), "not a deletion notice")
        self.fails(lambda: self.verify(notice), "not a sign-in token")
        self.assertEqual(self.deleted(self.id.deletion_token("sub-2", APP))["sub"], "sub-2")
        self.fails(lambda: self.deleted(self.id.deletion_token("sub-3", APP, nonce=NONCE)), "not a deletion notice")
        self.fails(lambda: self.deleted(self.id.deletion_token("sub-3", APP, events={})), "not a deletion notice")

    def test_unreachable_identity_server(self):
        with self.assertRaises(idtoken.JwksError):
            idtoken.verify_id_token(self.id.sign_in_token("s", APP, NONCE), issuer=self.id.origin, audience=APP,
                                    nonce=NONCE, jwks_url="http://127.0.0.1:9/.well-known/jwks.json")


if __name__ == "__main__":
    unittest.main()
