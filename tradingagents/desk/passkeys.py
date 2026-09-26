"""Passkeys for Desk's money actions: approving a plan, resuming, acknowledging.

Reaching Desk is not enough to send orders. Each money action carries a fresh
WebAuthn assertion -- Face ID or Touch ID on the device -- over a challenge made
for that action and that plan, used once, within two minutes.

Enrolling a device needs a one-time code shown in the Mac's terminal when Desk
starts (or by ``tradingagents desk code``), so a device that can merely reach
Desk cannot enrol itself. A passkey belongs to the host name it was made on
(the relying party): one made at ``localhost`` does not work at the Mac's
Tailscale name, so each name enrols its own.

Credentials are kept in ``<trading_dir>/desk/passkeys.json`` -- public keys and
sign counters only; the private key never leaves the device.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

RP_NAME = "Trade-Agents Desk"
CHALLENGE_SECONDS = 120
ENROLL_SECONDS = 15 * 60
# The actions a passkey guards. Halting is deliberately not one: stopping
# trading must never wait on a fingerprint.
MONEY_ACTIONS = ("submit", "resume", "ack")


class PasskeyError(ValueError):
    pass


@dataclass
class _Challenge:
    purpose: str          # "register" | "auth"
    rp_id: str
    action: str
    subject: str
    expires: float


def _dir(config: dict) -> Path:
    from tradingagents.trading.book import trading_dir

    d = trading_dir(config) / "desk"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Passkeys:
    def __init__(self, config: dict, clock=time.time):
        self.config = config
        self.clock = clock
        self._lock = threading.Lock()
        self._challenges: dict[str, _Challenge] = {}

    # -- enrolment codes ----------------------------------------------------------
    # Kept as a hash in a file only the operator can read, so `tradingagents desk
    # code` (another process) can issue one for the running server.

    def _code_path(self) -> Path:
        return _dir(self.config) / "enrol.json"

    def new_code(self) -> str:
        """A one-time enrolment code, valid for fifteen minutes, replacing any other."""
        code = "".join(secrets.choice("23456789ABCDEFGHJKMNPQRSTUVWXYZ") for _ in range(8))
        path = self._code_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sha256": hashlib.sha256(code.encode()).hexdigest(),
                                   "expires": self.clock() + ENROLL_SECONDS}))
        tmp.chmod(0o600)
        tmp.replace(path)
        return code

    def _take_code(self, code: str) -> None:
        wrong = PasskeyError("that enrolment code is wrong or has expired; "
                             "run `tradingagents desk code` on the Mac for a new one")
        with self._lock:
            try:
                held = json.loads(self._code_path().read_text())
            except (OSError, ValueError):
                raise wrong from None
            given = hashlib.sha256(code.strip().upper().replace("-", "").encode()).hexdigest()
            if held.get("expires", 0) <= self.clock() or not secrets.compare_digest(given, held.get("sha256", "")):
                raise wrong
            self._code_path().unlink(missing_ok=True)  # one use

    # -- storage ------------------------------------------------------------------

    def _path(self) -> Path:
        return _dir(self.config) / "passkeys.json"

    def _load(self) -> dict[str, list[dict]]:
        try:
            return json.loads(self._path().read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        tmp = self._path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self._path())

    def credentials(self, rp_id: str) -> list[dict]:
        return self._load().get(rp_id, [])

    def remove(self, rp_id: str, credential_id: str) -> bool:
        data = self._load()
        before = len(data.get(rp_id, []))
        data[rp_id] = [c for c in data.get(rp_id, []) if c["id"] != credential_id]
        self._save(data)
        return len(data[rp_id]) < before

    # -- challenges -----------------------------------------------------------------

    def _issue(self, purpose: str, rp_id: str, action: str = "", subject: str = "") -> bytes:
        challenge = secrets.token_bytes(32)
        now = self.clock()
        with self._lock:
            self._challenges = {k: v for k, v in self._challenges.items() if v.expires > now}
            self._challenges[bytes_to_base64url(challenge)] = _Challenge(
                purpose, rp_id, action, subject, now + CHALLENGE_SECONDS)
        return challenge

    def _redeem(self, credential: dict, purpose: str, rp_id: str, action: str = "",
                subject: str = "") -> bytes:
        """The challenge the browser signed, checked against what it was issued for, and spent."""
        try:
            client = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
            key = client["challenge"]
        except (KeyError, TypeError, ValueError) as exc:
            raise PasskeyError("that is not a passkey response") from exc
        with self._lock:
            c = self._challenges.pop(key, None)
        if c is None or c.expires <= self.clock():
            raise PasskeyError("the passkey prompt expired; try again")
        if (c.purpose, c.rp_id, c.action, c.subject) != (purpose, rp_id, action, subject):
            raise PasskeyError("that passkey approval was for something else")
        return base64url_to_bytes(key)

    # -- registration ---------------------------------------------------------------

    def registration_options(self, rp_id: str, code: str) -> str:
        self._take_code(code)
        existing = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"])) for c in self.credentials(rp_id)]
        options = generate_registration_options(
            rp_id=rp_id, rp_name=RP_NAME, user_name="operator", user_display_name="Desk operator",
            user_id=b"desk-operator", challenge=self._issue("register", rp_id),
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=existing)
        return options_to_json(options)

    def verify_registration(self, rp_id: str, origin: str, credential: dict, label: str = "") -> dict:
        challenge = self._redeem(credential, "register", rp_id)
        try:
            v = verify_registration_response(credential=credential, expected_challenge=challenge,
                                             expected_rp_id=rp_id, expected_origin=origin,
                                             require_user_verification=True)
        except Exception as exc:  # the library raises several types; all mean "not verified"
            raise PasskeyError(f"the passkey could not be verified: {exc}") from exc
        row = {"id": bytes_to_base64url(v.credential_id), "public_key": bytes_to_base64url(v.credential_public_key),
               "sign_count": v.sign_count, "label": (label or "device")[:60],
               "created": datetime.now(UTC).isoformat(timespec="seconds")}
        with self._lock:
            data = self._load()
            data.setdefault(rp_id, []).append(row)
            self._save(data)
        return {k: row[k] for k in ("id", "label", "created")}

    # -- authentication ---------------------------------------------------------------

    def auth_options(self, rp_id: str, action: str, subject: str = "") -> str:
        if action not in MONEY_ACTIONS:
            raise PasskeyError(f"{action!r} does not take a passkey")
        creds = self.credentials(rp_id)
        if not creds:
            raise PasskeyError("no passkey is enrolled for this address yet; enrol one under Security")
        options = generate_authentication_options(
            rp_id=rp_id, challenge=self._issue("auth", rp_id, action, subject),
            allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"])) for c in creds],
            user_verification=UserVerificationRequirement.REQUIRED)
        return options_to_json(options)

    def verify(self, rp_id: str, origin: str, credential: dict, action: str, subject: str = "") -> None:
        """Raises PasskeyError unless ``credential`` is a fresh assertion for exactly this action."""
        if not isinstance(credential, dict):
            raise PasskeyError("this action needs your passkey")
        challenge = self._redeem(credential, "auth", rp_id, action, subject)
        with self._lock:
            data = self._load()
            stored = next((c for c in data.get(rp_id, []) if c["id"] == credential.get("id")), None)
            if stored is None:
                raise PasskeyError("that passkey is not enrolled here")
            try:
                v = verify_authentication_response(
                    credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
                    expected_origin=origin, credential_public_key=base64url_to_bytes(stored["public_key"]),
                    credential_current_sign_count=stored["sign_count"], require_user_verification=True)
            except Exception as exc:
                raise PasskeyError(f"the passkey could not be verified: {exc}") from exc
            stored["sign_count"] = v.new_sign_count
            stored["last_used"] = datetime.now(UTC).isoformat(timespec="seconds")
            self._save(data)
