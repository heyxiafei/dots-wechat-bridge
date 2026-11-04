# Connect the original subscription without enabling Weixin workers

`--connection-only` is a bounded foreground stage in this project. It preserves
the exact original discovery response, tools catalog and events catalog. It does
not rescan, republish or update an App/plugin. Catalog presence does not mean
every operation is available during this stage.

Use it only after the existing original Tunnel connection is authorized and its
exact callback host, stable route digest and subscription ID have been verified
from actual platform evidence. A historical receipt identifies the route; it is
not a new authorization, an instruction to replay an old secret, or proof that
the previous writer has stopped. The current platform must send a real
`events/subscribe` request. No old request is synthesized or replayed.

The explicit arguments are `--connection-only`, `--connection-route-digest`,
`--connection-subscription-id`, exactly one `--allow-callback-host`, and an
optional `--connection-duration-seconds` from 1 to 180 (default 180). The route
digest and subscription identity use the existing subscription algorithm. They
are identifiers, not secrets. Secret values and full callback URLs are not CLI
arguments or diagnostic output.

The renderer accepts the same connection options and creates only a private
profile, never a service template for this mode. It does not activate a
connection. Back up the existing private profile before an explicitly authorized
replacement. The normal installer production `connect`/`run` path rejects a
connection-only profile; this is not a shortcut around production approval or
writer fencing. An authorized operator may use the official client's normal
foreground profile command for this finite connection stage. The operator must
also bound the parent Tunnel process: a child deadline alone cannot prevent an
external supervisor from restarting the child. Do not register or enable a
background service for this stage.

The adapter obtains the existing local worker lock and opens the existing
installation database in `mode=rw`, never creating a replacement. It does not
construct `Probe` business stores, run migrations, recover submissions, arm
receipts, read an account token, poll Weixin, deliver events, process media,
queue replies or start any business worker thread. Supported original
subscription schema is checked before use. Views and subscription triggers,
fresh/bounded markers, nonempty business queues and known policy state are
refused. No policy or marker is removed to make the check pass. A SQLite
authorizer additionally denies business writes and account-credential reads.

One narrow connection-only exception retains an old `weixin_new_message_check`
receipt: its exact original 12-column schema must contain exactly one singleton
row with phase `stopped`, finite ordered timestamps, an elapsed lifetime of at
most 180 seconds, and actual NULL source/send IDs. The record must already be
expired. This permits only the original subscription handshake; the entire
diagnostic row remains untouched, and the production fresh-mode gate still
rejects the marker. Active, unexpired, malformed, empty, admitted or sent trial
records do not qualify. Old approval/isolation hash values are not read or
treated as current authorization.

The accepted RPC methods are discovery, initialize, ping, tools/list,
events/list, the two non-content status tools, and an exact original
`weixin.message` subscribe. All other tool calls—including message and
attachment reads with hidden bookkeeping—are rejected before dispatch.
Unsubscribe, synthetic events, mirror operations, notifications, ACK operations
and all activation methods are unavailable. Requests without an ID have no
state effects, even if their method is a write. The adapter never delegates
arbitrary requests to the normal `Probe.rpc` dispatcher.

The single permitted outbound request is the original HTTPS callback challenge,
using the existing DNS/IP restrictions, signature validation, size limits and
subscription revision check. Verification is forced; cached success cannot stand
in for this attempt. On success the exact original subscription row is stored or
refreshed, including the platform-supplied signing secret and requested lifetime
under the existing subscription rules. This is a real configuration write, not
a read-only test. A different subscription ID, callback route or event is
rejected. No subscription is deleted or disabled, and no duplicate is created.

Both status tools return a connection-only label and a random per-process
`instance_id` for matching the local start with the remote MCP request. They
report `connected: false`, disabled business paths and phone delivery not tested,
even after a successful callback. The instance ID is not host identity or a
cross-host ownership lock. The local lock proves no more than exclusion in this
state directory.

Successful execution establishes only the original Tunnel/MCP request path and,
if a live subscribe arrives, callback verification. Whether the platform sends
that request during the finite window remains an actual platform behavior to
observe. It does not prove remote writer isolation, production readiness,
automatic subscription recovery, Weixin arrival, normal reply or phone receipt.
Production continues to require its independent approval and single-writer
conditions. This stage does not create a fresh generation or a fabricated fence.

Rollback stops the finite foreground connection and preserves its actual
subscription receipt. Restore the backed-up private profile only as an explicit
configuration action; do not delete subscription state or start another writer
to make the result look successful. No live rollback is claimed by offline tests.
