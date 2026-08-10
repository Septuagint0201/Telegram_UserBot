"""Inspect source/wheel builds before any artifact can be retained."""

import argparse
import io
import re
import tarfile
import zipfile
from pathlib import Path

SENSITIVE_PATTERN = re.compile(
    "|".join(
        (
            "BEGIN " + r"(?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY",
            r"AKIA[0-9A-Z]{16}",
            "glpat" + r"-[A-Za-z0-9_-]{20,}",
            r"[0-9]{8,10}:[A-Za-z0-9_-]{30,}",
            "Bearer" + r"\s+[A-Za-z0-9._-]{20,}",
            "TEST_" + "SECRET_" + "DO_NOT_" + r"LOG_[A-Za-z0-9_-]+",
        )
    )
)
PRIVATE_NAME_PATTERN = re.compile(r"(?i)(^|/)(\.env|.*\.session|.*\.(?:pem|key|p12|pfx))$")


def _inspect_members(members: dict[str, bytes], *, wheel: bool) -> tuple[str, ...]:
    findings: list[str] = []
    names = tuple(members)
    disclosure_present = (
        "telegram_userbot/DISCLOSURE" in names
        if wheel
        else any(name.endswith("/DISCLOSURE") for name in names)
    )
    if not disclosure_present:
        findings.append("DISCLOSURE missing")
    for name, content in members.items():
        if PRIVATE_NAME_PATTERN.search(name):
            findings.append(f"private path: {name}")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if SENSITIVE_PATTERN.search(text):
            findings.append(f"sensitive content: {name}")
    return tuple(findings)


def inspect_wheel(path: Path) -> tuple[str, ...]:
    with zipfile.ZipFile(path) as archive:
        return _inspect_members(
            {name: archive.read(name) for name in archive.namelist()}, wheel=True
        )


def inspect_sdist(path: Path) -> tuple[str, ...]:
    members: dict[str, bytes] = {}
    with tarfile.open(path, mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile():
                extracted = archive.extractfile(member)
                if extracted is not None:
                    members[member.name] = io.BytesIO(extracted.read()).getvalue()
    return _inspect_members(members, wheel=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    wheels = tuple(args.directory.glob("*.whl"))
    sdists = tuple(args.directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        print("expected exactly one wheel and one sdist")
        return 1
    findings = (*inspect_wheel(wheels[0]), *inspect_sdist(sdists[0]))
    if findings:
        print("\n".join(findings))
        return 1
    print("PASS build artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
