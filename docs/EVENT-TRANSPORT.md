# Event transport candidate

Build `0.5.0+local.video1.dotledger1.eventlease1.p0receipt1.eventpump1` is an
isolated candidate. It has not been deployed or timed on a real phone. The
reference is [wx-bot, fixed revision](https://github.com/lawrencewzen/wx-bot/tree/2fd366a60802a230583cebc89add2fb1c660ce8c),
particularly its durable inbox and onAdd dispatcher. No wx-bot executable or
consumer is installed. This implementation remains the existing Python bridge.

## Changes and retained semantics

- Owner inbox, media references, reply context and iLink cursor remain in the
  same SQLite transaction. New ingress timing is recorded in that transaction.
  A failed transaction does not advance the cursor. Stable IDs handle replay.
- A successful commit wakes the event pump. Attachment success or terminal
  failure also wakes it after commit; pending attachments still block their
  parent event. Clearing the Event precedes DB queries to avoid lost wakeups.
- The pump drains at most eight actions before checking stop and a due-time
  wait. A half-second fallback recovers missed notifications. No model polling
  is introduced. Already-running callback I/O is not preempted.
- Webhook delivery claims use `BEGIN IMMEDIATE`, a 30-second lease token and
  completion compare-and-set. An old callback cannot overwrite a newer claim,
  revive a cancelled event, or expire a refreshed subscription through stale410.
  Leases do not cancel network requests: webhook delivery remains at least once.
- Existing six-attempt budget, backoff, stable event ID, signature framing,
  TLS/public destination checks, permanent failure handling and410 subscription
  revocation remain. Recovery releases abandoned leases but preserves retry
  deadlines and terminal records. There is no automatic dead-letter replay.
- Weixin sender IDs, final/phase idempotency, media limits and uncertain-send
  protections remain. An unknown Weixin POST is never retried by this change.
  Random outgoing IDs in wx-bot are not adopted. Fixed system receipts remain
  disabled unless an operator explicitly configures a separate opt-in policy.

## Forward-only observations

`read_weixin_message` adds `transport_timing` with owner-scoped metadata:

1. ingest observation and post-commit observation;
2. local event queue time;
3. first/last webhook attempt and observed successful HTTP response;
4. first/last successful exact MCP message read;
5. existing final queue, POST intent and Weixin API acceptance times.

Internal reads used for validation do not increment agent-read observations.
Old records retain null timestamps. A historical later read records only that
new read; it never invents an earlier ingest, callback success or model start.
No message body, credentials, callback URL, signature or media key is stored in
this timing table. A successful MCP read is server-side observation, not proof
that a model received the response, started reasoning, or finished work.
HTTP2xx only acknowledges the webhook receiver; it is not a task-run receipt.
Host wall-clock differences are estimates; negative/overlapping or missing
stages return null with a flag. Phone display and source clocks remain unknown.

In-transaction timing failures roll back the corresponding stage. Ingest
failure leaves its cursor unchanged so input can be fetched again. Queue/claim
failures leave that stage uncommitted. If the HTTP-success result transaction
fails, the remote receiver may already have accepted the event; retry may deliver
the same event ID again and requires receiver deduplication. Post-commit and exact
read observations are best effort. Timing failures cannot grant mirror claims
or bypass any history, owner or unknown-result checks.

## Delay limits

Wakeups remove local idle waits and avoid an extra polling delay between ready
queued events. They do not bypass callback network delays, platform event
batching/scheduling, model work, MCP tool round trips, or phone display latency.
Existing old logs have no webhook-accepted or exact agent-read timestamps:
derived retry deadlines are not retroactively stored as measured observations.
No one- or three-second end-to-end guarantee follows from offline tests.

Weixin replies and dot mirrors are separate workflows. An authorized idempotent
reply may proceed while a dot mirror is held for partial/unknown history.
The mirror itself still requires its original reconciliation. There is no
native current-dot user/final export, real external writer-drain proof, or
cross-product exactly-once guarantee in this candidate. epoch1 is not activated.

## Upgrade and rollback

Deploy only after review, complete regression, schema check and explicit rollout
authorization. Stop the existing worker/client normally and establish single
ownership. Back up private state using SQLite backup; replace reviewed source
without overwriting credentials, profiles, subscriptions or messages. New timing
columns/tables are additive and old history is retained.

Before rollback stop the candidate and verify its callback worker is no longer
active. Preserve all new rows and uncertain Weixin results; never restore an
older DB snapshot over newly received messages. Restore only the reviewed prior
source. The prior version ignores added metadata; it cannot enforce lease-CAS,
so it must remain a single callback worker. Do not run old and new consumers
concurrently. Keep the fixed receipt policy disabled through upgrades/rollback.

## Verification matrix

Offline tests cover cursor rollback, after-commit wake, fallback/stop fairness,
concurrent lease claims, late completion, refreshed410 grants, retry deadlines,
restart, terminal preservation, metadata ownership and null historical fields.
Real acceptance still needs one fresh owner message after separately authorized
rollout, exact MCP read, idempotent final, clock-labelled timestamps and actual
phone display evidence. Do not replay old messages to fabricate that test.
