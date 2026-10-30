"""Bounded, non-recursive capture of untrusted upstream quote metadata.

This module neither resolves message identity nor reads or sends messages.
Only explicitly listed fields survive; media URLs, keys and sender fields do not.
"""
import json
import re


QUOTE_VERSION = 'weixin-quotes-v1'
MAX_ITEMS = 64
MAX_QUOTES = 8
MAX_OUTPUT_BYTES = 32 * 1024
MAX_SELECTION_INDEX = (1 << 31) - 1
MAX_SERVER_MESSAGE_ID = (1 << 64) - 1
_KINDS = {1: 'text', 2: 'image', 3: 'voice', 4: 'file', 5: 'video'}


def server_message_id(value):
    """Return a positive uint64 as ASCII decimal, or None.

    Decimal strings have at most 20 characters; leading zeroes within that
    bound are canonicalized. Whitespace, signs and non-ASCII digits are invalid.
    """
    if type(value) is int:
        return str(value) if 0 < value <= MAX_SERVER_MESSAGE_ID else None
    if (not isinstance(value, str) or not 1 <= len(value) <= 20
            or re.fullmatch(r'[0-9]+', value) is None):
        return None
    number = int(value)
    return str(number) if 0 < number <= MAX_SERVER_MESSAGE_ID else None


def _issue(issues, code):
    if code not in issues:
        issues.append(code)


def _text(value, limit, issues, field):
    """Read only a bounded prefix; replace invalid Unicode and never split UTF-8."""
    if not isinstance(value, str):
        _issue(issues, 'invalid_' + field)
        return None
    kept, byte_count, consumed = [], 0, 0
    for character in value[:limit + 1]:
        if 0xD800 <= ord(character) <= 0xDFFF:
            character = '\ufffd'
            _issue(issues, 'invalid_' + field + '_unicode')
        size = len(character.encode('utf-8'))
        if byte_count + size > limit:
            break
        kept.append(character)
        byte_count += size
        consumed += 1
    if consumed < len(value):
        _issue(issues, field + '_truncated')
    return ''.join(kept)


def _nested(value, issues):
    # Checking membership does not inspect the nested value, even for cycles.
    if isinstance(value, dict) and 'ref_msg' in value:
        _issue(issues, 'nested_reference_omitted')


def _content(reference, issues):
    item = reference.get('message_item')
    result = {'kind': 'unknown', 'type': None}
    if item is None:
        _issue(issues, 'missing_quoted_content')
        return result
    if not isinstance(item, dict):
        _issue(issues, 'invalid_quoted_content')
        return result
    _nested(item, issues)
    value = item.get('type')
    if type(value) is not int:
        _issue(issues, 'invalid_quoted_type')
        return result
    # Never serialize an arbitrarily large integer supplied by the caller.
    if -(1 << 31) <= value <= MAX_SELECTION_INDEX:
        result['type'] = value
    if value not in _KINDS:
        _issue(issues, 'unsupported_quoted_type')
        return result
    kind = _KINDS[value]
    result['kind'] = kind
    data = item.get(kind + '_item')
    if not isinstance(data, dict):
        _issue(issues, 'invalid_quoted_' + kind + '_item')
        _issue(issues, 'missing_quoted_content')
        return result
    _nested(data, issues)
    if kind == 'text':
        result['text'] = _text(data.get('text'), 2048, issues, 'quoted_text')
        if not result['text']:
            _issue(issues, 'missing_quoted_content')
    elif kind == 'voice' and 'text' in data:
        result['text'] = _text(data['text'], 2048, issues, 'quoted_text')
    elif kind == 'file':
        result['file_name'] = _text(data.get('file_name'), 256, issues, 'quoted_file_name')
        if not result['file_name']:
            _issue(issues, 'missing_quoted_content')
    return result


