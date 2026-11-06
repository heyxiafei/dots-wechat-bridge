"""Conservative protocol-shape checks and bounded, non-authoritative diagnostics.

This module never recognizes a request as authorized, opens account data or
sends a message. Its acceptance gates and diagnostic vocabulary are independent
of display text and host execution.
"""
import math
try:
    from .command_catalog import match_command
except ImportError:
    from command_catalog import match_command

_REFERENCE_MARKER_REASONS = {
    'ref_msg': ('message_reference_marker_null', 'message_reference_marker_false', 'message_reference_marker_other'),
    'quote': ('message_quote_marker_null', 'message_quote_marker_false', 'message_quote_marker_other'),
    'quoted_message': ('message_quoted_message_marker_null', 'message_quoted_message_marker_false', 'message_quoted_message_marker_other'),
    'forward': ('message_forward_marker_null', 'message_forward_marker_false', 'message_forward_marker_other'),
    'forwarded': ('message_forwarded_marker_null', 'message_forwarded_marker_false', 'message_forwarded_marker_other'),
    'forward_info': ('message_forward_info_marker_null', 'message_forward_info_marker_false', 'message_forward_info_marker_other'),
}
_KNOWN_NONCOMMAND_ITEM_REASONS = {
    'ref_msg': 'item_reference_field_present',
    'image_item': 'item_image_field_present',
    'voice_item': 'item_voice_field_present',
    'file_item': 'item_file_field_present',
    'video_item': 'item_video_field_present',
}
COMMAND_CONTEXT_REASONS = frozenset((
    'standalone_text_shape', 'message_not_object',
    'item_list_type_invalid',
    'item_count_not_one', 'item_not_object', 'item_required_fields_missing',
    'item_unknown_fields', 'item_content_type_invalid', 'item_not_text',
    'item_time_type_invalid', 'item_time_value_invalid',
    'item_completion_type_invalid', 'item_not_completed',
    'item_id_type_invalid', 'item_id_size_invalid',
    'text_item_not_object', 'text_item_required_field_missing',
    'text_item_unknown_fields', 'text_type_invalid', 'text_too_long',
)) | frozenset(code for codes in _REFERENCE_MARKER_REASONS.values() for code in codes) \
   | frozenset(_KNOWN_NONCOMMAND_ITEM_REASONS.values())


# Closed vocabulary of interface property names in Tencent/openclaw-weixin
# src/api/types.ts at 874637a0dbc24050f72d1385add4fc6170a2d726
# (Git blob 276e845eea75d0e7f48e3a5d418f93636628481b).
# Includes names from other protocol objects ONLY for rejection diagnostics:
# being in this vocabulary does not make a field valid on a text MessageItem.
OFFICIAL_PROTOCOL_FIELDS = (
    'aes_key', 'aeskey', 'base_info', 'bits_per_sample',
    'bot_agent', 'channel_version', 'client_id', 'context_token',
    'create_time_ms', 'delete_time_ms', 'encode_type', 'encrypt_query_param',
    'encrypt_type', 'end', 'endindex', 'errcode',
    'errmsg', 'file_item', 'file_name', 'filekey',
    'filesize', 'from_user_id', 'full_url', 'get_updates_buf',
    'group_id', 'hd_size', 'ilink_user_id', 'image_item',
    'is_completed', 'item_list', 'len', 'longpolling_timeout_ms',
    'md5', 'media', 'media_type', 'message_id',
    'message_item', 'message_state', 'message_type', 'mid_size',
    'msg', 'msg_id', 'msgs', 'no_need_thumb',
    'partial_text', 'play_length', 'playtime', 'quotemd5',
    'rawfilemd5', 'rawsize', 'ref_msg', 'ret',
    'run_id', 'sample_rate', 'seq', 'session_id',
    'start', 'startindex', 'status', 'svr_id',
    'sync_buf', 'text', 'text_item', 'thumb_filesize',
    'thumb_height', 'thumb_media', 'thumb_rawfilemd5', 'thumb_rawsize',
    'thumb_size', 'thumb_upload_param', 'thumb_width', 'title',
    'to_user_id', 'tool_call_id', 'tool_call_result_item', 'tool_call_start_item',
    'tool_name', 'type', 'typing_ticket', 'update_time_ms',
    'upload_full_url', 'upload_param', 'url', 'video_item',
    'video_md5', 'video_size', 'voice_item',
)
OFFICIAL_PROTOCOL_FIELD_CODES = tuple(
    (name, 'official_' + name) for name in OFFICIAL_PROTOCOL_FIELDS)
_ALLOWED_COMMAND_ITEM_FIELDS = frozenset((
    'type', 'text_item', 'create_time_ms', 'update_time_ms', 'is_completed', 'msg_id'))
_JSON_TYPES = frozenset(('null', 'boolean', 'number', 'string', 'array', 'object', 'non_json'))


def _time_metadata_status(item, key):
    if key not in item:
        return 'absent'
    value = item[key]
    if type(value) not in (int, float):
        return 'type_invalid'
    if not 0 <= value <= 10**15 or not math.isfinite(value):
        return 'value_invalid'
    return 'valid'


def _completion_metadata_status(item):
    if 'is_completed' not in item:
        return 'absent'
    value = item['is_completed']
    if type(value) is not bool:
        return 'type_invalid'
    return 'true' if value is True else 'false'


