# Exact event and existing-claim reconciliation candidate

Build suffix `eventpump1.reconcile1` is isolated and undeployed. It introduces
no new MCP tools, input parameters, database migration, subscription, credential,
service, network access or message sender. Existing owner-only tools gain
additive metadata. No WeDot implementation is copied; its UNLICENSED code was
only a design reference in an earlier static review.

## Exact original-message event status

`read_weixin_message({message_id})` adds `event_delivery`. Its component uses
SELECTs in a SQLite snapshot, checks the current unique owner, the exact inbox
record and both directions of its `weixin.message` event association. It does
not scan recent history or rely on the latest-ten status window.

The projection includes `event_id`, `assignment_status`, stored event `status`,
`attempts`, `next_at`, fixed HTTP/error categories, `lease_until`, `lease_active`,
the associated subscription's `present/active/expires_at`, and
`dispatch_blocked_reason`. `observed_at` is host epoch seconds. Subscription
scope is the already associated event; an unassigned message has no associated
subscription even if some other grant exists. Invalid reverse links disclose
none of that other event's status or subscription fields.

An unassigned source remains unassigned. Failed/cancelled records retain their
original ID and state. A queued event whose grant is missing/expired is shown
as blocked: a supervisor may have stopped before the pump could mark cancelled.
An active lease is a local callback claim, not a model/run or phone receipt.
Queries never clear a lease, allocate an event, renew a grant, create a claim or
schedule replay. `claim_granted`, `external_write_allowed`, and
`automatic_replay_allowed` are always false. DB state alone cannot establish
current online health; `transport_ready_confirmed` is false.

The new component does not select message bodies, bot/context tokens, callback
URLs/secrets, lease tokens or media keys. The surrounding exact-message MCP
read retains its existing server-side timing observation; that separate
observation is not a new event/claim or permission to send.

## Lost preparation/commit response

`get_weixin_dot_mirror_status({message_id})` preserves its existing `records`.
It adds a separate `reconciliation` projection containing only validated
existing mirror/event/attempt/phase/attachment identities, current state and
existing target receipt metadata. Its flags always say reconciliation only,
no new claim, no external write, and no automatic resend. Empty means no
recoverable existing handle, never proof that dot has no historical display.

This reads the original attempt ID; it never regenerates an identity, prepares
another row, turns prepared into unknown, or changes any stored state. Duplicate
`prepare` still returns `claim_granted=false` and `attempt_id=null`. A lost first
response therefore cannot authorize another dot write.

If a supported target API or lawful history has already produced a real,
explicitly correlated target message ID, the caller can reconcile that existing
receipt through the unchanged metadata commit tool and original attempt/event.
For a lost/uncertain external result, use actual history readback. Unknown still
requires `receipt_source=history_readback`; a conflicting committed target is
rejected. A claimed API response is not verified remotely by the bridge.
Partial history, missing receipts or uncertain correlation keep the record
unresolved. Never resend to obtain a new target ID. The caller's original
authorization, history checks and room/attachment/phase correlation remain
mandatory. This candidate neither activates epoch1 nor changes automation
deduplication or native-dot export boundaries.

## Verification and rollback

Offline tests use temporary SQLite, synthetic messages and fake callbacks with
network/process guards. Check exact events beyond the recent-ten window,
unassigned/expired/missing grants, invalid reverse links, owner changes,
read-only behavior and caller transactions, concurrent readers/committers,
lost prepare/commit responses, unknown restart recovery and target conflicts.
Use the normal complete unittest and schema checks after independent review.

No live testing or rollout is authorized by this file. A later rollout must
stop only the original idle service and preserve its private state. Rollback
restores prior source only; no database snapshot may overwrite newly received
messages or uncertain results. The previous version simply ignores additive
response metadata. Fixed system receipts remain disabled.
