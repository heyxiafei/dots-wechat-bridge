"""Reusable, side-effect-free Codex quota presentation for the owner handler.

No network, credentials, cache, sender, command dispatcher or runtime hook.
Binding and CreditInfo provenance are attestations supplied by a trusted
coordinator, not authentication performed by this module or user JSON.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import math
import re
import time
from typing import Any, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

MAX_REPLY_BYTES = 8192
MAX_RESULT_TEXT_BYTES = 65536
DEFAULT_MAX_AGE_SECONDS = 300  # Project policy, not an official cache TTL.


@dataclass(frozen=True)
class UsageBinding:
    owner_ref: str
    account_ref: str
    verified: bool = False


@dataclass(frozen=True)
class CreditInfo:
    """Explicit normalized mapping verified by the collector; no guessed keys."""
    balance: Any = None
    unlimited: Optional[bool] = None
    mapping_verified: bool = False
    scope: str = 'reported'


@dataclass(frozen=True)
class UsageObservation:
    owner_ref: str
    account_ref: str
    read_at: Any
    payload: Any
    source: str = 'get_usage_limits'
    credits: Optional[CreditInfo] = None


@dataclass(frozen=True)
class UsageWindow:
    slot: str
    minutes: Optional[int]
    used: Optional[float]
    remaining: Optional[float]
    resets_at: Optional[float]
    state: str


@dataclass(frozen=True)
class UsageView:
    status: str
    read_at: Optional[float] = None
    windows: Tuple[UsageWindow, ...] = ()
    credit_text: str = '未知（本次未提供已核实余额）'
    bucket: str = 'codex'
    source_view: str = 'none'
    issues: Tuple[str, ...] = ()


@dataclass(frozen=True)
class UsageReply:
    status: str
    text: str
    issues: Tuple[str, ...] = ()


def _number(value):
    if type(value) not in (int, float):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (OverflowError, ValueError):
        return None


def _time(value):
    value = _number(value)
    if value is None or value <= 0:
        return None
    try:
        datetime.fromtimestamp(value, ZoneInfo('Asia/Shanghai'))
        return value
    except (ValueError, OverflowError, OSError):
        return None


def _bound(binding):
    return (isinstance(binding, UsageBinding) and binding.verified is True
            and all(isinstance(x, str) and 0 < len(x) <= 256
                    for x in (binding.owner_ref, binding.account_ref)))


def _unwrap(raw):
    """Bounded official-result wrappers; structured content takes precedence."""
    for _ in range(4):
        if not isinstance(raw, Mapping) or raw.get('isError') is True or 'error' in raw:
            return None
        if 'structuredContent' in raw:
            raw = raw['structuredContent']
        elif 'result' in raw:
            raw = raw['result']
        elif 'rateLimitsByLimitId' in raw or 'rateLimits' in raw:
            return raw
        else:
            blocks = raw.get('content')
            if (not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], Mapping)
                    or blocks[0].get('type') != 'text' or not isinstance(blocks[0].get('text'), str)):
                return None
            text = blocks[0]['text']
            try:
                if len(text) > MAX_RESULT_TEXT_BYTES or len(text.encode('utf-8')) > MAX_RESULT_TEXT_BYTES:
                    return None
                raw = json.loads(text, parse_constant=lambda x: None)
            except (ValueError, TypeError, RecursionError, UnicodeError):
                return None
    return None


def _credit(info):
    # The documented result describes credits but does not establish all
    # subfields. The collector must explicitly attest its supported mapping.
    if not isinstance(info, CreditInfo) or info.mapping_verified is not True:
        return '未知（本次未提供已核实余额）'
    if info.scope not in ('reported', 'account', 'workspace'):
        return '未知（余额范围未核实）'
    suffix = '（工作区）' if info.scope == 'workspace' else ''
    if info.unlimited is True:
        return '不限额（接口标记）' + suffix
    value = info.balance
    if type(value) not in (str, int, float, Decimal):
        return '未知（本次未提供有效余额）'
    if type(value) is int and value.bit_length() > 64:
        return '未知（本次未提供有效余额）'
    try:
        text = str(value)
        if len(text) > 64:
            raise InvalidOperation()
        number = Decimal(text)
        if (not number.is_finite() or number < 0 or number > Decimal('1e18')
                or not -8 <= number.as_tuple().exponent <= 18):
            raise InvalidOperation()
        text = format(number, 'f')
        if '.' in text:
            text = text.rstrip('0').rstrip('.')
        return ('0' if number == 0 else text) + suffix
    except (ValueError, InvalidOperation, OverflowError):
        return '未知（本次未提供有效余额）'


def normalize_usage(observation, binding, *, now=None,
                    max_age_seconds=DEFAULT_MAX_AGE_SECONDS, limit_id='codex'):
    """Validate a fresh per-owner observation; never read an account itself."""
    if not _bound(binding):
        return UsageView('binding_unverified')
    if (not isinstance(observation, UsageObservation)
            or observation.owner_ref != binding.owner_ref or observation.account_ref != binding.account_ref):
        return UsageView('account_mismatch')
    if observation.source not in ('get_usage_limits', 'account/rateLimits/read'):
        return UsageView('source_unverified')
    if not isinstance(limit_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', limit_id):
        return UsageView('invalid_result')
    current, read_at, age_limit = _time(time.time() if now is None else now), _time(observation.read_at), _number(max_age_seconds)
    if current is None or read_at is None or age_limit is None or age_limit <= 0:
        return UsageView('timestamp_invalid')
    if read_at > current:
        return UsageView('timestamp_invalid')
    if current - read_at > age_limit:
        return UsageView('stale', read_at=read_at)
    raw = _unwrap(observation.payload)
    if raw is None:
        return UsageView('invalid_result')
    issues = []
    buckets = raw.get('rateLimitsByLimitId')
    if buckets is not None:
        if not isinstance(buckets, Mapping):
            return UsageView('invalid_result', read_at=read_at, issues=('bucket_map_invalid',))
        bucket = buckets.get(limit_id)
        source_view = 'rateLimitsByLimitId'
        if len(buckets) > 1:
            issues.append('other_buckets_not_combined')
    else:
        bucket = raw.get('rateLimits')
        source_view = 'rateLimits'
        issues.append('legacy_single_bucket')
    if not isinstance(bucket, Mapping):
        return UsageView('no_windows', read_at=read_at, bucket=limit_id,
                         source_view=source_view, issues=tuple(issues + ['requested_bucket_missing']))
    declared = bucket.get('limitId')
    if declared is not None and declared != limit_id:
        return UsageView('invalid_result', read_at=read_at, issues=('bucket_identity_mismatch',))
    if declared is None and source_view == 'rateLimits':
        issues.append('legacy_bucket_identity_unreported')
    windows = []
    for slot in ('primary', 'secondary'):
        value = bucket.get(slot)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            windows.append(UsageWindow(slot, None, None, None, None, 'invalid'))
            continue
        minutes = value.get('windowDurationMins')
        minutes = minutes if type(minutes) is int and 0 < minutes <= 2**31-1 else None
        used = _number(value.get('usedPercent'))
        if used is not None and not 0 <= used <= 1_000_000:
            used = None
        remaining = max(0., min(100., 100. - used)) if used is not None else None
        reset = _time(value.get('resetsAt'))
        state = 'reported' if used is not None else 'usage_unknown'
        if reset is not None and reset <= current:
            state, used, remaining = 'reset_elapsed', None, None
        windows.append(UsageWindow(slot, minutes, used, remaining, reset, state))
    windows.sort(key=lambda x: (x.minutes is None, x.minutes or 0, x.slot))
    if not windows:
        return UsageView('no_windows', read_at=read_at, credit_text=_credit(observation.credits),
                         bucket=limit_id, source_view=source_view, issues=tuple(issues))
    return UsageView('reported', read_at, tuple(windows), _credit(observation.credits),
                     limit_id, source_view, tuple(issues))


def _label(window):
    minutes = window.minutes
    if minutes == 10080:
        return '周额度'
    if minutes == 300:
        return '5小时额度'
    if minutes is None:
        return '未知时长窗口'
    if minutes % 1440 == 0:
        return str(minutes // 1440) + '天额度'
    if minutes % 60 == 0:
        return str(minutes // 60) + '小时额度'
    return str(minutes) + '分钟额度'


def _date(timestamp):
    return datetime.fromtimestamp(timestamp, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') + '（北京时间）'


def format_usage(view):
    """Return the unified Chinese text only; never send or commit a reply."""
    messages = {
        'binding_unverified': '⚠️ 尚未核验当前微信与额度账户的对应关系，当前额度未知。',
        'account_mismatch': '⚠️ 额度结果与当前本人账户不匹配，未展示该结果。',
        'source_unverified': '⚠️ 本次额度来源未核实，当前额度未知。',
        'timestamp_invalid': '⚠️ 读取时间异常，当前额度未知，请重新读取。',
        'invalid_result': '⚠️ 官方额度结果未能解析，当前额度未知。',
        'unavailable': '⚠️ 当前执行环境未接通官方额度读取，当前额度未知。',
        'read_failed': '⚠️ 官方额度读取未完成，当前额度未知。',
    }
    if (not isinstance(view, UsageView) or view.status not in (*messages, 'stale', 'reported', 'no_windows')
            or view.status in ('stale', 'reported', 'no_windows') and _time(view.read_at) is None):
        return messages['invalid_result']
    if view.status in messages:
        return messages[view.status]
    if view.status == 'stale':
        return '⚠️ 这份额度快照已过期，不能代表当前额度。\n🕒 原读取：' + _date(view.read_at) + '\n📊 当前额度：未知，请重新读取。'
    title = 'Codex' if view.bucket == 'codex' else 'Codex [' + view.bucket + ']'
    lines = []
    labels = [_label(w) for w in view.windows]
    labels = [label + '（' + w.slot + '）' if labels.count(label) > 1 else label
              for label, w in zip(labels, view.windows)]
    for w, label in zip(view.windows, labels):
        if w.used is None:
            explanation = '重置时间已过，需重新读取' if w.state == 'reset_elapsed' else '本次未返回有效用量'
            line = '未知（' + explanation + '）'
        else:
            line = '剩余' + format(w.remaining, '.10g') + '%，已用' + format(w.used, '.10g') + '%'
            if w.used > 100:
                line += '（接口报告超出100%）'
        lines.append('📊 ' + title + ' ' + label + '：' + line)
    if not view.windows:
        lines.append('📊 ' + title + ' 额度：未知（本次未返回可识别窗口）')
    lines.append('🕒 读取：' + _date(view.read_at))
    for w, label in zip(view.windows, labels):
        prefix = '下次重置' if len(view.windows) == 1 else label + '重置'
        if w.state == 'reset_elapsed':
            prefix = '原报告重置' if len(view.windows) == 1 else label + '原报告重置'
        lines.append('🔄 ' + prefix + '：' + (_date(w.resets_at) if w.resets_at is not None else '未知'))
    lines.append('💳 信用点余额：' + view.credit_text)
    present = {w.minutes for w in view.windows}
    for duration, label in ((300, '5小时窗口'), (10080, '周窗口')):
        if duration not in present:
            lines.append('⏳ ' + label + '：本次未返回，未知')
    result = '\n'.join(lines)
    return result if len(result.encode('utf-8')) <= MAX_REPLY_BYTES else '⚠️ 额度结果超出展示范围，当前额度未知。'


def render_usage(observation, binding, **options):
    view = normalize_usage(observation, binding, **options)
    return UsageReply(view.status, format_usage(view), view.issues)


def read_and_format_usage(reader, binding, **options):
    """Optional trusted collector seam; None stays explicitly unconnected.

    The project supplies no reader implementation. A reader runs exactly once,
    must use the requesting user's authorized official read context, and returns
    an observation whose account binding and read timestamp it has verified.
    """
    if not _bound(binding):
        return UsageReply('binding_unverified', format_usage(UsageView('binding_unverified')))
    if reader is None:
        return UsageReply('unavailable', format_usage(UsageView('unavailable')))
    try:
        observation = reader(binding)
    except Exception:
        return UsageReply('read_failed', format_usage(UsageView('read_failed')))
    return render_usage(observation, binding, **options)
