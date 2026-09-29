# Contact/account erasure: current execution boundary

The worker now advances durable contact/account requests through scope quiescence,
canonical and derived payload redaction, profile/operational metadata redaction,
physical media/export cleanup, budget settlement, and filesystem inventory verification.
Migration `0034_scope_erasure_completion` completes eligible requests and writes their
cumulative HMAC ledger in the same transaction. `docs/Design.md` is unchanged.

## State and recovery

| Stage | Current behavior |
|---|---|
| Scope quiescence | Mark contacts `deleting`, disable proactive contact admission, pause conversations, and cancel active model/memory/proactive jobs. Account wipe additionally marks the account `deleting`. Existing send gates reject that scope. |
| Canonical redaction | Preserve message identities as tombstones; clear every unredacted revision's text, caption, entities and content hash using existing one-way redaction constraints. Late Telegram message projections lock and recheck scope admission. |
| Physical media | Find originals and provider copies through source bindings, message, context, memory-manifest and evidence references. Expedite exclusive families while preserving leases and retry state. Live shared families wait with `ERASURE_MEDIA_SHARED_SCOPE`; other families continue. Unattributed legacy uploads keep contact requests pending with `ERASURE_MEDIA_UNATTRIBUTED_UPLOAD`. |
| Derived cleanup | Clear memory/proposal/summary payloads, COPILOT revisions, outbound bodies, model input/output hashes, context/memory manifest hashes, proactive source payloads and evidence/decision snapshots. Physically delete associated embedding rows. Preserve scope and dependency identities. |
| Operational metadata | Clear scoped account-peer profiles, attachment access references, message metadata, provider request IDs, control/history payloads, job/outbox payloads and audit metadata. Delete reactions and COPILOT edit sessions. Preserve cleanup dispatch and reconciliation identities. |
| Export artifacts | Durable deletion intent immediately revokes overlapping queued/claimed exports. The Operations exporter removes encrypted files and acknowledges absence; until then the worker records `export_artifacts` pending and `ERASURE_EXPORT_PENDING`. |
| Budget settlement | Release only held reservations with proof that no Telegram side effect started. Known sent groups commit; started/partial/unknown groups retain conservative `send_unknown` charges. Insufficient evidence leaves a hold and `ERASURE_BUDGET_PENDING`. |
| Retention review | Clear proactive relationship/timezone preferences, account policy JSON and quiet-window preferences, proposal actor references, and erasure request actor text. See the [retention contract](../architecture/erasure-retention.md). |
| Filesystem inventory | App cleanup checks the account namespace under the writer lock. Every surviving file must belong to a non-deleted object with a live source or reference, including its original/provider family; an account wipe requires no files. Unattached ready uploads and orphan files keep `ERASURE_MEDIA_INVENTORY_PENDING`. Only the filesystem owner can insert the acknowledgment. |
| Finalization | Recheck all phases under request/account locks. When media, exports, budget and inventory are complete, write `completed_at`, a content-free audit event and the HMAC ledger atomically. Repeated wakeups do not duplicate completion. |

The worker revisits unfinished media/export/budget requests by rearming their successful background
jobs after at least one minute. It preserves leased/failed/dead-letter jobs. Previously paused
`ERASURE_DERIVED_CLEANUP_PENDING`, `ERASURE_FINALIZATION_PENDING` and
`ERASURE_METADATA_PENDING` and `ERASURE_FINALIZATION_REVIEW_PENDING` requests can now resume.
Only old failures carrying
`ERASURE_SCOPE_PIPELINE_UNAVAILABLE` are automatically eligible for the new path.
Other failures retain their existing operator-controlled recovery behavior.

The preview maintenance introduced earlier continues independently: revoke previews
and try deleting known Bot message IDs, preserving unknown external outcomes.

## Derived data and concurrency guarantees

Migration `0030_scope_derived_erasure` adds one-way empty payload tombstones to 23
derived tables. Live rows retain their required payload constraints. A tombstone
cannot restore its content or clear its erasure timestamp; redaction cannot change
ownership or model provenance. Immutable sealed manifests permit only this narrow
redaction transition. Memory evidence now has an independent UUID primary key so
its content hash can be removed without dropping media/dependency references; the
original live-evidence deduplication constraint is retained.

The worker follows memory and summary evidence recursively, including all historical
versions of an affected identity. A dependent memory in another conversation is
redacted while unrelated canonical messages stay intact. Manifests containing an
erased source and their linked model/output payloads are invalidated for reuse.
The entire database redaction shares the existing account serialization lock and
transaction; an interruption rolls back the stage and replay is idempotent.

