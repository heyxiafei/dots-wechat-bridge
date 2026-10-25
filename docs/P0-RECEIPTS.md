# Owner transport receipt rollout

Build `0.5.0+local.video1.dotledger1.eventlease1.p0receipt1` adds an opt-in
system receipt to the existing bound-owner transport. It has no model runtime.
It does not activate epoch1, change subscription lifetimes, or capture native
dot messages. Existing media, owner checks and delivery protections remain.

The sole worker reads `state-dir/owner-receipts.json` at startup. This private,
owner-owned, regular non-symlink file must have mode 600, at most 4096 bytes,
and exactly these fields:

```json
{"version":1,"enabled":true,"activation_id":"a-new-UUID","grace_ms":150}
```

The first startup of an activation captures all existing owner message IDs in
one immediate transaction. Only later successful inbox inserts register new
receipt intents in that same transaction. Duplicate ingestion and restarts do
not enroll old messages. Do not change activation IDs to replay messages.

After the grace, the single sender atomically chooses the existing shared ACK
slot. Text is `已收到。（系统收件确认）`: durable local receipt only. It does not
assert dot processing, successful work, mirroring, or phone delivery.
A final committed before receipt POST begins suppresses an unsent system ACK.
An existing model ACK wins the same slot; the system never adds a third ACK.
A final arriving after receipt POST begins may produce ACK plus final. The
bridge cannot guarantee that all semantically simple questions are ready
before the grace, because dot scheduling remains external.

Old `reply_weixin_message_phase` callers stay compatible: if a system ACK has
already selected the slot, an ACK call returns the **actual outgoing_text**,
`ack_origin=system_receipt`, and `requested_text_used=false` when the requested
text differs. Callers must mirror the returned canonical text, not the unused
request. A queued/superseded system ACK is ineligible for dot mirror preparation;
only an irreversible send intent or result can be recorded, with its true state.
No history lookup or partial-history guard is relaxed.

Sender wakeups remove idle half-second polling. A bounded burst retains fair
stop checks; receipt deadlines are checked between sends. New, not-yet-started
media preparation can wait for a pending receipt. Already running upload/POST,
network latency, webhook scheduling and phone display latency cannot be
preempted. No one-second end-to-end SLA is claimed.

System receipt POSTs have at most one transport attempt, including explicit
context/rate/auth rejection. Unknown outcomes are never resent. `post_started_at`,
intent `observed_at`/`due_at`, and `accepted_at` are controlled local timestamps;
accepted is not a phone delivery receipt.

## Stop and rollback

Set `enabled=false` in the same private policy and perform a controlled restart
of the existing sole worker to read it. Merely editing the file does not reload
policy. This cancels waiting intents and system ACKs in explicit pre-POST states
(queued/context_required/auth_required with attempts=0); never model ACKs,
finals, sending or unknown attempts.

Before restoring baseline source: verify no live worker/client remains, no
uncertain in-flight sending operation needs reconciliation, and policy disable
has been committed. Keep all added tables/columns and message data. Restore only
reviewed source and policy, never an old database backup over new messages.
Baseline code ignores terminal cancelled/superseded/unknown rows. If safe
quiescence cannot be established, stop rollback rather than replaying anything.

## Acceptance still required

Offline fake API tests establish arbitration and persistence only. A fresh real
owner message must measure phone send, local durable receipt, selected ACK,
POST intent, API acceptance, dot start/final, and phone visibility separately.
Record whether a quick final suppressed ACK and whether a slow task had no more
than one ACK and one final. Do not create fake real inbox rows or replay old
held events as a live test. P1 full dot archiving and P2 native dot export are
separate gates and are not solved by a transport receipt.
