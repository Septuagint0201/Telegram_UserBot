"""Transactional-shaped in-memory memory lifecycle used by fakes and tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from telegram_userbot.domain.memory.models import (
    MemoryOperation,
    MemoryProposal,
    MemoryRecord,
    MemoryStatus,
    MemoryVersion,
    ProposalState,
)
from telegram_userbot.domain.memory.validation import ValidatedProposal
from telegram_userbot.domain.shared.time import require_aware


class MemoryConflictError(RuntimeError):
    pass


class MemoryNotFoundError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    proposal_id: UUID
    state: ProposalState
    memory_id: UUID | None
    memory_version_id: UUID | None
    idempotent: bool = False


class MemoryStore:
    """Deterministic store mirroring the acceptance transaction invariants.

    Production persistence uses the same rules inside a PostgreSQL transaction;
    this store intentionally has no provider or network behavior.
    """

    def __init__(self) -> None:
        self._records: dict[UUID, MemoryRecord] = {}
        self._proposal_results: dict[UUID, AcceptanceResult] = {}
        self._semantic_index: dict[tuple[UUID, UUID, bytes], UUID] = {}

    @property
    def records(self) -> tuple[MemoryRecord, ...]:
        return tuple(self._records.values())

    def get(self, memory_id: UUID) -> MemoryRecord:
        try:
            return self._records[memory_id]
        except KeyError as exc:
            raise MemoryNotFoundError(memory_id) from exc

    def active(self, *, account_id: UUID, conversation_id: UUID) -> tuple[MemoryRecord, ...]:
        return tuple(
            record
            for record in self._records.values()
            if record.account_id == account_id
            and record.conversation_id == conversation_id
            and record.status is MemoryStatus.ACTIVE
        )

    def accept(  # noqa: PLR0912, PLR0915 - operation transitions remain explicit
        self,
        validated: ValidatedProposal,
        *,
        acceptance_kind: str = "automatic",
        expected_versions: dict[UUID, int] | None = None,
        now: datetime | None = None,
        allow_candidate: bool = False,
    ) -> AcceptanceResult:
        current_time = datetime.now(UTC) if now is None else require_aware(now, "now")
        prior = self._proposal_results.get(validated.proposal.id)
        if prior is not None:
            return AcceptanceResult(
                prior.proposal_id,
                prior.state,
                prior.memory_id,
                prior.memory_version_id,
                idempotent=True,
            )
        proposal = validated.proposal
        if validated.state is not ProposalState.ACCEPTED and not (
            allow_candidate and validated.state is ProposalState.CANDIDATE
        ):
            result = AcceptanceResult(proposal.id, validated.state, None, None)
            self._proposal_results[proposal.id] = result
            return result
        if acceptance_kind not in {"automatic", "manual", "reconciliation", "migration"}:
            raise ValueError("unknown acceptance kind")
        expected_versions = expected_versions or {}
        targets = [self.get(memory_id) for memory_id in proposal.target_memory_ids]
        for target in targets:
            expected = expected_versions.get(target.id, target.current_version_no)
            if target.current_version_no != expected:
                raise MemoryConflictError("target version changed before acceptance")
            if target.status is not MemoryStatus.ACTIVE:
                raise MemoryConflictError("target memory is not active")
            if proposal.operation in {MemoryOperation.UPDATE, MemoryOperation.INVALIDATE} and (
                target.memory_type is not proposal.memory_type
                or target.semantic_key_hash != proposal.semantic_key_hash
            ):
                raise MemoryConflictError("target memory identity changed")
        if proposal.operation is MemoryOperation.CREATE:
            key = (
                proposal.account_id,
                proposal.conversation_id,
                proposal.semantic_key_hash,
            )
            existing_id = self._semantic_index.get(key)
            if existing_id is not None and self._records[existing_id].status is MemoryStatus.ACTIVE:
                raise MemoryConflictError("semantic key already has an active memory")
            memory_id = self._new_memory_id(proposal)
            version = self._version_for(proposal, memory_id, 1, acceptance_kind, current_time)
            record = MemoryRecord(
                id=memory_id,
                account_id=proposal.account_id,
                conversation_id=proposal.conversation_id,
                memory_type=proposal.memory_type,
                semantic_key_hash=proposal.semantic_key_hash,
                current_version_no=1,
                versions=(version,),
            )
            self._records[memory_id] = record
            self._semantic_index[key] = memory_id
        elif proposal.operation is MemoryOperation.SUPERSEDE:
            target = targets[0]
            key = (
                proposal.account_id,
                proposal.conversation_id,
                proposal.semantic_key_hash,
            )
            existing_id = self._semantic_index.get(key)
            if (
                existing_id is not None
                and existing_id != target.id
                and self._records[existing_id].status is MemoryStatus.ACTIVE
            ):
                raise MemoryConflictError("semantic key already has an active memory")
            memory_id = self._new_memory_id(proposal)
            version = self._version_for(proposal, memory_id, 1, acceptance_kind, current_time)
            self._records[memory_id] = MemoryRecord(
                id=memory_id,
                account_id=proposal.account_id,
                conversation_id=proposal.conversation_id,
                memory_type=proposal.memory_type,
                semantic_key_hash=proposal.semantic_key_hash,
                current_version_no=1,
                versions=(version,),
            )
            self._records[target.id] = MemoryRecord(
                id=target.id,
                account_id=target.account_id,
                conversation_id=target.conversation_id,
                memory_type=target.memory_type,
                semantic_key_hash=target.semantic_key_hash,
                current_version_no=target.current_version_no,
                status=MemoryStatus.SUPERSEDED,
                versions=target.versions,
                superseded_by=memory_id,
            )
            self._semantic_index[key] = memory_id
        else:
            if not targets:
                raise MemoryConflictError("operation target is missing")
            target = targets[0]
            next_version = target.current_version_no + 1
            status = (
                MemoryStatus.INVALIDATED
                if proposal.operation is MemoryOperation.INVALIDATE
                else MemoryStatus.ACTIVE
            )
            version = self._version_for(
                proposal, target.id, next_version, acceptance_kind, current_time, status=status
            )
            record = target.with_version(version, status=status)
            self._records[target.id] = record
            if proposal.operation is MemoryOperation.MERGE:
                for source in targets[1:]:
                    self._records[source.id] = MemoryRecord(
                        id=source.id,
                        account_id=source.account_id,
                        conversation_id=source.conversation_id,
                        memory_type=source.memory_type,
                        semantic_key_hash=source.semantic_key_hash,
                        current_version_no=source.current_version_no,
                        status=MemoryStatus.SUPERSEDED,
                        versions=source.versions,
                        superseded_by=target.id,
                    )
            memory_id = target.id
        result = AcceptanceResult(proposal.id, ProposalState.ACCEPTED, memory_id, version.id)
        self._proposal_results[proposal.id] = result
        return result

    @staticmethod
    def _version_for(  # noqa: PLR0913 - immutable version fields are explicit
        proposal: MemoryProposal,
        memory_id: UUID,
        version_no: int,
        acceptance_kind: str,
        now: datetime,
        *,
        status: MemoryStatus = MemoryStatus.ACTIVE,
    ) -> MemoryVersion:
        return MemoryVersion(
            id=uuid5(memory_id, f"memory-version:{version_no}"),
            memory_id=memory_id,
            version_no=version_no,
            operation=proposal.operation,
            memory_type=proposal.memory_type,
            semantic_key_hash=proposal.semantic_key_hash,
            payload=dict(proposal.payload),
            rendered_text=proposal.rendered_text,
            confidence=proposal.confidence,
            importance=proposal.importance,
            acceptance_kind=acceptance_kind,
            evidence=proposal.evidence,
            status=status,
            created_at=now,
        )

    def _new_memory_id(self, proposal: MemoryProposal) -> UUID:
        ordinal = len(self._records) + 1
        return uuid5(
            proposal.account_id,
            f"memory:{proposal.conversation_id}:{ordinal}:{proposal.semantic_key_hash.hex()}",
        )

    def invalidate_sources(self, source_ids: Iterable[UUID]) -> int:
        source_set = set(source_ids)
        changed = 0
        for memory_id, record in tuple(self._records.items()):
            if record.status is not MemoryStatus.ACTIVE:
                continue
            if any(evidence.source_id in source_set for evidence in record.current.evidence):
                invalidated = MemoryRecord(
                    id=record.id,
                    account_id=record.account_id,
                    conversation_id=record.conversation_id,
                    memory_type=record.memory_type,
                    semantic_key_hash=record.semantic_key_hash,
                    current_version_no=record.current_version_no,
                    status=MemoryStatus.INVALIDATED,
                    versions=record.versions,
                    superseded_by=record.superseded_by,
                )
                self._records[memory_id] = invalidated
                changed += 1
        return changed

    def forget(self, memory_id: UUID) -> MemoryRecord:
        record = self.get(memory_id)
        forgotten = MemoryRecord(
            id=record.id,
            account_id=record.account_id,
            conversation_id=record.conversation_id,
            memory_type=record.memory_type,
            semantic_key_hash=record.semantic_key_hash,
            current_version_no=record.current_version_no,
            status=MemoryStatus.FORGOTTEN,
            versions=tuple(
                MemoryVersion(
                    id=version.id,
                    memory_id=version.memory_id,
                    version_no=version.version_no,
                    operation=version.operation,
                    memory_type=version.memory_type,
                    semantic_key_hash=version.semantic_key_hash,
                    payload={},
                    rendered_text=None,
                    confidence=version.confidence,
                    importance=version.importance,
                    acceptance_kind=version.acceptance_kind,
                    evidence=(),
                    status=MemoryStatus.FORGOTTEN,
                    created_at=version.created_at,
                    redacted_at=datetime.now(UTC),
                )
                for version in record.versions
            ),
            superseded_by=record.superseded_by,
        )
        self._records[memory_id] = forgotten
        self._semantic_index.pop(
            (record.account_id, record.conversation_id, record.semantic_key_hash), None
        )
        return forgotten
