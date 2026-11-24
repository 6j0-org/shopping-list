"""A stand-in for a 6j0.org identity server in tests: serves a JWKS on localhost and signs tokens the way the real
one's tokens.js does (EdDSA JWTs with iss, sub, aud, nonce or events, iat, exp, and jti)."""

import base64
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

DELETED_EVENT = "6j0/v1/identity-deleted"


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def enc(obj):
    return b64u(json.dumps(obj, separators=(",", ":")).encode())


class FakeIdServer:
    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        x = b64u(self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
        self.kid = b64u(hashlib.sha256(x.encode()).digest())[:16]
        self.jwks = {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": x, "kid": self.kid, "alg": "EdDSA", "use": "sig"}]}
        self.jwks_requests = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.jwks_requests += 1
                body = json.dumps(fake.jwks).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.jwks_url = f"{self.origin}/.well-known/jwks.json"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def sign(self, claims, head=None, key=None, ttl=120, now=None):
        now = int(time.time()) if now is None else now
        h = enc(head or {"alg": "EdDSA", "typ": "JWT", "kid": self.kid})
        b = enc({**claims, "iat": now, "exp": now + ttl, "jti": secrets.token_urlsafe(16)} | claims)
        sig = (key or self.key).sign(f"{h}.{b}".encode())
        return f"{h}.{b}.{b64u(sig)}"

    def sign_in_token(self, sub, aud, nonce, **extra):
        return self.sign({"iss": self.origin, "sub": sub, "aud": aud, "nonce": nonce, **extra})

    def deletion_token(self, sub, aud, **extra):
        return self.sign({"iss": self.origin, "sub": sub, "aud": aud, "events": {DELETED_EVENT: {}}, **extra})
