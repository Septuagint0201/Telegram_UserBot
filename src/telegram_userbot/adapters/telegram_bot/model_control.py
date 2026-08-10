"""Framework-independent Control Bot model-configuration command boundary."""

import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from urllib.parse import urlencode

from telegram_userbot.domain.model_config import LogicalRole
from telegram_userbot.domain.shared.redaction import SensitiveValue

POSSIBLE_SECRET = re.compile(
    r"(?i)(?:^sk-[a-z0-9_-]{12,}$|^bearer\s+\S+$|api[_-]?key\s*[:=]|^[a-z0-9_-]{40,}$)"
)


@dataclass(frozen=True, slots=True)
class ModelProfileSummary:
    role: LogicalRole
    state: str
    protocol: str | None
    model_name: str | None
    endpoint_label: str | None
    credential_status: str
    config_version: int | None
    profile_version: int


@dataclass(frozen=True, slots=True)
class ControlSessionPrompt:
    field_name: str
    prompt: str
    draft_version: int


@dataclass(frozen=True, slots=True)
class IssuedKeyLaunch:
    role: LogicalRole
    action: str
    token: SensitiveValue[str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class BotReply:
    text: str
    web_app_url: SensitiveValue[str] | None = field(default=None, repr=False)


class ModelControlBackend(Protocol):
    async def list_profiles(self) -> tuple[ModelProfileSummary, ...]: ...

    async def start_config(
        self, *, admin_id: int, role: LogicalRole, now: datetime
    ) -> ControlSessionPrompt: ...

    async def apply_session_input(
        self, *, admin_id: int, value: str, now: datetime
    ) -> ControlSessionPrompt | None: ...

    async def cancel(self, *, admin_id: int, now: datetime) -> bool: ...

    async def validate(self, *, admin_id: int, role: LogicalRole, now: datetime) -> bool: ...

    async def activate(self, *, admin_id: int, role: LogicalRole, now: datetime) -> bool: ...

    async def issue_key_launch(
        self, *, admin_id: int, role: LogicalRole, action: str, now: datetime
    ) -> IssuedKeyLaunch: ...


class ControlBotModelController:
    def __init__(
        self,
        *,
        allowed_admin_ids: frozenset[int],
        backend: ModelControlBackend,
        web_app_origin: str,
    ) -> None:
        if not allowed_admin_ids or not web_app_origin.startswith("https://"):
            raise ValueError("Control Bot model configuration is invalid")
        self._allowed_admin_ids = allowed_admin_ids
        self._backend = backend
        self._web_app_origin = web_app_origin.rstrip("/")

    async def handle(  # noqa: PLR0911,PLR0912,PLR0915 - command dispatcher
        self, *, admin_id: int, message_text: str, now: datetime
    ) -> BotReply:
        if admin_id not in self._allowed_admin_ids:
            return BotReply("Request rejected.")
        text = message_text.strip()
        if not text:
            return BotReply("No input received.")
        if not text.startswith("/"):
            if POSSIBLE_SECRET.search(text):
                return BotReply("API keys are accepted only by /model_key Web App.")
            try:
                prompt = await self._backend.apply_session_input(
                    admin_id=admin_id, value=text, now=now
                )
            except RuntimeError, ValueError:
                return BotReply("Request rejected; draft unchanged.")
            return (
                BotReply("Draft input saved.")
                if prompt is None
                else BotReply(f"Draft v{prompt.draft_version}: {prompt.prompt}")
            )
        try:
            arguments = shlex.split(text)
        except ValueError:
            return BotReply("Invalid command syntax.")
        command = arguments[0].split("@", maxsplit=1)[0].lower()
        if command == "/models" and len(arguments) == 1:
            try:
                profiles = await self._backend.list_profiles()
            except RuntimeError:
                return BotReply("Model status is temporarily unavailable.")
            lines = ["Model profiles:"]
            for item in profiles:
                model = item.model_name or "not configured"
                protocol = item.protocol or "not configured"
                lines.append(
                    f"{item.role.value}: {item.state}; {protocol}; {model}; "
                    f"key={item.credential_status}; config={item.config_version or '-'}"
                )
            lines.append(
                "Commands: /model_show, /model_config, /model_cancel, "
                "/model_validate, /model_activate, /model_key"
            )
            return BotReply("\n".join(lines))
        if command == "/model_show" and len(arguments) == 2:
            role = _role(arguments[1])
            if role is None:
                return BotReply("Unknown model role.")
            try:
                profile = next(
                    (item for item in await self._backend.list_profiles() if item.role is role),
                    None,
                )
            except RuntimeError:
                profile = None
            if profile is None:
                return BotReply("Model status is temporarily unavailable.")
            return BotReply(
                f"{role.value}: state={profile.state}; protocol={profile.protocol or '-'}; "
                f"model={profile.model_name or '-'}; endpoint={profile.endpoint_label or '-'}; "
                f"key={profile.credential_status}; config={profile.config_version or '-'}; "
                f"profile_version={profile.profile_version}"
            )
        if command == "/model_config" and len(arguments) == 2:
            role = _role(arguments[1])
            if role is None:
                return BotReply("Unknown model role.")
            try:
                prompt = await self._backend.start_config(admin_id=admin_id, role=role, now=now)
            except RuntimeError, ValueError:
                return BotReply("Request rejected; active config unchanged.")
            return BotReply(f"Draft v{prompt.draft_version}: {prompt.prompt}")
        if command == "/model_cancel" and len(arguments) == 1:
            try:
                cancelled = await self._backend.cancel(admin_id=admin_id, now=now)
            except RuntimeError:
                cancelled = False
            return BotReply("Draft cancelled." if cancelled else "No active draft session.")
        if command in {"/model_validate", "/model_activate"} and len(arguments) == 2:
            role = _role(arguments[1])
            if role is None:
                return BotReply("Unknown model role.")
            try:
                accepted = (
                    await self._backend.validate(admin_id=admin_id, role=role, now=now)
                    if command == "/model_validate"
                    else await self._backend.activate(admin_id=admin_id, role=role, now=now)
                )
            except RuntimeError, ValueError:
                accepted = False
            return BotReply(
                "Completed." if accepted else "Request rejected; active config unchanged."
            )
        if command == "/model_key" and len(arguments) in {2, 3}:
            role = _role(arguments[1])
            action = arguments[2].lower() if len(arguments) == 3 else "set"
            if role is None or action not in {"set", "replace", "delete"}:
                return BotReply("Usage: /model_key <role> [set|replace|delete]")
            try:
                launch = await self._backend.issue_key_launch(
                    admin_id=admin_id, role=role, action=action, now=now
                )
            except RuntimeError, ValueError:
                return BotReply("Request rejected; credential unchanged.")
            fragment = urlencode(
                {
                    "role": launch.role.value,
                    "action": launch.action,
                    "launch": launch.token.reveal_for_use(),
                }
            )
            return BotReply(
                "Open the one-time key page. The link expires shortly.",
                SensitiveValue(f"{self._web_app_origin}/webapp/model-key#{fragment}"),
            )
        return BotReply("Unknown model command. Use /models.")


def _role(raw: str) -> LogicalRole | None:
    try:
        return LogicalRole(raw.lower())
    except ValueError:
        return None
