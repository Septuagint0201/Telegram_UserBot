"""Fixed, content-free role-closure assets for the one-shot PostgreSQL migrator."""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path

ROLE_CLOSURE_FILENAMES = (
    "m1_roles.sql",
    "m2_roles.sql",
    "m3_roles.sql",
    "m4_roles.sql",
    "m5_roles.sql",
    "m6_roles.sql",
    "m7_roles.sql",
    "m8_roles.sql",
)
_MAX_ROLE_SCRIPT_BYTES = 64 * 1024


class RoleClosureError(RuntimeError):
    """Stable role-closure asset error that never contains a path or file content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RoleClosureScript:
    name: str
    sql: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.name not in ROLE_CLOSURE_FILENAMES or not self.sql or "\x00" in self.sql:
            raise RoleClosureError("ROLE_CLOSURE_ASSET_INVALID")


@dataclass(frozen=True, slots=True)
class RoleClosurePlan:
    scripts: tuple[RoleClosureScript, ...]

    def __post_init__(self) -> None:
        if tuple(script.name for script in self.scripts) != ROLE_CLOSURE_FILENAMES:
            raise RoleClosureError("ROLE_CLOSURE_ORDER_INVALID")


def load_role_closure_plan(directory: Path) -> RoleClosurePlan:
    """Read only the eight fixed image assets, in release order, without path discovery."""

    scripts: list[RoleClosureScript] = []
    for filename in ROLE_CLOSURE_FILENAMES:
        path = directory / filename
        try:
            metadata = path.lstat()
        except OSError:
            raise RoleClosureError("ROLE_CLOSURE_ASSET_UNAVAILABLE") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or not 0 < metadata.st_size <= _MAX_ROLE_SCRIPT_BYTES
        ):
            raise RoleClosureError("ROLE_CLOSURE_ASSET_INVALID")
        try:
            payload = path.read_bytes()
        except OSError:
            raise RoleClosureError("ROLE_CLOSURE_ASSET_UNAVAILABLE") from None
        if len(payload) != metadata.st_size or b"\x00" in payload:
            raise RoleClosureError("ROLE_CLOSURE_ASSET_INVALID")
        try:
            sql = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise RoleClosureError("ROLE_CLOSURE_ASSET_INVALID") from None
        scripts.append(RoleClosureScript(filename, sql))
    return RoleClosurePlan(tuple(scripts))


__all__ = [
    "ROLE_CLOSURE_FILENAMES",
    "RoleClosureError",
    "RoleClosurePlan",
    "RoleClosureScript",
    "load_role_closure_plan",
]
