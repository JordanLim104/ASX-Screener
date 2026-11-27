#!/usr/bin/env python3
"""
Push a notification to the installed app when a refresh adds stocks to the screen.

Runs after tv_screen.py in the daily workflow. It reads `changes.added` from
docs/data/latest.json and, if anything was added, sends a Web Push message to
every subscription in the PUSH_SUBSCRIPTIONS secret, signed with the
VAPID_PRIVATE_KEY secret. Nothing is sent when nothing was added.

Headline rules: one or two new stocks are named by ticker; three or more are
given as a count.

Web Push is implemented directly (RFC 8291 aes128gcm encryption + RFC 8292
VAPID) with the `cryptography` package, so no extra dependency is needed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SITE = "https://jordanlim104.github.io/ASX-Screener/"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --------------------------------------------------------------------------- #
# Message text
# --------------------------------------------------------------------------- #
def build_message(doc: dict) -> dict | None:
    added = (doc.get("changes") or {}).get("added") or []
    if not added:
        return None
    by = {m["ticker"]: m for m in doc.get("matches", [])}
    if len(added) == 1:
        title = f"{added[0]} added to Wounded Mid-Caps"
    elif len(added) == 2:
        title = f"{added[0]} and {added[1]} added to Wounded Mid-Caps"
    else:
        title = f"{len(added)} stocks added to Wounded Mid-Caps"

    def line(t):
        m = by.get(t)
        if not m:
            return t
        return f"{t} {int(m['drawdown_pct'] + 0.5)}% below its high"  # round half up, like the app

    body = " · ".join(line(t) for t in added[:4]) + (f" · +{len(added) - 4} more" if len(added) > 4 else "")
    return {"title": title, "body": body, "tickers": added, "run_at": doc.get("run_at"), "url": "./"}


# --------------------------------------------------------------------------- #
# Web Push
# --------------------------------------------------------------------------- #
def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """RFC 8291 aes128gcm body for one subscription."""
    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = server_key.public_key().public_bytes(serialization.Encoding.X962,
                                                     serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh = server_key.exchange(ec.ECDH(), ua_key)
    prk_key = hmac.new(auth_secret, ecdh, hashlib.sha256).digest()
    ikm = _hkdf_expand(prk_key, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", 4096, len(as_public)) + as_public + ciphertext


def vapid_headers(endpoint: str, private_key_b64u: str) -> dict:
    key = ec.derive_private_key(int.from_bytes(unb64u(private_key_b64u), "big"), ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    u = urlparse(endpoint)
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
                              "sub": SITE}, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f"{header}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return {"Authorization": f"vapid t={jwt}, k={b64u(pub)}"}


def send(sub: dict, message: dict, private_key: str) -> int:
    body = encrypt(json.dumps(message).encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
    headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
               "TTL": str(36 * 3600), "Urgency": "normal", **vapid_headers(sub["endpoint"], private_key)}
    return requests.post(sub["endpoint"], data=body, headers=headers, timeout=20).status_code


def main(data_dir="docs/data"):
    doc = json.load(open(Path(data_dir) / "latest.json"))
    message = build_message(doc)
    if not message:
        print("No stocks added this refresh; no notification.")
        return
    print(f"Notification: {message['title']} | {message['body']}")
    key, subs_raw = os.environ.get("VAPID_PRIVATE_KEY"), os.environ.get("PUSH_SUBSCRIPTIONS")
    if not key or not subs_raw:
        print("VAPID_PRIVATE_KEY or PUSH_SUBSCRIPTIONS not set; skipping push "
              "(the installed app's background check can still notify).")
        return
    subs = json.loads(subs_raw)
    subs = subs if isinstance(subs, list) else [subs]
    for i, sub in enumerate(subs, 1):
        try:
            status = send(sub, message, key)
            note = {201: "sent", 410: "subscription expired; re-copy it from the app",
                    404: "subscription not found; re-copy it from the app"}.get(status, "")
            print(f"  subscription {i}: HTTP {status} {note}")
        except Exception as e:
            print(f"  subscription {i}: failed ({e})", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
