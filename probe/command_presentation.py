"""Pure display functions for fixed commands; no parsing, reads or sends."""
from datetime import datetime
from zoneinfo import ZoneInfo
try:
    from .command_catalog import HELP_COMMAND, WEEKLY_USAGE_COMMAND
except ImportError:
    from command_catalog import HELP_COMMAND, WEEKLY_USAGE_COMMAND


def render_help_menu(*, mirror):
    """Preserve the existing menu, with command lines from the same catalog."""
    return ('🤖 微信助手 · 使用指南\n\n'
            '💬 直接说需求\n'
            '查资料、整理任务、分析内容。\n\n'
            '🖼️ 图片 / 📄 文件 / 🎬 视频\n'
            '发来时说一句：希望我做什么。\n'
            '文件≤20 MiB；超长图建议在dot上传。\n\n'
            '↩️ 接着聊\n'
            '可引用消息补充，识别不全时贴上关键文字。\n\n'
            '⏳ 查看进度\n'
            '直接问“进度怎么样”；没收到先查，不盲目重发。\n\n'
            '🔒 仅本人私聊\n'
            '不支持群聊；' + mirror + '\n\n'
            + WEEKLY_USAGE_COMMAND.help_line + '\n\n' + HELP_COMMAND.help_line)


def _date(value):
    try:
        return datetime.fromtimestamp(value, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') + '（北京时间）'
    except (ValueError, TypeError, OverflowError, OSError):
        return '未知'


def render_weekly_usage(view):
    """Format an already validated view as the established four-line reply."""
    status = view.status
    weekly = [w for w in view.windows if w.minutes == 10080]
    window = weekly[0] if status == 'reported' and len(weekly) == 1 else None
    amounts = ('剩余' + format(window.remaining, '.10g') + '%，已用' + format(window.used, '.10g') + '%'
               if window and window.used is not None and window.remaining is not None else '剩余未知，已用未知')
    read_at = _date(view.read_at) if status in ('reported', 'no_windows') else '未知'
    reset = _date(window.resets_at) if window and window.resets_at is not None and window.state != 'reset_elapsed' else '未知'
    credits = view.credit_text if status in ('reported', 'no_windows') else '未知'
    reason = {
        'unavailable': '官方读取未接通，未调用',
        'binding_unverified': '账户未核验，未调用',
        'read_failed': '官方读取失败',
        'permission_denied': '官方读取被拒，已停止',
        'requires_async': '官方异步读取待核实，请由宿主等待原读取结果',
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
    return ('📊 Codex 周额度：' + amounts + '\n🕒 读取：' + read_at
            + '\n🔄 下次重置：' + reset + '\n💳 信用点余额：' + credits)