Database triggers reject new derived payloads after durable scope deletion intent,
including late model, embedding, draft and outbound writes. They still permit
content removal and external outcome reconciliation. Pending/retry-wait outbound
intents are cancelled. Sending, sent and unknown records retain their random IDs,
leases and outcome history; a late exact receipt can settle an unknown send without
recreating text. The subsequent budget stage uses the existing side-effect proof and
transactional bucket settlement; replay never decrements or charges a bucket twice.
Already committed/unknown reservations are retained, and database admission fences
reject new holds or rebinding after scope deletion intent.

A downgrade to 0029 is allowed only when no scope payload has been erased. Otherwise
it fails atomically with `MIGRATION_0030_DOWNGRADE_REQUIRES_UNERASED_PAYLOADS`.
It never manufactures hashes or restores deleted payloads to satisfy old constraints.
Apply the matching role grants and deployment schema configuration with the migration.

## Profile and operational metadata

Migration `0032_scope_metadata_erasure` adds one-way metadata tombstones to 17 tables.
Account wipes remove the account display label and timezone; contact wipes remove only
that account's contact profile. A global Telegram peer and its profiles in other accounts
stay independent. The redactor removes usernames, display names, access hashes, locale,
attachment file references and names, message metadata, provider request IDs, requester
labels on drafts, control results, history reasons/actors and free-form audit metadata.
It deletes reactions and draft edit sessions within the requested scope.

The worker calls a database function restricted to an existing contact/account request
whose scope is already quiesced. This function has a fixed search path and no public
execute permission. The worker does not receive general permission to update profiles
or rewrite audit records. Audit actions, results, target identities and timestamps survive;
their actor references, content hashes and metadata are cleared. Late audit/history/event
records retain their facts with empty metadata. Late model receipts and control results
can update outcome/accounting fields while private metadata is discarded.

Affected jobs are cancelled and fenced; terminal job states are retained. Their outbox
notifications are marked consumed without publication and the payloads are emptied.
Only the exact ID-only `memory.reconcile_erasure` job and its matching queue notification
remain runnable, so erasure recovery can continue. Triggers reject profile restoration,
reaction/edit-session recreation, job revival and outbox republication. Telegram peer
admission checks deletion intent before refreshing any profile information.

The database transaction and account lock cover this stage; a failure rolls it back with
the other database redactions. Replay does not advance job fences or duplicate the
redaction audit. Once metadata has been erased, downgrade to 0031 fails atomically with
`MIGRATION_0032_DOWNGRADE_REQUIRES_UNERASED_METADATA`. Upgrade the binaries, deployment
revision configuration and role grants together.

This stage retains stable account/peer/chat/message IDs, deduplication and routing keys,
send-result evidence, counters, policy/configuration snapshots and erasure recovery
records. Account-owned rows with no contact-resolvable target remain until an
account wipe. These retained fields require the final retention review; completing this
metadata stage is not proof of a complete identity purge.

## Encrypted export cleanup

Migration `0031_scope_export_budget` serializes durable contact/account deletion intent
with export admission and publication. A contact deletion also revokes account-wide
exports, because their snapshots can contain that contact. Other contacts' separate
exports and other accounts remain independent. Existing erasure requests are backfilled
during migration. Revocation markers and physical cleanup acknowledgments are one-way;
the worker has read-only access to these export records.

The exporter and cleaner hold the same persistent per-request filesystem lock. A writer
rechecks its database lease after acquiring that lock and before reading or writing.
Cleanup skips live writers and cannot acknowledge deletion until their lock is released.
It removes registered finals, unregistered finals left by a crash, prior attempts and
encrypted temporary files. A registered file must match its recorded digest. Symlinks,
changed files and digest mismatches leave the request unfinished. Directory entries are
synced before the database acknowledgment; interruption before acknowledgment is replay-safe.
Lock files contain no exported data and must not be removed while these tools can run.

Run a bounded cleanup pass with the matching release image (same UID and staging mount
as the exporter):

```bash
docker compose --env-file /etc/telegram-userbot/compose.env \
  -f "$release_root/deploy/compose.yaml" --profile ops \
  run --rm --no-deps data-export /opt/ops/data_export.py --cleanup-due
```

The existing daily data-export timer now runs `--maintenance`: cleanup first, then one
queued export. Use the command above to expedite deletion instead of waiting for that
window. A busy lock is skipped; a successful batch is not proof that every request is
cleaned. Inspect `erasure_cleaned_at` and worker progress. Cleanup errors exit nonzero.

Stop old export/cleanup processes before upgrading and restart them with the matching
0031 code, migration and role grants. Older binaries do not participate in the filesystem
lock protocol. Once an export has an erasure marker, downgrade to 0030 is rejected with
`MIGRATION_0031_DOWNGRADE_REQUIRES_NO_EXPORT_ERASURE` so the cleanup obligation cannot
be lost. Copies already downloaded or moved outside this staging directory are external
copies; this stage does not claim to delete them.

