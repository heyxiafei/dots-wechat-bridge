"""Candidate producer hook: only reviewed monitor validation may create intents.

No import or read of production bridge, tokens, platform state, or chats.
The caller supplies the already reviewed monitor module; it must not be loaded
from an announcement field or another untrusted path.
"""


import re
from .relay import key_for
from .store_helpers import Invalid
from .text_b import build_display, format_b, RICH_STYLE, UNAVAILABLE_STYLE

# Observed in the public source and included in its CodexResetEvent type enum.
KNOWN_BANKED_TYPES = frozenset({'reset_credit'})

def skipped_banked(raw, view):
    """Recognize only the observed banked receipt-review shape, never a reset.

    Unknown/missing/contradictory forms fail closed instead of being ignored.
    """
    eid = raw.get('id')
    if not isinstance(eid, str) or not re.fullmatch(r'banked-[0-9]{5,25}-[1-9][0-9]{0,2}-[1-9][0-9]{0,2}', eid):
        return False
    typ = raw.get('type')
    return (isinstance(typ, str) and typ in KNOWN_BANKED_TYPES and typ != 'direct_reset'
            and raw.get('status') == 'confirmed'
            and raw.get('confirmationBasis') == 'receipt_review'
            and 'confirmedAt' in raw and raw['confirmedAt'] is None
            and isinstance(raw.get('presentation'), dict)
            and raw['presentation'].get('status') == 'confirmed'
            and isinstance(view, dict) and view.get('eligible') is False)


def capture_verified(relay, monitor, payload, now):
    try:
        checked, normalized, _ = monitor.envelope(payload, now)
        views = monitor.notification_view(payload, checked)
        skipped = set()
        versions = {}
        for event in payload['events']:
            eid = event['id']
            if skipped_banked(event, views[eid]):
                skipped.add(eid)
                continue
            key_for(eid, 'announced')  # Unknown and contradictory IDs remain blocked.
            if event.get('type') != 'direct_reset':
                raise Invalid('event_category_conflict')
            versions[eid] = monitor.stamp(event['updatedAt'])
        canonical_ids = set(versions)
        terminal = {event['id']: 'confirmed' for event in payload['events'] if event['id'] not in skipped and event.get('status') == 'confirmed'}
        for activity in payload['activities']:
            action = activity.get('action')
            reason = 'withdrawn' if action == 'withdraw' else 'confirmed' if action == 'confirm' else 'unknown' if action is None and activity.get('statusChanged') is True else None
            if reason is not None:
                for eid in activity['eventIds']:
                    if eid in skipped:
                        continue
                    key_for(eid, 'announced')
                    if terminal.get(eid) not in ('withdrawn', 'unknown'):
                        terminal[eid] = reason
                    # Event versions and activity timestamps are different clocks.
                    # A related activity can roll out of /recent without changing
                    # the canonical event. Never inflate that event's updatedAt.
                    # Missing events still retain terminal tombstone watermarks.
                    if eid not in canonical_ids:
                        versions[eid] = max(versions.get(eid, 0), monitor.stamp(activity['publishedAt']))
        intents = []
        raw_by_id = {raw["id"]: raw for raw in payload["events"]}
        for eid, view in views.items():
            if eid in skipped:
                continue
            if not view['eligible'] or normalized[eid]['reason'] == 'conflicting_forecasts':
                continue
            try:
                display = build_display(payload, raw_by_id[eid], view, style=RICH_STYLE)
                format_b(display)  # Fail display independently before the relay capture.
            except (ValueError, TypeError, KeyError, OverflowError):
                # Do not silently fall back to a different template for either channel.
                display = {"version": 1, "style": UNAVAILABLE_STYLE}
            intents.append(dict(event_id=eid, stage=view['stage'], source_at=view['source_at'],
                                expires_at=view['expires_at'], text=view['text'],
                                link=view['text'].splitlines()[-1], eligible=True, display=display))
        return relay.sync(intents, now, checked_at=checked, healthy=True, terminal=terminal, versions=versions)
    except Exception:
        relay.suspend()
        raise
