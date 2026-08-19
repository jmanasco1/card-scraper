"""Prove the token request we send matches what eBay's OAuth spec requires.

A live 401 invalid_client points at either the credentials or the request we
build. This pins the request half down against a real socket, so a future 401
can be attributed to the credentials with confidence.
"""
import base64
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ebay_scanner import auth, config  # noqa: E402

CAPTURED = {}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        CAPTURED["body"] = self.rfile.read(length).decode()
        CAPTURED["auth"] = self.headers.get("Authorization")
        CAPTURED["content_type"] = self.headers.get("Content-Type")
        payload = json.dumps({
            "access_token": "TESTTOKEN", "expires_in": 7200,
            "token_type": "Application Access Token",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def main():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    auth.TOKEN_URL = f"http://127.0.0.1:{server.server_port}/identity/v1/oauth2/token"

    config.TOKEN_CACHE = Path("/tmp/_selftest_token.json")
    config.CACHE_DIR = Path("/tmp")
    config.TOKEN_CACHE.unlink(missing_ok=True)

    client_id = "jm-cardscan-PRD-1a2b3c4d5-6e7f8g9h"
    client_secret = "PRD-1a2b3c4d5e6f-7g8h-9i0j-1k2l-3m4n"
    token, minted = auth.get_token(client_id, client_secret)

    assert token == "TESTTOKEN" and minted is True

    # 1. Basic auth must be base64(client_id:client_secret), exactly.
    scheme, _, encoded = CAPTURED["auth"].partition(" ")
    assert scheme == "Basic", CAPTURED["auth"]
    decoded = base64.b64decode(encoded).decode()
    assert decoded == f"{client_id}:{client_secret}", decoded
    print("PASS  Authorization header = Basic base64(id:secret), byte-exact")

    # 2. Content type and body must be form-encoded with the right grant/scope.
    assert CAPTURED["content_type"] == "application/x-www-form-urlencoded"
    fields = urllib.parse.parse_qs(CAPTURED["body"])
    assert fields["grant_type"] == ["client_credentials"], fields
    assert fields["scope"] == ["https://api.ebay.com/oauth/api_scope"], fields
    print("PASS  Content-Type + body: grant_type=client_credentials, correct scope")

    # 3. Credentials must not be leaked into the body.
    assert client_secret not in CAPTURED["body"]
    print("PASS  secret confined to the Authorization header")

    # 4. The cached token must be reused rather than re-minted.
    token2, minted2 = auth.get_token(client_id, client_secret)
    assert token2 == "TESTTOKEN" and minted2 is False
    print("PASS  second call served from cache (no re-auth)")

    config.TOKEN_CACHE.unlink(missing_ok=True)
    server.shutdown()
    print("\nRequest construction is correct per RFC 6749 §2.3.1 and eBay's docs.")


if __name__ == "__main__":
    main()
