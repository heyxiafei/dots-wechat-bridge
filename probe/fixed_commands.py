"""Compatibility facade and preparation policy for fixed command requests.

The catalog owns text recognition; command_context owns protocol gates and
bounded diagnostics; command_presentation owns display. The coordinator alone
handles trusted-host verification, provider calls and durable final submission.
Existing imports from this module remain supported.
"""
try:
    from .command_catalog import match_command, WEEKLY_USAGE_COMMAND
    from .command_context import (COMMAND_CONTEXT_REASONS, OFFICIAL_PROTOCOL_FIELDS,
        OFFICIAL_PROTOCOL_FIELD_CODES, inspect_command_context,
        capture_command_context, unknown_command_item_diagnostic)
    from .command_presentation import render_weekly_usage
    from .usage_display import UsageBinding, UsageView, normalize_usage
except ImportError:
    from command_catalog import match_command, WEEKLY_USAGE_COMMAND
    from command_context import (COMMAND_CONTEXT_REASONS, OFFICIAL_PROTOCOL_FIELDS,
        OFFICIAL_PROTOCOL_FIELD_CODES, inspect_command_context,
        capture_command_context, unknown_command_item_diagnostic)
    from command_presentation import render_weekly_usage
    from usage_display import UsageBinding, UsageView, normalize_usage

_WEEKLY = WEEKLY_USAGE_COMMAND.aliases


def classify(text, kind, capture, quotes, attachments, reply_state, help_menu):
    """Return a preparation hint only; absent or ambiguous inputs go to model."""
    if not isinstance(text, str) or len(text) > 128 or kind != 'text':
        return None
    if (not isinstance(capture, dict) or capture.get('complete') is not True
            or capture.get('command_context_v1') is not True
            or not isinstance(quotes, dict) or quotes.get('recorded') is not True
            or quotes.get('complete') is not True or quotes.get('status') != 'none'
            or not isinstance(attachments, list) or attachments):
        return None
    parts = capture.get('parts')
    if (not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict)
            or parts[0].get('kind') != 'text' or parts[0].get('state') != 'captured'
            or type(parts[0].get('item_type')) is not int or parts[0]['item_type'] != 1):
        return None
    if not isinstance(reply_state, dict):
        return None
    command = match_command(text)
    if command is None:
        return None
    if command.preparation == 'help_menu':
        if not isinstance(help_menu, dict) or not isinstance(help_menu.get('text'), str):
            return None
        prepared = help_menu['text']
    elif command.preparation == 'weekly_usage':
        prepared = None
    else:
        return None
    for phase in ('ack', 'final'):
        slot = reply_state.get(phase)
        if not isinstance(slot, dict) or type(slot.get('committed')) is not bool:
            return None
        if slot['committed']:
            return {'state': 'already_committed', 'automatic_send': False,
                    'reply_via': 'existing_idempotent_final'}
    return {'state': 'candidate', 'kind': command.name, 'prepared_text': prepared,
            'official_reader_required': command.official_reader_required,
            'automatic_send': False, 'owner_intent_verification_required': True,
            'forwarding_provenance': 'not_provided_by_protocol',
            'reply_via': 'existing_idempotent_final'}



def prepare_weekly_usage(reader, binding, *, now):
    """Call an injected, authorized reader once; never send or discover a reader.

    Invoke only after independent owner intent and account mapping verification.
    No reader ships with the bridge. Reader returns a fresh UsageObservation.
    """
    attempted = False
    if (not isinstance(binding, UsageBinding) or binding.verified is not True
            or any(not isinstance(x, str) or not 0 < len(x) <= 256
                   for x in (binding.owner_ref, binding.account_ref))):
        status, view = 'binding_unverified', UsageView('binding_unverified')
    elif not callable(reader):
        status, view = 'unavailable', UsageView('unavailable')
    else:
        try:
            attempted = True
            view = normalize_usage(reader(binding), binding, now=now)
            status = view.status
        except Exception:
            status, view = 'read_failed', UsageView('read_failed')
    return {'status': status, 'text': render_weekly_usage(view), 'official_call_attempted': attempted}