def _selection(reference, issues):
    if 'partial_text' not in reference:
        return None
    raw = reference['partial_text']
    result = dict.fromkeys(('start', 'end', 'startindex', 'endindex', 'quotemd5'))
    if not isinstance(raw, dict):
        _issue(issues, 'invalid_partial_text')
        return result
    _nested(raw, issues)
    for key in ('start', 'end'):
        result[key] = _text(raw.get(key), 128, issues, 'partial_text_' + key)
    for key in ('startindex', 'endindex'):
        value = raw.get(key)
        if type(value) is int and 0 <= value <= MAX_SELECTION_INDEX:
            result[key] = value
        else:
            _issue(issues, 'invalid_partial_text_' + key)
    if (result['startindex'] is not None and result['endindex'] is not None
            and result['startindex'] > result['endindex']):
        _issue(issues, 'invalid_partial_text_range')
    digest = raw.get('quotemd5')
    if isinstance(digest, str) and len(digest) == 32 and re.fullmatch(r'[a-fA-F0-9]{32}', digest):
        result['quotemd5'] = digest.lower()
    else:
        _issue(issues, 'invalid_partial_text_quotemd5')
    return result


def _reference_id(reference, issues):
    """Read the two protocol ID locations without guessing or resolving conflicts.

    The official client also supports message_item.msg_id as a legacy fallback.
    An invalid supplied value or two differing IDs must never select a target.
    """
    item = reference.get('message_item')
    raw_primary = reference.get('svr_id')
    raw_nested = item.get('msg_id') if isinstance(item, dict) else None
    primary = server_message_id(raw_primary)
    nested = server_message_id(raw_nested)
    invalid = False
    for raw, parsed, issue in (
            (raw_primary, primary, 'invalid_server_message_id'),
            (raw_nested, nested, 'invalid_nested_server_message_id')):
        if raw is not None and parsed is None:
            _issue(issues, issue)
            invalid = True
    if invalid:
        return None, None
    if primary is not None and nested is not None and primary != nested:
        _issue(issues, 'conflicting_server_message_ids')
        return None, None
    if primary is not None:
        return primary, ('ref_msg.svr_id+message_item.msg_id' if nested is not None
                         else 'ref_msg.svr_id')
    if nested is not None:
        return nested, 'ref_msg.message_item.msg_id'
    _issue(issues, 'missing_server_message_id')
    return None, None


def _quote(reference, index):
    issues = []
    result = {'item_index': index, 'server_message_id': None,
              'server_message_id_source': None, 'title': None,
              'content': {'kind': 'unknown', 'type': None}, 'partial_text': None,
              'provenance': 'upstream_ref_msg', 'untrusted': True,
              'complete': False, 'issue_codes': issues}
    if not isinstance(reference, dict):
        issues.append('invalid_ref_msg')
        return result
    _nested(reference, issues)
    result['server_message_id'], result['server_message_id_source'] = _reference_id(reference, issues)
    if 'title' in reference:
        result['title'] = _text(reference['title'], 512, issues, 'quote_title')
    result['content'] = _content(reference, issues)
    result['partial_text'] = _selection(reference, issues)
    result['complete'] = not issues
    return result


def capture_quotes(raw_items):
    """Capture at most eight top-level refs from at most 64 list items.

    The result fits 32 KiB using Python's default JSON serialization, including
    ASCII escaping. If necessary, trailing summaries are omitted explicitly.
    No raw container is returned or recursively visited.
    """
    result = {'version': QUOTE_VERSION, 'recorded': True, 'status': 'none',
              'complete': True, 'quotes': [], 'issues': [], 'untrusted': True}
    issues = set()
    if not isinstance(raw_items, list):
        issues.add('invalid_item_list')
    else:
        if len(raw_items) > MAX_ITEMS:
            issues.add('item_scan_limit_exceeded')
        for index, item in enumerate(raw_items[:MAX_ITEMS]):
            if not isinstance(item, dict):
                issues.add('invalid_item')
                continue
            if 'ref_msg' not in item:
                continue
            result['status'] = 'present'
            if len(result['quotes']) >= MAX_QUOTES:
                issues.add('quote_limit_exceeded')
                continue
            quote = _quote(item['ref_msg'], index)
            result['quotes'].append(quote)
            issues.update(quote['issue_codes'])
    result['issues'] = sorted(issues)
    result['complete'] = not issues
    # Default ASCII escaping is at least as large as UTF-8 JSON for every
    # retained string. Account for standard separator whitespace as well.
    while len(json.dumps(result, allow_nan=False).encode('ascii')) > MAX_OUTPUT_BYTES:
        issues.add('output_budget_exceeded')
        result['issues'] = sorted(issues)
        result['complete'] = False
        result['quotes'].pop()
    return result


