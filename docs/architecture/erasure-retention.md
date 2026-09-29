# Erasure retention contract (0034)

`completed` on a contact/account request means application-owned content has been
redacted, exclusive media and overlapping export artifacts have been checked absent,
budget evidence has been conservatively settled, and the media namespace has passed
an independent inventory. It is not a claim that every row or external copy was deleted.

| Data | Decision and reason |
|---|---|
| Canonical/derived text, images, vectors, source hashes and model snapshots | Erase, including dependent historical versions; history cannot preserve deleted content. |
| Display names, usernames, attachment names/references, provider request IDs, metadata | Erase through the 0032 contract. |
| Contact relationship classification, timezone and contact limits/intervals | Erase all settings versions and disable them. |
| Proactive occurrence/candidate/decision/projection timezone labels, source-projection validity dates and bucket timezone snapshot | Erase; bucket UTC boundaries, dates and counters remain sufficient for settlement. |
| Account proactive policy JSON, timezone and quiet-window preferences | Erase on account wipe and disable policies. Contact deletion leaves unrelated account-wide policy intact. Fixed safety hours and numeric budget limits remain as accounting constraints. |
| Proposal decision actor text and erasure request actor text | Erase; action, request identity, policy version and timestamps remain. |
| Account/contact/conversation/message IDs, Telegram routing and dedup IDs, random send IDs, revisions and dependency links | Retain as tombstones to reject replay and identify late exact receipts. Shared global Telegram identity rows are separate from private account profiles. |
| Send attempts/outcomes, unknown results, authorization generations, lease/fencing history, budget counts | Retain to reconcile external side effects and avoid duplicate sends or false refunds. |
| Audit action/result/target identity, machine reason codes, version identifiers, counts and timestamps | Retain content-free evidence. Human actor references, body hashes and audit JSON were removed in 0032. Numeric control administrator/Bot routing IDs remain authorization and external-receipt evidence. |
| Erasure requests/progress, inventory acknowledgments and cumulative ledger | Retain for crash recovery and restore. Export only keyed scope HMACs, request UUID/idempotency key, policy version and historical completion time. |
| Global operator model/prompt configuration, encrypted credentials, Session and backups | Separate deployment assets. Account retirement requires Operations decommissioning. Operator configuration must not serve as a private-chat archive. |

Filesystem inventory uses the upload lock and leaves durable object/account erasure
markers. A contact request can retain files only with a live source/reference proving
ownership of the surviving object family, after its own complete family is deleted.
An unattached ready upload cannot pass solely because its database row exists.
Unknown files, anonymous remnants,
symlinks and files for deleted rows are not certified. An account wipe requires an
empty account file namespace. Stop old writers before migration or restore maintenance.

Completion, audit and ledger insertion share one transaction. Inventory is written
only by the App filesystem owner, never by the Worker. Failed transactions cannot
produce premature ledger entries. The independent ledger's completion time remains
a historical fact during restoration; local progress and filesystem checks are reset
for each new restore generation before access can reopen.

External Telegram previews retain their independent best-effort deletion status.
Remote chat history, notifications, forwarded/saved copies and downloaded exports
are outside local completion. Independent ledger replication and actual backup/restore
drills still require deployment evidence.
