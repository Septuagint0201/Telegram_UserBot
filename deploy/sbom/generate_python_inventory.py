"""Generate a content-free CycloneDX Python dependency/license inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime
from importlib import metadata
from pathlib import Path

LOCK_LINE = re.compile(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)")
IMAGE_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _locked_packages(lock_path: Path) -> tuple[tuple[str, str], ...]:
    packages: dict[str, tuple[str, str]] = {}
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        match = LOCK_LINE.match(line)
        if match is None:
            continue
        name, version = match.groups()
        packages[_canonical_name(name)] = (name, version)
    if not packages:
        raise ValueError("lock file contains no pinned distributions")
    return tuple(packages[key] for key in sorted(packages))


def _license_name(distribution: metadata.Distribution) -> str:
    expression = distribution.metadata.get("License-Expression", "").strip()
    if expression:
        return expression
    declared = distribution.metadata.get("License", "").strip()
    if declared and "\n" not in declared and len(declared) <= 200:
        return declared
    classifiers = distribution.metadata.get_all("Classifier") or []
    license_classifiers = sorted(
        item.removeprefix("License :: ") for item in classifiers if item.startswith("License :: ")
    )
    return "; ".join(license_classifiers) or "NOASSERTION"


def build_inventory(
    *, lock_path: Path, image_reference: str, source_commit: str, created_at: str
) -> dict[str, object]:
    if IMAGE_REFERENCE.fullmatch(image_reference) is None:
        raise ValueError("image reference must use an immutable sha256 digest")
    if SOURCE_COMMIT.fullmatch(source_commit) is None:
        raise ValueError("source commit must be a full lowercase SHA-1")
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except ValueError as error:
        raise ValueError("created-at must be an RFC 3339 timestamp") from error
    if parsed_created_at.tzinfo is None:
        raise ValueError("created-at must include a timezone")

    installed = {_canonical_name(item.metadata["Name"]): item for item in metadata.distributions()}
    components: list[dict[str, object]] = []
    mismatches: list[str] = []
    for locked_name, locked_version in _locked_packages(lock_path):
        canonical = _canonical_name(locked_name)
        distribution = installed.get(canonical)
        if distribution is None:
            mismatches.append(f"{canonical}=={locked_version}:missing")
            continue
        if distribution.version != locked_version:
            mismatches.append(f"{canonical}=={locked_version}:installed-{distribution.version}")
            continue
        components.append(
            {
                "type": "library",
                "name": canonical,
                "version": locked_version,
                "bom-ref": f"pkg:pypi/{canonical}@{locked_version}",
                "purl": f"pkg:pypi/{canonical}@{locked_version}",
                "licenses": [{"license": {"name": _license_name(distribution)}}],
            }
        )
    if mismatches:
        raise ValueError("installed distributions do not match lock: " + ", ".join(mismatches))

    identity = hashlib.sha256(f"{source_commit}\n{image_reference}".encode()).hexdigest()
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.UUID(identity[:32])}",
        "version": 1,
        "metadata": {
            "timestamp": created_at,
            "component": {
                "type": "container",
                "name": image_reference.split("@", maxsplit=1)[0],
                "version": image_reference.rsplit("@", maxsplit=1)[1],
                "bom-ref": image_reference,
            },
            "properties": [
                {
                    "name": "telegram-userbot:inventory-scope",
                    "value": "python-installed-distributions",
                },
                {
                    "name": "telegram-userbot:whole-image-sbom-required",
                    "value": "true",
                },
                {"name": "telegram-userbot:source-commit", "value": source_commit},
            ],
        },
        "components": components,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inventory = build_inventory(
        lock_path=args.lock,
        image_reference=args.image_reference,
        source_commit=args.source_commit,
        created_at=args.created_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