def _id_metadata_status(item):
    if 'msg_id' not in item:
        return 'absent'
    value = item['msg_id']
    if not isinstance(value, str):
        return 'type_invalid'
    return 'valid' if 0 < len(value) <= 256 else 'value_invalid'


def _json_type(value):
    # Inspect only the top-level Python type produced by JSON parsing.
    # Do not stringify, measure, hash or traverse any payload.
    kind = type(value)
    return ('null' if value is None else 'boolean' if kind is bool else
            'number' if kind in (int, float) else 'string' if kind is str else
            'array' if kind is list else 'object' if kind is dict else 'non_json')


def unknown_command_item_diagnostic(message, text, capture, decision):
    """Closed, bounded diagnostic for a newly admitted owner text command.

    The caller supplies the same primary decision saved with the source.
    This does not change that decision or authorize any action. It can be
    returned to the cloud by read_weixin_message; it is not local-only data.
    """
    if (type(decision) is not tuple or len(decision) != 2 or decision[0] is not False
            or decision[1] != 'item_unknown_fields'
            or not isinstance(text, str) or len(text) > 128
            or match_command(text) is None
            or not isinstance(capture, dict) or capture.get('complete') is not True
            or type(capture.get('source_item_count')) is not int or capture['source_item_count'] != 1):
        return None
    parts = capture.get('parts')
    if (not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict)
            or parts[0].get('kind') != 'text' or parts[0].get('state') != 'captured'
            or type(parts[0].get('item_type')) is not int or parts[0]['item_type'] != 1):
        return None
    if not isinstance(message, dict):
        return None
    items = message.get('item_list')
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return None
    item = items[0]
    extra = set(item) - _ALLOWED_COMMAND_ITEM_FIELDS
    if not extra:
        return None
    fields = []
    truncated = False
    # Order and returned codes derive from constants, never unknown raw keys.
    for key, code in OFFICIAL_PROTOCOL_FIELD_CODES:
        if key in extra:
            if len(fields) < 8:
                fields.append({'code': code, 'json_type': _json_type(item[key])})
            else:
                truncated = True
    return {
        'version': 'closed-command-item-v1', 'fields': fields,
        'unlisted': bool(extra - frozenset(OFFICIAL_PROTOCOL_FIELDS)),
        'truncated': truncated,
        'metadata': {
            'create_time_ms': _time_metadata_status(item, 'create_time_ms'),
            'update_time_ms': _time_metadata_status(item, 'update_time_ms'),
            'is_completed': _completion_metadata_status(item),
            'msg_id': _id_metadata_status(item),
        },
    }


def inspect_command_context(message):
    """One boolean decision plus a bounded, static, non-authoritative reason.

    Absence of a forwarding marker does not prove direct composition. Capture
    can enable a hint only; independent owner intent/account checks still apply.
    No raw keys, values, text, identifiers or exceptions enter the reason code.
    The first failed gate is reported; it is not a complete shape inventory.
    """
    if not isinstance(message, dict):
        return False, 'message_not_object'
    for key, codes in _REFERENCE_MARKER_REASONS.items():
        if key in message:
            # Only literal null/false versus other; never traverse or stringify
            # the marker value or expose an input-provided key.
            value = message[key]
            return False, codes[0 if value is None else 1 if value is False else 2]
    items = message.get('item_list')
    if not isinstance(items, list):
        return False, 'item_list_type_invalid'
    if len(items) != 1:
        return False, 'item_count_not_one'
    item = items[0]
    # Tencent MessageItem has these optional, non-authoritative metadata fields.
    # Match the bounded new-message check's conservative types/ranges. They do
    # not establish forwarding provenance, source identity or approval.
    if not isinstance(item, dict):
        return False, 'item_not_object'
    if not {'type', 'text_item'}.issubset(item):
        return False, 'item_required_fields_missing'
    if not set(item).issubset(_ALLOWED_COMMAND_ITEM_FIELDS):
        for key, code in _KNOWN_NONCOMMAND_ITEM_REASONS.items():
            if key in item:
                return False, code
        return False, 'item_unknown_fields'
    if type(item['type']) is not int:
        return False, 'item_content_type_invalid'
    if item['type'] != 1:
        return False, 'item_not_text'
    for key in ('create_time_ms', 'update_time_ms'):
        status = _time_metadata_status(item, key)
        if status == 'type_invalid':
            return False, 'item_time_type_invalid'
        if status == 'value_invalid':
            return False, 'item_time_value_invalid'
    completion = _completion_metadata_status(item)
    if completion == 'type_invalid':
        return False, 'item_completion_type_invalid'
    if completion == 'false':
        return False, 'item_not_completed'
    identity = _id_metadata_status(item)
    if identity == 'type_invalid':
        return False, 'item_id_type_invalid'
    if identity == 'value_invalid':
        return False, 'item_id_size_invalid'
    data = item['text_item']
    if not isinstance(data, dict):
        return False, 'text_item_not_object'
    if 'text' not in data:
        return False, 'text_item_required_field_missing'
    if set(data) != {'text'}:
        return False, 'text_item_unknown_fields'
    if not isinstance(data['text'], str):
        return False, 'text_type_invalid'
    if len(data['text']) > 128:
        return False, 'text_too_long'
    return True, 'standalone_text_shape'


def capture_command_context(message):
    """Legacy boolean API; derives from the same decision as future diagnostics."""
    return inspect_command_context(message)[0]

