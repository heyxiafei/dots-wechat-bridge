"""The fixed command vocabulary, independent of transport and authorization.

Adding a name here changes only text recognition. A command still needs a
complete owner message, independently verified intent and the original reply
slot. This module cannot read account data or submit a reply.
"""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    aliases: frozenset
    preparation: str
    help_line: str

    @property
    def official_reader_required(self):
        return self.preparation == 'weekly_usage'


HELP_COMMAND = CommandDefinition(
    'help', frozenset(('/help',)), 'help_menu', '❔ 再看帮助：/help')
WEEKLY_USAGE_COMMAND = CommandDefinition(
    'weekly_usage', frozenset((
        '额度', '查额度', '查一下额度',
        '查一下我的周额度还剩多少', '查一下我的周额度', '查询我的周额度',
        '查看我的周额度', '我的周额度还剩多少', '我的周额度',
        '查一下codex周额度', '查询codex周额度', '查看codex周额度',
        'codex周额度还剩多少', '我的codex周额度还剩多少', '查一下我的codex周额度',
    )), 'weekly_usage', '📊 额度：查询当前 Codex 周额度')

COMMANDS = (HELP_COMMAND, WEEKLY_USAGE_COMMAND)
COMMANDS_BY_NAME = MappingProxyType({command.name: command for command in COMMANDS})
_COMMANDS_BY_ALIAS = MappingProxyType({
    alias: command for command in COMMANDS for alias in command.aliases})


def normalize_command_text(text):
    """Normalize existing standalone aliases; never perform substring matching."""
    if not isinstance(text, str) or len(text) > 128:
        return None
    return text.strip().casefold().rstrip('？?').strip()


def match_command(text):
    return _COMMANDS_BY_ALIAS.get(normalize_command_text(text))


def is_help_request(text):
    return match_command(text) is HELP_COMMAND
