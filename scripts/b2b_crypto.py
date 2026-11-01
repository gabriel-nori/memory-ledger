"""B2B's cryptography, on the brain's side (docs/b2b.md#the-crypto).

The one module of the engine that is not stdlib: it needs `cryptography`
(X25519, HKDF, AES-GCM), which is in `requirements-server.txt`. `b2b.py`
imports it only where a key is used, and its command line re-runs itself under
the server's venv when this import fails.

* An identity is an X25519 key pair. Its **user id** is the public key, 32
  bytes as unpadded base64url. The private key stays in `b2b.json`.
* A **challenge** from a relay is a secret sealed to the user id with an
  ephemeral key; `open_challenge` returns it.
* A **message** is sealed by the sender to the recipient before it leaves:
  the key is HKDF over two exchanges, ephemeral-to-recipient and
  sender-to-recipient. The first gives each message a fresh key; the second
  means only the holder of `from`'s private key could have sealed it, so the
  recipient knows who sent it without trusting the relay.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import uuid
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CHALLENGE_INFO = b"b2b/v2/challenge"
MESSAGE_INFO = b"b2b/v2/message"
FILE_INFO = b"b2b/v2/file/"


class BadBox(Exception):
    """A challenge or envelope that does not open with this key."""


def b64(raw: bytes) -> str:
    """Unpadded base64url."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    """The inverse of `b64`. Raises `BadBox` on anything that is not."""
    try:
        return base64.urlsafe_b64decode(str(text) + "=" * (-len(str(text)) % 4))
    except (binascii.Error, TypeError, ValueError) as e:
        raise BadBox(str(e)) from None


def generate() -> tuple[str, str]:
    """A new identity: `(private_key, user_id)`, both base64url."""
    priv = X25519PrivateKey.generate()
    return b64(priv.private_bytes_raw()), b64(priv.public_key().public_bytes_raw())


def _private(private_key: str) -> X25519PrivateKey:
    raw = unb64(private_key)
    if len(raw) != 32:
        raise BadBox("a private key is 32 bytes")
    return X25519PrivateKey.from_private_bytes(raw)


def _public(user_id: str) -> X25519PublicKey:
    raw = unb64(user_id)
    if len(raw) != 32:
        raise BadBox("a user id is a 32-byte public key")
    return X25519PublicKey.from_public_bytes(raw)


def user_id_of(private_key: str) -> str:
    """The user id (public key) of a private key. Raises `BadBox`."""
    return b64(_private(private_key).public_key().public_bytes_raw())


def is_user_id(value: str) -> bool:
    """Whether `value` is a well-formed user id."""
    try:
        _public(value)
        return len(value) == 43
    except (BadBox, ValueError):
        return False


def open_challenge(private_key: str, challenge_id: str, box: dict[str, Any]) -> str:
    """The secret a relay sealed to this key, as the text to answer with."""
    priv = _private(private_key)
    try:
        eph = unb64(box["eph"])
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                   info=CHALLENGE_INFO + eph + priv.public_key().public_bytes_raw()
                   ).derive(priv.exchange(X25519PublicKey.from_public_bytes(eph)))
        return AESGCM(key).decrypt(unb64(box["nonce"]), unb64(box["ct"]),
                                   challenge_id.encode()).decode("utf-8")
    except (InvalidTag, KeyError, TypeError, ValueError, UnicodeDecodeError) as e:
        raise BadBox(f"the challenge does not open with this key: {e}") from None


def _message_key(own: X25519PrivateKey, eph_shared: bytes, other: X25519PublicKey,
                 eph: bytes, sender: bytes, recipient: bytes, msg_id: str) -> bytes:
    static = own.exchange(other)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=MESSAGE_INFO + eph + sender + recipient + msg_id.encode()
                ).derive(eph_shared + static)


def seal(private_key: str, to: str, payload: dict[str, Any],
         msg_id: str = "") -> dict[str, Any]:
    """`payload` (JSON) sealed from this identity to the user id `to`. Returns
    the envelope a relay carries: `{v, id, from, eph, nonce, ct}`. `msg_id`
    (32 hex) is the message's id when the caller chose it ahead, as a staged
    message does; else a new one."""
    me, them = _private(private_key), _public(to)
    sender = me.public_key().public_bytes_raw()
    recipient = them.public_bytes_raw()
    eph_priv = X25519PrivateKey.generate()
    eph = eph_priv.public_key().public_bytes_raw()
    msg_id = msg_id or uuid.uuid4().hex
    key = _message_key(me, eph_priv.exchange(them), them, eph, sender, recipient, msg_id)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             MESSAGE_INFO)
    return {"v": 2, "id": msg_id, "from": b64(sender), "eph": b64(eph),
            "nonce": b64(nonce), "ct": b64(ct)}


def open_envelope(private_key: str, env: dict[str, Any]) -> dict[str, Any]:
    """The payload of an envelope sealed to this identity. Opening proves it
    was sealed by `env["from"]`. Raises `BadBox` otherwise."""
    me = _private(private_key)
    try:
        sender_pub = _public(env["from"])
        eph = unb64(env["eph"])
        recipient = me.public_key().public_bytes_raw()
        key = _message_key(me, me.exchange(X25519PublicKey.from_public_bytes(eph)), sender_pub,
                           eph, sender_pub.public_bytes_raw(), recipient, str(env["id"]))
        out = json.loads(AESGCM(key).decrypt(unb64(env["nonce"]), unb64(env["ct"]),
                                             MESSAGE_INFO))
    except (InvalidTag, KeyError, TypeError, ValueError) as e:
        raise BadBox(f"the message does not open with this key: {e}") from None
    if not isinstance(out, dict):
        raise BadBox("the message is not an object")
    return out


def seal_file(key: bytes, nonce: bytes, file_id: str, data: bytes) -> bytes:
    """A file's bytes sealed with its own random key, which travels inside the
    sealed message, never next to the file. The same key, nonce and bytes give
    the same blob, so a staged upload can be redone."""
    return AESGCM(key).encrypt(nonce, data, FILE_INFO + file_id.encode())


def open_file(key: bytes, nonce: bytes, file_id: str, blob: bytes) -> bytes:
    """The inverse of `seal_file`. Raises `BadBox`."""
    try:
        return AESGCM(key).decrypt(nonce, blob, FILE_INFO + file_id.encode())
    except (InvalidTag, ValueError) as e:
        raise BadBox(f"the file does not open with its key: {e}") from None
