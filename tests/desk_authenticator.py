"""A software passkey for tests: a P-256 key that answers WebAuthn like a device.

It builds the same bytes a phone's authenticator would -- client data, the
authenticator data with user-presence and user-verification flags, "none"
attestation, and an ECDSA signature -- so the real ``webauthn`` library checks
every assertion in the tests; nothing about verification is stubbed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftAuthenticator:
    def __init__(self, user_verified: bool = True):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0
        self.flags = 0x01 | (0x04 if user_verified else 0)  # user present, user verified

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    @staticmethod
    def _client(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin,
                           "crossOrigin": False}).encode()

    def register(self, options: dict, origin: str) -> dict:
        rp_hash = hashlib.sha256(options["rp"]["id"].encode()).digest()
        attested = bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + self._cose()
        auth_data = rp_hash + bytes([self.flags | 0x40]) + struct.pack(">I", self.sign_count) + attested
        client = self._client("webauthn.create", options["challenge"], origin)
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(self.credential_id), "rawId": b64u(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client), "attestationObject": b64u(att)},
                "clientExtensionResults": {}}

    def assert_(self, options: dict, origin: str) -> dict:
        self.sign_count += 1
        rp_hash = hashlib.sha256(options["rpId"].encode()).digest()
        auth_data = rp_hash + bytes([self.flags]) + struct.pack(">I", self.sign_count)
        client = self._client("webauthn.get", options["challenge"], origin)
        sig = self.key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.credential_id), "rawId": b64u(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client), "authenticatorData": b64u(auth_data),
                             "signature": b64u(sig)},
                "clientExtensionResults": {}}
