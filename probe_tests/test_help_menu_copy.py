"""Approved help copy and existing guards. Disposable DB; no events or sends."""
import json
import tempfile
import unittest
from unittest.mock import patch

from probe.probe import Probe
from probe.weixin import WeixinError


APPROVED = '''🤖 微信助手 · 使用指南

💬 直接说需求
查资料、整理任务、分析内容。

🖼️ 图片 / 📄 文件 / 🎬 视频
发来时说一句：希望我做什么。
文件≤20 MiB；超长图建议在dot上传。

↩️ 接着聊
可引用消息补充，识别不全时贴上关键文字。

⏳ 查看进度
直接问“进度怎么样”；没收到先查，不盲目重发。

🔒 仅本人私聊
不支持群聊；dot自动镜像目前关闭。

📊 额度：查询当前 Codex 周额度

❔ 再看帮助：/help'''


def denied(*args, **kwargs):
    raise AssertionError('help-copy tests prohibit network, event delivery and sending')


class NoSender:
    def __getattr__(self, name):
        return denied


class HelpMenuCopyTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.getaddrinfo', 'socket.create_connection',
                     'socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(name, side_effect=denied)
            guard.start(); self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.probe = Probe(self.temp.name, clock=lambda: 1800000000.,
                           post=denied, weixin_client=NoSender())
        self.addCleanup(self.probe.close)
        self.probe.weixin.bind_confirmed(dict(status='confirmed', ilink_bot_id='fixture-bot',
            ilink_user_id='fixture-owner', bot_token='synthetic-token'))
        self.index = 0

    def read(self, text):
        self.index += 1
        source = 'help-fixture-' + str(self.index)
        self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs': [dict(
            message_id=source, message_type=1, message_state=2, from_user_id='fixture-owner',
            to_user_id='fixture-bot', item_list=[{'type': 1, 'text_item': {'text': text}}])]})
        mid = self.probe.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()[0]
        result = self.probe.rpc(dict(jsonrpc='2.0', id=1, method='tools/call', params=dict(
            name='read_weixin_message', arguments=dict(message_id=mid))))
        self.assertNotIn('error', result)
        return result['result']['structuredContent']

    def test_real_rpc_menu_matches_approved_copy_and_never_sends(self):
        source = self.read('/help')
        menu = source['help_menu']
        self.assertEqual(menu['text'], APPROVED)
        self.assertEqual(menu['text'].count('额度：查询当前 Codex 周额度'), 1)
        self.assertNotIn('查额度', menu['text'])
        self.assertNotIn('/quota', menu['text'])
        self.assertEqual(source['source'], 'owner_weixin_dm')
        self.assertTrue(menu['matched'])
        self.assertEqual(menu['reply_via'], 'existing_idempotent_final')
        self.assertFalse(menu['automatic_send'])
        self.assertTrue(menu['owner_intent_verification_required'])
        self.assertFalse(source['reply_state']['final']['committed'])
        self.assertLessEqual(len(menu['text'].encode('utf-8')), 8192)
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM pings').fetchone()[0], 0)
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 0)

    def test_mirror_copy_remains_dynamic_without_changing_admission(self):
        for sample, expected in (({'any_new_admission_enabled': False}, 'dot自动镜像目前关闭。'),
                                 ({'any_new_admission_enabled': True}, 'dot镜像仅按已批准范围运行。')):
            with self.subTest(sample=sample), patch.object(self.probe.dot_journal.cutover, 'status', return_value=sample):
                self.assertIn(expected, self.read('/help')['help_menu']['text'])
        with patch.object(self.probe.dot_journal.cutover, 'status', side_effect=WeixinError('fixture_unavailable')):
            self.assertIn('dot自动镜像状态尚未核实。', self.read('/help')['help_menu']['text'])
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_edit_requests_and_other_commands_do_not_match_help(self):
        for text in ('/help这个菜单优化一下', '/help foo', '/quota', '/status'):
            with self.subTest(text=text):
                self.assertIsNone(self.read(text)['help_menu'])

    def test_quote_and_incomplete_capture_guards_are_preserved(self):
        row = dict(text='/help', kind='text')
        complete = dict(complete=True)
        quotes = dict(status='none', recorded=True, complete=True)
        for capture, quote in ((dict(complete=False), quotes),
                               (complete, dict(status='present', recorded=True, complete=True)),
                               (complete, dict(status='none', recorded=False, complete=True)),
                               (complete, dict(status='none', recorded=True, complete=False))):
            with self.subTest(capture=capture, quote=quote):
                self.assertIsNone(self.probe.weixin.help_menu(row, capture, quote))


if __name__ == '__main__':
    unittest.main()
