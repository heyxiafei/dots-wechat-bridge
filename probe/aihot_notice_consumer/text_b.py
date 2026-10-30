"""Pure B-style source projection. No clock reads, models, I/O or send calls.

The existing monitor supplies the eligible view. Optional display fields fail
to explicit unknowns; they never enlarge source eligibility or stop email.
"""
import datetime as dt
import json
import math
import re
import unicodedata

STYLE = 'wechat-text-b-v1'
RICH_STYLE = 'wechat-email-rich-v2'
UNAVAILABLE_STYLE = 'rich-unavailable-v2'
MAX_TEXT_BYTES = 2048
BEIJING = dt.timezone(dt.timedelta(hours=8))
FIELDS = frozenset(('version', 'style', 'event_id', 'stage', 'source_at', 'link',
    'reference_date', 'original_text', 'translated_text', 'source_precision', 'time_inferred',
    'source_from', 'source_through', 'estimate_from', 'estimate_through', 'estimate_label',
    'scope_known', 'scope_label', 'display_label', 'confirmation_basis', 'payload_timezone'))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value < 4102444800


def _plain(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    if any(unicodedata.category(c).startswith('C') and c not in '\n\t' for c in value):
        return None
    if '<' in value or '>' in value:
        return None
    return value.strip()


def _stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        result = parsed.timestamp() if parsed.tzinfo is not None else None
        return result if _finite(result) else None
    except (ValueError, OverflowError, OSError):
        return None


def _window(value):
    if not isinstance(value, dict):
        return None, None
    start, end = _stamp(value.get('from')), _stamp(value.get('through'))
    if start is None or end is None or not start <= end <= start + 7 * 86400:
        return None, None
    return start, end


def _source_text(value, limit):
    # Preserve source-provided text verbatim; unsafe controls/oversize become unknown.
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    if any(unicodedata.category(c).startswith('C') and c not in '\n\t' for c in value):
        return None
    return value


def legacy_display(value):
    """Preserve existing v1 entries and their original immutable rendering."""
    if value is None:
        return None
    if value.get('style') != RICH_STYLE:
        return value
    old = dict(value, style=STYLE)
    for key in ('original_text', 'translated_text'):
        old[key] = _plain(old[key], 1000)
    validate_display(old)
    return old


def build_display(payload, raw, view, *, style=STYLE):
    """Project only the exact activity/post already bound by the old source gate."""
    stage, source_at = view['stage'], view['source_at']
    if (view.get('eligible') is not True or raw.get('status') != stage
            or stage not in ('announced', 'confirmed') or raw.get('id') is None):
        raise ValueError('ineligible_display')
    p = raw.get('presentation')
    if not isinstance(p, dict) or p.get('status') != stage:
        raise ValueError('display_stage_conflict')
    if stage == 'confirmed' and raw.get('confirmationBasis') != 'source_post':
        raise ValueError('confirmation_not_source_post')
    link = view['text'].splitlines()[-1]
    action = 'announce' if stage == 'announced' else 'confirm'
    matches = [a for a in payload.get('activities', []) if isinstance(a, dict)
               and raw['id'] in a.get('eventIds', []) and a.get('action') == action
               and _stamp(a.get('publishedAt')) == source_at and a.get('url') == link]
    # Multiple equivalent URLs were accepted by the original view. Ambiguous
    # prose is omitted, never selected from an unrelated first array element.
    source = matches[0] if len(matches) == 1 else {}
    post_stage = 'announcement' if stage == 'announced' else 'confirmation'
    posts = raw.get('posts', [])
    posts = [post for post in posts if isinstance(post, dict) and post.get('url') == link
             and _stamp(post.get('publishedAt')) == source_at and post.get('stage') == post_stage] if isinstance(posts, list) else []
    if len(posts) == 1:
        source = posts[0]
    clean = _source_text if style == RICH_STYLE else _plain
    original = clean(source.get('originalText'), 1000)
    translated = clean(source.get('text'), 1000)
    schedule, estimate = raw.get('schedule'), raw.get('estimate')
    source_from, source_through = _window(schedule)
    estimate_from, estimate_through = _window(estimate)
    precision = schedule.get('precision') if isinstance(schedule, dict) else None
    if precision not in ('exact', 'window', 'day', 'date', 'approximate', 'deadline'):
        precision = None
    scope_label = _plain(p.get('scopeLabel'), 160) if p.get('scopeKnown') is True else None
    checked = _stamp(payload.get('checkedAt'))
    display = dict(version=1, style=style, event_id=raw['id'], stage=stage,
        source_at=source_at, link=link,
        reference_date=dt.datetime.fromtimestamp(checked or source_at, BEIJING).date().isoformat(),
        original_text=original, translated_text=translated, source_precision=precision,
        time_inferred=p.get('timeInferred') if type(p.get('timeInferred')) is bool else None,
        source_from=source_from, source_through=source_through,
        estimate_from=estimate_from, estimate_through=estimate_through,
        estimate_label=_plain(estimate.get('label'), 160) if isinstance(estimate, dict) else None,
        scope_known=scope_label is not None, scope_label=scope_label,
        display_label=_plain(p.get('displayLabel'), 80),
        confirmation_basis='source_post' if stage == 'confirmed' else None,
        payload_timezone=payload.get('timezone') if payload.get('timezone') in ('Asia/Shanghai', 'UTC') else None)
    validate_display(display)
    return display


def validate_display(value, intent=None):
    if (not isinstance(value, dict) or set(value) != FIELDS or type(value['version']) is not int
            or value['version'] != 1 or value['style'] not in (STYLE, RICH_STYLE)):
        raise ValueError('display_shape')
    if (not isinstance(value['event_id'], str) or not re.fullmatch(r'reset-[0-9]{5,25}-[1-9][0-9]{0,2}-[1-9][0-9]{0,2}', value['event_id'])
            or value['stage'] not in ('announced', 'confirmed') or not _finite(value['source_at'])
            or not isinstance(value['link'], str) or not re.fullmatch(r'https://x\.com/thsottiaux/status/[0-9]{5,25}', value['link'])):
        raise ValueError('display_identity')
    if value['confirmation_basis'] != ('source_post' if value['stage'] == 'confirmed' else None):
        raise ValueError('display_confirmation')
    try:
        if not isinstance(value['reference_date'], str) or dt.date.fromisoformat(value['reference_date']).isoformat() != value['reference_date']:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('display_reference_date') from None
    for name, limit in [('original_text', 1000), ('translated_text', 1000), ('estimate_label', 160), ('scope_label', 160), ('display_label', 80)]:
        clean = _source_text if value['style'] == RICH_STYLE and name in ('original_text', 'translated_text') else _plain
        if value[name] is not None and clean(value[name], limit) != value[name]:
            raise ValueError('display_text_field')
    if type(value['scope_known']) is not bool or value['scope_known'] != (value['scope_label'] is not None):
        raise ValueError('display_scope')
    if value['time_inferred'] is not None and type(value['time_inferred']) is not bool:
        raise ValueError('display_time_flag')
    if value['source_precision'] not in ('exact', 'window', 'day', 'date', 'approximate', 'deadline', None) or value['payload_timezone'] not in ('Asia/Shanghai', 'UTC', None):
        raise ValueError('display_enum')
    for first, last in [('source_from', 'source_through'), ('estimate_from', 'estimate_through')]:
        if value[first] is None and value[last] is None:
            continue
        if not _finite(value[first]) or not _finite(value[last]) or not value[first] <= value[last] <= value[first] + 7 * 86400:
            raise ValueError('display_window')
    if intent is not None and any(value[name] != intent[name] for name in ('event_id', 'stage', 'source_at', 'link')):
        raise ValueError('display_intent_binding')
    return value


def _day_only(value):
    original = value['original_text'] or ''
    says_today = bool(re.search(r'\btoday\b|今天', original, re.I))
    has_time = bool(re.search(r'\d{1,2}[:：]\d{2}|\b(?:am|pm|noon|midnight|morning|afternoon|evening|lunch|utc|gmt)\b|上午|下午|中午|凌晨|晚上|\d\s*[时点]', original, re.I))
    return says_today and not has_time and value['time_inferred'] is True and value['source_precision'] in ('day', 'date', 'approximate')


def _day_prefix(epoch, value, today):
    day = dt.datetime.fromtimestamp(epoch, BEIJING)
    if today and day.date().isoformat() == value['reference_date']:
        return '今天 '
    return f'{day.month}月{day.day}日 '


def _window_label(first, last, value, today):
    start, end = (dt.datetime.fromtimestamp(value[n], BEIJING) for n in (first, last))
    begin = _day_prefix(value[first], value, today) + start.strftime('%H:%M')
    finish = end.strftime('%H:%M') if start.date() == end.date() else f'{end.month}月{end.day}日 ' + end.strftime('%H:%M')
    return begin + '–' + finish


def format_rich(value):
    validate_display(value)
    label = '重置预告' if value['stage'] == 'announced' else '重置确认'
    published = dt.datetime.fromtimestamp(value['source_at'], BEIJING).strftime('%Y-%m-%d %H:%M:%S')
    lines = ['🔔 Codex ' + label, '', '📜 来源原文', value['original_text'] or '未提供或未核验',
             '', '🌐 中文翻译', value['translated_text'] or '未提供或未核验',
             '', '🕒 发布时间：' + published + '（北京时间）', '👤 来源人物：Tibo',
             '', '🔗 原帖', value['link'], '',
             '这是来源公告，具体适用范围及你的额度是否到账，以账号实际显示为准。', '📡 来源：AI Hot']
    text = '\n'.join(lines)
    if len(text.encode('utf-8')) > MAX_TEXT_BYTES:
        raise ValueError('rich_text_too_large')
    return text


def format_b(value):
    validate_display(value)
    if value['style'] == RICH_STYLE:
        return format_rich(value)
    announced = value['stage'] == 'announced'
    source_day = dt.datetime.fromtimestamp(value['source_at'], BEIJING)
    today = _day_only(value) and source_day.date().isoformat() == value['reference_date']
    title = '📣 Codex ' + ('今天有重置预告' if today else '重置预告') if announced else '📣 Codex 重置确认（来源公告）'
    lines = [title, '']
    if announced and value['estimate_from'] is not None:
        lines += ['⏳ AI Hot 预计：' + _window_label('estimate_from', 'estimate_through', value, today), '🕒 北京时间']
    elif announced and value['estimate_label']:
        label = value['estimate_label']
        # Relative-only labels retain a date anchor; timezone is never guessed.
        if '今天' in label or re.search(r'\btoday\b', label, re.I):
            label = f'{source_day.month}月{source_day.day}日原标签：' + label
        lines += ['⏳ AI Hot 预计：' + label,
                  '🕒 北京时间' if value['payload_timezone'] == 'Asia/Shanghai' else '🕒 预计标签时区未说明']
    else:
        lines += [f'🕒 原帖发布：{source_day.month}月{source_day.day}日 ' + source_day.strftime('%H:%M') + '（北京时间）']
    # displayLabel is presentational data, never confirmation authority.
    safe_labels = ('待生效', '预告') if announced else ('来源已确认', '已确认')
    status = value['display_label'] if value['display_label'] in safe_labels else ('待生效' if announced else '来源已确认')
    lines += [('🟠' if announced else '🟢') + ' 当前状态：' + status, '']
    if _day_only(value) and announced:
        summary = 'Tibo 原帖只说“今天”，没有给具体时间'
    elif value['original_text']:
        quote = ' '.join(value['original_text'].split())
        quote = quote if len(quote) <= 180 else quote[:180] + '…（节选）'
        summary = 'Tibo 原帖：“' + quote + '”'
    elif value['translated_text']:
        quote = ' '.join(value['translated_text'].split())
        quote = quote if len(quote) <= 180 else quote[:180] + '…（节选）'
        summary = '原帖摘要：“' + quote + '”'
    else:
        summary = '原帖具体时间信息未能核验'
    summary += '，适用范围：' + value['scope_label'] + '。' if value['scope_known'] else '，适用范围也未明确。'
    lines += ['💬 ' + summary,
              '👀 预计窗口来自 AI Hot，是否恢复以你的账号额度为准。' if announced and (value['estimate_from'] is not None or value['estimate_label']) else
              '👀 是否恢复以你的账号额度为准；来源状态不代表个人额度到账。',
              '', '🔗 原帖', value['link'], '', '📡 来源：AI Hot']
    text = '\n'.join(lines)
    if len(text.encode('utf-8')) > MAX_TEXT_BYTES:
        raise ValueError('b_text_too_large')
    return text


def evidence_signature(value):
    """Used only to stop changed pending content, never to rekey an event."""
    validate_display(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
