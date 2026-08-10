"""Telegram Web App initData and one-time launch-token authentication."""

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qsl

from telegram_userbot.domain.shared.redaction import SensitiveValue

MAX_INIT_DATA_BYTES = 8192


class WebAppAuthenticationError(ValueError):
    """Content-free Web App authentication failure."""


def _reject_duplicate_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WebAppAuthenticationError("WEBAPP_AUTH_REJECTED")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class TelegramWebIdentity:
    user_id: int
    query_id_hash: bytes
    auth_date: datetime


class TelegramInitDataVerifier:
    def __init__(
        self,
        *,
        bot_token: SensitiveValue[str],
        allowed_admin_ids: frozenset[int],
        max_age: timedelta = timedelta(minutes=5),
        future_skew: timedelta = timedelta(seconds=30),
    ) -> None:
        if not allowed_admin_ids or max_age <= timedelta(0) or future_skew < timedelta(0):
            raise WebAppAuthenticationError("WEBAPP_AUTH_CONFIGURATION_INVALID")
        self._bot_token = bot_token
        self._allowed_admin_ids = allowed_admin_ids
        self._max_age = max_age
        self._future_skew = future_skew

    def verify(self, raw: str, *, now: datetime) -> TelegramWebIdentity:
        if now.tzinfo is None or now.utcoffset() is None:
            raise WebAppAuthenticationError("WEBAPP_AUTH_CLOCK_INVALID")
        encoded = raw.encode("utf-8")
        if not encoded or len(encoded) > MAX_INIT_DATA_BYTES or "\x00" in raw:
            raise WebAppAuthenticationError("WEBAPP_AUTH_REJECTED")
        try:
            pairs = parse_qsl(
                raw,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=32,
                encoding="utf-8",
                errors="strict",
            )
        except (UnicodeError, ValueError) as error:
            raise WebAppAuthenticationError("WEBAPP_AUTH_REJECTED") from error
        values: dict[str, str] = {}
        for key, value in pairs:
            if key in values:
                raise WebAppAuthenticationError("WEBAPP_AUTH_REJECTED")
            values[key] = value
        supplied_hash = values.pop("hash", "")
        values.pop("signature", None)
        if len(supplied_hash) != 64:
            raise WebAppAuthenticationError("WEBAPP_AUTH_REJECTED")
        check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(
            b"WebAppData",
            self._bot_token.reveal_for_use().encode("utf-8"),
            hashlib.sha256,
        ).digest()
        expected = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied_hash.lower()):
            raise WebAppAuthenticationError("WEBAPP_AUTH_REJECTED")
        try:
            auth_date = datetime.fromtimestamp(int(values["auth_date"]), tz=UTC)
            user = json.loads(values["user"], object_pairs_hook=_reject_duplicate_json)
            raw_user_id = cast(dict[str, object], user)["id"]
            if not isinstance(raw_user_id, int) or isinstance(raw_user_id, bool):
                raise WebAppAuthenticationError("WEBAPP_AUTH_REJECTED")
            user_id = raw_user_id
            query_id = values["query_id"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as error:
            raise WebAppAuthenticationError("WEBAPP_AUTH_REJECTED") from error
        current = now.astimezone(UTC)
        if auth_date > current + self._future_skew or current - auth_date > self._max_age:
            raise WebAppAuthenticationError("WEBAPP_AUTH_REJECTED")
        if user_id not in self._allowed_admin_ids or not query_id:
            raise WebAppAuthenticationError("WEBAPP_AUTH_REJECTED")
        return TelegramWebIdentity(
            user_id=user_id,
            query_id_hash=hashlib.sha256(query_id.encode("utf-8")).digest(),
            auth_date=auth_date,
        )


@dataclass(frozen=True, slots=True)
class IssuedLaunchToken:
    token: SensitiveValue[str]
    digest: bytes


class LaunchTokenCodec:
    def __init__(self, pepper: SensitiveValue[bytes]) -> None:
        raw = pepper.reveal_for_use()
        if len(raw) != 32:
            raise WebAppAuthenticationError("LAUNCH_TOKEN_CONFIGURATION_INVALID")
        self._pepper = raw

    def issue(self) -> IssuedLaunchToken:
        raw = secrets.token_bytes(32)
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        return IssuedLaunchToken(SensitiveValue(token), self.digest(SensitiveValue(token)))

    def digest(self, token: SensitiveValue[str]) -> bytes:
        raw = token.reveal_for_use()
        if len(raw) != 43 or any(character.isspace() for character in raw):
            raise WebAppAuthenticationError("LAUNCH_TOKEN_REJECTED")
        try:
            encoded = raw.encode("ascii")
        except UnicodeEncodeError as error:
            raise WebAppAuthenticationError("LAUNCH_TOKEN_REJECTED") from error
        return hmac.new(self._pepper, encoded, hashlib.sha256).digest()
