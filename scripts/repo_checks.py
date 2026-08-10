"""Content-free repository, Markdown, disclosure, signature, and secret gates."""

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

PRIVATE_PATH_PATTERN = re.compile(
    r"(?i)(^|/)(\.env|.*\.session(?:-journal)?|id_(?:rsa|ed25519)|.*\.(?:pem|key|p12|pfx))$"
)
MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
SENTINEL_PATTERN = re.compile("TEST_" + "SECRET_" + "DO_NOT_" + r"LOG_[A-Za-z0-9_-]+")
SECRET_PATTERNS = (
    re.compile("BEGIN " + r"(?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile("glpat" + r"-[A-Za-z0-9_-]{20,}"),
    re.compile("gh" + r"[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"[0-9]{8,10}:[A-Za-z0-9_-]{30,}"),
    re.compile("Bearer" + r"\s+[A-Za-z0-9._-]{20,}"),
)
SKIPPED_DIRECTORIES = frozenset({".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv"})
SKIPPED_SUFFIXES = frozenset({".gz", ".pyc", ".whl", ".zip"})


def _run_git(root: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is required")
    completed = subprocess.run(  # noqa: S603 - list invocation with controlled arguments
        [git, *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def tracked_paths(root: Path) -> tuple[Path, ...]:
    output = _run_git(root, "ls-files", "--cached", "--others", "--exclude-standard")
    return tuple(root / line for line in output.splitlines() if line)


def extra_paths(extra_root: Path) -> tuple[Path, ...]:
    if not extra_root.exists():
        return ()
    return tuple(
        path
        for path in extra_root.rglob("*")
        if path.is_file()
        and path.suffix.lower() not in SKIPPED_SUFFIXES
        and not any(part in SKIPPED_DIRECTORIES for part in path.parts)
    )


def _safe_finding(path: Path, line: int, category: str, matched: str) -> str:
    fingerprint = hashlib.sha256(matched.encode()).hexdigest()[:16]
    return f"{path.as_posix()}:{line}: {category} fingerprint={fingerprint}"


def scan_text(path: Path, text: str) -> tuple[str, ...]:
    findings: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern in (*SECRET_PATTERNS, SENTINEL_PATTERN):
            match = pattern.search(line)
            if match:
                findings.append(
                    _safe_finding(path, line_number, "sensitive-content", match.group())
                )
    return tuple(findings)


def check_markdown_links(root: Path, path: Path, text: str) -> tuple[str, ...]:
    findings: list[str] = []
    for match in MARKDOWN_LINK_PATTERN.finditer(text):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path_part = target.split("#", maxsplit=1)[0]
        if path_part and not (path.parent / path_part).resolve().exists():
            findings.append(f"{path.relative_to(root).as_posix()}: broken local link {target}")
    return tuple(findings)


def inspect_paths(root: Path, paths: Iterable[Path]) -> tuple[str, ...]:
    findings: list[str] = []
    for path in sorted(set(paths)):
        relative = path.relative_to(root) if path.is_relative_to(root) else path
        if PRIVATE_PATH_PATTERN.search(relative.as_posix()):
            findings.append(f"forbidden private path: {relative.as_posix()}")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            findings.append(f"non-UTF-8 text artifact: {relative.as_posix()}")
            continue
        if "\x00" in text:
            findings.append(f"NUL byte: {relative.as_posix()}")
        findings.extend(scan_text(relative, text))
        if path.suffix.lower() == ".md" and path.is_relative_to(root):
            findings.extend(check_markdown_links(root, path, text))
    return tuple(findings)


def commit_has_signature(root: Path) -> bool:
    commit_object = _run_git(root, "cat-file", "commit", "HEAD")
    return "\ngpgsig -----BEGIN PGP SIGNATURE-----" in "\n" + commit_object


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-disclosure", action="store_true")
    parser.add_argument("--require-signed-commit", action="store_true")
    parser.add_argument("--scan-extra", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    findings = list(inspect_paths(root, tracked_paths(root)))
    if args.scan_extra:
        findings.extend(inspect_paths(root, extra_paths(root / args.scan_extra)))
    if args.require_disclosure and not (root / "DISCLOSURE").is_file():
        findings.append("root DISCLOSURE is missing")
    if args.require_signed_commit and not commit_has_signature(root):
        findings.append("HEAD has no embedded GPG signature")

    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("PASS repository checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
