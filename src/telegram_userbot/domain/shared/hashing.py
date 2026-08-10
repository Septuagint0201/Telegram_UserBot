"""Stable content hashing helpers."""

import hashlib
import json
from collections.abc import Mapping, Sequence

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | Mapping[str, "JsonValue"] | Sequence["JsonValue"]


def stable_json_bytes(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_json_sha256(value: JsonValue) -> str:
    return sha256_hex(stable_json_bytes(value))
