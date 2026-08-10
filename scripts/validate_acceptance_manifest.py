"""Validate acceptance evidence with JSON Schema and semantic rules."""

import argparse
import json
from pathlib import Path

import jsonschema

from telegram_userbot.platform.evidence.manifest import validate_manifest_semantics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    document = json.loads(args.manifest.read_text(encoding="utf-8"))
    schema = json.loads(
        (root / "schemas" / "acceptance-manifest.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        document
    )
    validate_manifest_semantics(
        document,
        required_requirement_ids=frozenset(f"M0-{index:03d}" for index in range(1, 13)),
    )
    print("PASS acceptance manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