QUOTE_SHAPE_MAX_BYTES = 4096
QUOTE_SHAPE_MAX_NODES = 32
QUOTE_SHAPE_MAX_DEPTH = 4


def quote_shape(reference):
    """Return value-free structural facts for a fixed quote field whitelist.

    ``nodes`` is flat. Its trusted paths identify present source fields; absent
    paths mean absent fields unless ``truncated`` is true. The root path is
    ``$`` and source depth starts at one. Each inspected source value counts as
    one node. String lengths count Unicode characters, object/array lengths
    count entries; numbers, booleans and null have no length or recorded value.

    Only exact built-in JSON types are inspected. Unknown keys are never
    enumerated, media contents are never traversed, and no repr or exception
    text enters the result. The result is bounded under default ASCII JSON.
    """
    item_fields = (
        ('type', None), ('msg_id', None), ('text_item', (('text', None),)),
        ('image_item', (('media', None),)),
        ('voice_item', (('text', None), ('media', None))),
        ('file_item', (('file_name', None), ('media', None))),
        ('video_item', (('media', None),)),
    )
    fields = (('svr_id', None), ('title', None), ('message_item', item_fields),
              ('partial_text', tuple((key, None) for key in
                                     ('start', 'end', 'startindex', 'endindex', 'quotemd5'))))
    result = {'version': 'quote-shape-v1', 'scalar_values_recorded': False,
              'omitted_fields_mean': 'absent_unless_truncated',
              'truncated': False, 'nodes': [], 'issues': []}

    def omitted(reason):
        result['truncated'] = True
        if reason not in result['issues']:
            result['issues'].append(reason)

    # This stack follows the constant schema, never the shape of arbitrary data.
    pending = [('$', reference, fields, 1, frozenset())]
    while pending:
        if len(result['nodes']) >= QUOTE_SHAPE_MAX_NODES:
            omitted('node_limit_exceeded')
            break
        path, value, allowed, depth, ancestors = pending.pop()
        if depth > QUOTE_SHAPE_MAX_DEPTH:
            omitted('depth_limit_exceeded')
            continue
        value_type = type(value)
        kinds = ((dict, 'object'), (list, 'array'), (str, 'string'), (int, 'integer'),
                 (float, 'number'), (bool, 'boolean'), (type(None), 'null'))
        kind = next((label for known, label in kinds if value_type is known), 'unsupported')
        node = {'path': path, 'type': kind}
        result['nodes'].append(node)
        if value_type is dict or value_type is list or value_type is str:
            node['length'] = len(value)
        if value_type is not dict or allowed is None:
            continue
        if id(value) in ancestors:
            omitted('cycle_omitted')
            continue
        try:
            # Only these constant keys are touched. Unknown dict key names and
            # their values are never copied, formatted, sorted or iterated.
            present = [(key, child_fields) for key, child_fields in allowed if key in value]
            node['unknown_field_count'] = len(value) - len(present)
            next_ancestors = ancestors | {id(value)}
            children = [(key if path == '$' else path + '.' + key,
                         value[key], child_fields, depth + 1, next_ancestors)
                        for key, child_fields in present]
            pending.extend(reversed(children))
        except Exception:
            # Malformed Python fixtures may have hostile non-JSON dictionary
            # keys. A failed lookup is represented without their repr or error.
            omitted('inspection_failed')

    while len(json.dumps(result, allow_nan=False).encode('ascii')) > QUOTE_SHAPE_MAX_BYTES:
        omitted('output_budget_exceeded')
        if not result['nodes']:
            break  # The fixed header alone is well below the 4096-byte limit.
        result['nodes'].pop()
    return result