## Physical cleanup when normal app startup is disabled

Migration `0033_media_upload_erasure` records an upload's source revision before the
first filesystem write. Parent/provider copies keep the same source and account.
Database triggers reject uploads after source erasure intent, attachment to erased
sources or objects under deletion, identity changes and resurrection of deleted objects.
The App cleanup loop now claims only its configured account's media.

A shared original/provider family remains protected while another contact still has
an unredacted canonical or derived reference. Exclusive families in the same request
continue independently. When the last live reference is redacted, either request can
schedule the shared family for deletion; both then observe the confirmed result.
Ordinary retention expiry remains applicable. A deleted object cannot keep an old
shared-reference check pending forever. No copy is made solely to bypass an erasure.

Pending, rejected and legacy failed uploads become eligible for physical inspection
after five minutes; a scoped deletion can expedite uploads with known provenance.
The cleaner commits a deletion lease, then takes the same filesystem lock as writers
and persists an object erasure marker before unlinking. It checks registered hashes
and removes exact-UUID unregistered finals and named temporary files. A process dying
after unlink is recoverable: the next lease holder verifies absence. A delayed writer
checks the persistent marker and cannot recreate the file. Markers survive restarts
and must not be removed while old work can still execute.

Legacy anonymous `.ingest-*` files cannot be assigned to a contact safely. Contact
requests wait for unbound legacy upload rows to be checked; anonymous temporary files
cause cleanup failure rather than a false absence acknowledgment. A deleting account's
cleanup entry point fences all account writers and removes these anonymous files under
the same lock. It never scans another account's namespace. Unknown file types,
directories, symlinks and changed registered hashes leave cleanup unfinished.

Stop older app/upload/cleanup processes before upgrading to 0033 and use matching
role grants and binaries; older writers do not check the persistent erasure markers.
The App receives only one additional metadata-column read grant (`scope_erased_at`
on memory manifests). Downgrade is rejected when upload provenance or deletion intent
would be lost: `MIGRATION_0033_DOWNGRADE_REQUIRES_NO_MEDIA_CLEANUP`.

The app normally owns the media filesystem cleanup loop. An account in `deleting`
cannot restart normal Telegram service. Use the dedicated, bounded cleanup entry
point from the reviewed application image and its existing app configuration:

```bash
docker compose --env-file /etc/telegram-userbot/compose.env \
  -f "$release_root/deploy/compose.yaml" \
  run --rm --no-deps app -m telegram_userbot.processes.media_cleanup
```

This command validates schema readiness, uses the app database role and media mount,
and processes up to 50 eligible objects for the configured account. It opens no
Telegram Session, Redis client, or model provider. It prints only counts; a deletion
failure exits nonzero. Repeat batches as necessary, respecting persisted retry
deadlines. A zero exit status means that batch had no deletion failures; it does not
prove that the entire account or contact erasure is complete.

Do not remove database storage keys manually. They are needed for hash-verified
unlink, crash recovery, and fencing against stale cleanup workers. Hash mismatches,
unattributed legacy files, and live shared references must remain visible as unfinished
work. The account and contacts stay in `deleting`, including after local completion,
so duplicate events cannot recreate data. Files without database rows fail inventory
verification and require an explicit Operations repair.

## Bounded offline reconciliation

While normal startup is disabled (including during restore), run the matching Worker
image with its existing Worker configuration:

```bash
docker compose --env-file /etc/telegram-userbot/compose.env \
  -f "$release_root/deploy/compose.yaml" \
  run --rm --no-deps worker -m telegram_userbot.processes.erasure_reconcile
```

This advances up to 50 requests for the configured account, validates schema/roles,
and opens no Telegram, Redis or provider connection. `advanced` counts attempts, not
completed purges. Alternate this with media cleanup and export cleanup until durable
request state is `completed`. The Worker first observes media absence; a later media
pass supplies inventory evidence; the next Worker pass can finalize.

Stop old binaries before upgrading and use matching 0034 code and role scripts.
Downgrade fails with `MIGRATION_0034_DOWNGRADE_REQUIRES_NO_COMPLETION` if retention
redaction, inventory evidence or restore replay records would be lost.

## Operational boundaries

Incomplete delivery evidence, live shared media and unattributed files remain visible
as pending work. Completion covers local content with the documented retained
tombstones. It does not attest erasure of Telegram/platform copies, downloaded exports,
Session/credential volumes or old backups. Preview deletion remains best effort.
Session/backup decommissioning and independent off-host ledger replication remain
Operations work. Real Telegram/provider and complete deployment validation are pending.
