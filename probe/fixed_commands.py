"""Read-only fixed-command hints; no official reader, sender or authorization."""
WEEKLY_REQUESTS = frozenset((
    '额度', '查额度', '查一下额度',
    '查一下我的周额度还剩多少', '查一下我的周额度', '查询我的周额度',
    '查看我的周额度', '我的周额度还剩多少', '我的周额度',
    '查一下codex周额度', '查询codex周额度', '查看codex周额度',
    'codex周额度还剩多少', '我的codex周额度还剩多少', '查一下我的codex周额度',
))
HELP_TEXT = '''🤖 微信助手 · 使用指南

💬 直接说需求
查资料、整理任务、分析内容。

🖼️ 图片 / 📄 文件 / 🎬 视频
发来时说一句：希望我做什么。
文件≤20 MiB。

↩️ 接着聊
引用内容识别不全时，请贴上关键文字。

⏳ 查看进度
直接问“进度怎么样”；没收到先查，不盲目重发。

🔒 仅本人私聊
不支持群聊。

📊 额度：查询当前 Codex 周额度
需主处理者具备本人账户的官方读取工具；当前未接通插件自动读取。

❔ 再看帮助：/help'''


def capture_command_context(message):
    """Boolean only; unknown item fields and explicit quoted/forwarded data fail closed.

    Protocol absence of a forward marker does not prove direct composition.
    This observation can enable a hint, never automatic execution.
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


def prepare_hint(text, kind, capture, attachments, reply_state):
    if (kind != 'text' or not isinstance(text, str) or len(text) > 128
            or not isinstance(capture, dict) or capture.get('complete') is not True
            or capture.get('command_context_v1') is not True
            or not isinstance(attachments, list) or attachments
            or not isinstance(reply_state, dict)):
        return None
    normalized = text.strip().casefold().rstrip('？?').strip()
    if normalized != '/help' and normalized not in WEEKLY_REQUESTS:
        return None
    for phase in ('ack', 'final'):
        slot = reply_state.get(phase)
        if not isinstance(slot, dict) or type(slot.get('committed')) is not bool:
            return None
        if slot['committed']:
            return {'state': 'already_committed', 'automatic_send': False,
                    'reply_via': 'existing_idempotent_final'}
    return {'state': 'candidate', 'kind': 'help' if normalized == '/help' else 'weekly_usage',
            'prepared_text': HELP_TEXT if normalized == '/help' else None,
            'official_reader_required': normalized != '/help',
            'automatic_send': False, 'owner_intent_verification_required': True,
            'forwarding_provenance': 'not_provided_by_protocol',
            'reply_via': 'existing_idempotent_final'}
