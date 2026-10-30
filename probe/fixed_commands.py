"""Conservative, side-effect-free recognition and quota text preparation.

The transport lacks forwarding provenance. These hints are never authorization
or an automatic sender. A trusted coordinator must verify owner intent and use
the existing final reply slot, and supply an authorized current-user reader.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
try:
    from .usage_display import UsageBinding, UsageView, normalize_usage
except ImportError:
    from usage_display import UsageBinding, UsageView, normalize_usage

# Exact standalone requests only. No substring or arbitrary natural-language match.
_WEEKLY = frozenset((
    '额度', '查额度', '查一下额度',
    '查一下我的周额度还剩多少', '查一下我的周额度', '查询我的周额度',
    '查看我的周额度', '我的周额度还剩多少', '我的周额度',
    '查一下codex周额度', '查询codex周额度', '查看codex周额度',
    'codex周额度还剩多少', '我的codex周额度还剩多少', '查一下我的codex周额度',
))


def capture_command_context(message):
    """Boolean shape evidence; explicit quotes/forwards and unknown fields fail closed.

    Absence of a forwarding marker does not prove direct composition. Capture
    can enable a hint only; independent owner intent/account checks still apply.
    """
    if not isinstance(message, dict):
        return False
    if any(key in message for key in ('ref_msg', 'quote', 'quoted_message', 'forwarded', 'forward_info')):
        return False
    items = message.get('item_list')
    if not isinstance(items, list) or len(items) != 1:
        return False
    item = items[0]
    if not isinstance(item, dict) or set(item) != {'type', 'text_item'} or type(item['type']) is not int or item['type'] != 1:
        return False
    data = item['text_item']
    return (isinstance(data, dict) and set(data) == {'text'}
            and isinstance(data['text'], str) and len(data['text']) <= 128)


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
    normalized = text.strip().casefold().rstrip('？?').strip()
    if normalized == '/help':
        if not isinstance(help_menu, dict) or not isinstance(help_menu.get('text'), str):
            return None
        kind_name, prepared = 'help', help_menu['text']
    elif normalized in _WEEKLY:
        kind_name, prepared = 'weekly_usage', None
    else:
        return None
    for phase in ('ack', 'final'):
        slot = reply_state.get(phase)
        if not isinstance(slot, dict) or type(slot.get('committed')) is not bool:
            return None
        if slot['committed']:
            return {'state': 'already_committed', 'automatic_send': False,
                    'reply_via': 'existing_idempotent_final'}
    return {'state': 'candidate', 'kind': kind_name, 'prepared_text': prepared,
            'official_reader_required': kind_name == 'weekly_usage',
            'automatic_send': False, 'owner_intent_verification_required': True,
            'forwarding_provenance': 'not_provided_by_protocol',
            'reply_via': 'existing_idempotent_final'}


def _date(value):
    try:
        return datetime.fromtimestamp(value, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') + '（北京时间）'
    except (ValueError, TypeError, OverflowError, OSError):
        return '未知'


def prepare_weekly_usage(reader, binding, *, now):
    """Use an injected, authorized official reader once; never call or send itself.

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
    weekly = [w for w in view.windows if w.minutes == 10080]
    window = weekly[0] if status == 'reported' and len(weekly) == 1 else None
    amounts = ('剩余' + format(window.remaining, '.10g') + '%，已用' + format(window.used, '.10g') + '%'
               if window and window.used is not None and window.remaining is not None else '剩余未知，已用未知')
    read_at = _date(view.read_at) if status in ('reported', 'no_windows') else '未知'
    reset = _date(window.resets_at) if window and window.resets_at is not None and window.state != 'reset_elapsed' else '未知'
    credits = view.credit_text if status in ('reported','no_windows') else '未知'
    reason = {
        'unavailable': '官方读取未接通，未调用',
        'binding_unverified': '账户未核验，未调用',
        'read_failed': '官方读取失败',
        'account_mismatch': '账户不匹配',
        'source_unverified': '来源未核验',
        'stale': '结果已过期',
        'timestamp_invalid': '读取时间无效',
        'invalid_result': '官方结果未能核实',
        'no_windows': '本次未返回周窗口',
    }.get(status, '')
    if status == 'reported' and window is None:
        reason = '本次未返回唯一周窗口'
    if reason:
        amounts += '（' + reason + '）'
    text = ('📊 Codex 周额度：' + amounts + '\n🕒 读取：' + read_at
            + '\n🔄 下次重置：' + reset + '\n💳 信用点余额：' + credits)
    return {'status':status, 'text':text, 'official_call_attempted': attempted}
