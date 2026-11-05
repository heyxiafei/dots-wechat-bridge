"""Use the bridge's canonical ledger and explicit operator configuration.

The producer creates intents, never network sends. Sharing the implementation
keeps hashes, immutable requests and receipt reconciliation identical on both
sides; no private recipient is supplied here.
"""

from probe.aihot_notice_consumer.relay import (
    CHANNELS, FRESHNESS, LIMIT, STATES, Relay, email_payload, finite, identity,
    key_for, validate_intent, wechat_text,
)

__all__ = ['CHANNELS', 'FRESHNESS', 'LIMIT', 'STATES', 'Relay', 'email_payload',
           'finite', 'identity', 'key_for', 'validate_intent', 'wechat_text']
