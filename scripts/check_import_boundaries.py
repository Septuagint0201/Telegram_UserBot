"""Fail when source imports violate the modular-monolith dependency direction."""

import ast
import sys
from pathlib import Path

PROJECT_PACKAGE = "telegram_userbot"
COMPOSITION_PROCESS_FILES = frozenset(
    {"processes/conversation_runtime.py", "processes/memory_runtime.py"}
)
ALLOWED_ADAPTER_BRIDGES = frozenset(
    {
        ("embedding", "llm"),
        ("media", "persistence"),
        ("persistence", "media"),
        ("queue", "persistence"),
        ("telegram_bot", "persistence"),
        ("telegram_bot", "webapp"),
        ("webapp", "persistence"),
    }
)
FORBIDDEN_DOMAIN_ROOTS = frozenset(
    {
        "aiogram",
        "anthropic",
        "arq",
        "httpx",
        "openai",
        "redis",
        "sqlalchemy",
        "telegram",
        "telethon",
    }
)


def imported_modules(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return tuple(modules)


def boundary_violations(source_root: Path) -> tuple[str, ...]:
    violations: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root).as_posix()
        layer = relative.split("/", maxsplit=1)[0]
        for imported in imported_modules(path):
            imported_root = imported.split(".", maxsplit=1)[0]
            if layer == "domain":
                if imported_root in FORBIDDEN_DOMAIN_ROOTS:
                    violations.append(f"{relative}: domain imports framework {imported}")
                if imported.startswith(f"{PROJECT_PACKAGE}.") and not imported.startswith(
                    f"{PROJECT_PACKAGE}.domain"
                ):
                    violations.append(f"{relative}: domain imports outer layer {imported}")
            if layer == "application" and imported.startswith(
                (f"{PROJECT_PACKAGE}.adapters", f"{PROJECT_PACKAGE}.processes")
            ):
                violations.append(f"{relative}: application imports outer layer {imported}")
            if (
                layer == "processes"
                and imported.startswith(f"{PROJECT_PACKAGE}.adapters")
                and relative not in COMPOSITION_PROCESS_FILES
            ):
                violations.append(f"{relative}: process imports adapter {imported}")
            if layer == "adapters" and imported.startswith(f"{PROJECT_PACKAGE}.adapters."):
                parts = imported.split(".")
                if len(parts) >= 4:
                    source_adapter = relative.split("/", maxsplit=2)[1]
                    target_adapter = parts[2]
                    if (
                        source_adapter != target_adapter
                        and (
                            source_adapter,
                            target_adapter,
                        )
                        not in ALLOWED_ADAPTER_BRIDGES
                    ):
                        violations.append(
                            f"{relative}: adapter bridge {source_adapter}->{target_adapter} "
                            f"is not declared ({imported})"
                        )
    return tuple(violations)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations = boundary_violations(root / "src" / PROJECT_PACKAGE)
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("PASS import boundaries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
