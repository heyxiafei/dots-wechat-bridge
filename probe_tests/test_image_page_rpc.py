"""Offline page RPC contract; synthetic cache, no actual events or sending."""
import base64
import hashlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from probe.probe import Probe, MAX_ATTACHMENT_RPC_BYTES, canonical


def no_action(*args, **kwargs):
    raise AssertionError('offline image read test forbids external action')


class NoClient:
    def __getattr__(self, name):
        return no_action


class ImagePageRpcTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.getaddrinfo', 'socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(name, side_effect=no_action)
            guard.start(); self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.probe = Probe(self.temp.name, clock=lambda: 1800000000., post=no_action, weixin_client=NoClient())
        self.addCleanup(self.probe.close)
        self.probe.weixin.bind_confirmed(dict(status='confirmed', ilink_bot_id='fixture-bot',
            ilink_user_id='fixture-owner', bot_token='synthetic-only'))
        out = io.BytesIO()
        with Image.new('RGB', (100, 3000), (25, 90, 140)) as image:
            image.save(out, 'PNG')
        self.raw = out.getvalue()
        self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs': [dict(
            message_id='fixture-page-rpc', message_type=1, from_user_id='fixture-owner',
            to_user_id='fixture-bot', item_list=[{'type': 2, 'image_item': {'media': {
                'full_url': 'https://novac2c.cdn.weixin.qq.com/fixture-not-requested'}}}])]})
        self.mid = self.probe.db.execute('SELECT id FROM weixin_inbox').fetchone()[0]
        self.probe.media.download = lambda url: self.raw
        self.probe.media.process_once()
        self.aid = self.probe.db.execute('SELECT id FROM weixin_attachments').fetchone()[0]

    def read(self, **extra):
        return self.probe.rpc(dict(jsonrpc='2.0', id=3, method='tools/call', params=dict(
            name='read_attachment_image', arguments=dict(message_id=self.mid, attachment_id=self.aid, **extra))))

    def test_schema_keeps_existing_required_arguments_and_adds_optional_bounded_page(self):
        schema = next(t['inputSchema'] for t in self.probe.tools() if t['name'] == 'read_attachment_image')
        self.assertEqual(schema['required'], ['message_id', 'attachment_id'])
        self.assertEqual(schema['properties']['page'], dict(type='integer', minimum=0, maximum=63))
        self.assertFalse(schema['additionalProperties'])

    def test_compatible_overview_and_explicit_page_are_one_image_each_without_writes(self):
        tables = ('weixin_inbox', 'weixin_attachments', 'weixin_outbox', 'subscriptions', 'pings')
        snapshot = lambda: {n:[tuple(r) for r in self.probe.db.execute('SELECT * FROM ' + n + ' ORDER BY rowid')] for n in tables}
        before = snapshot()
        for arguments, expected in (({}, 'overview'), ({'page': 0}, 'region'), ({'page': 1}, 'region')):
            with self.subTest(arguments=arguments):
                result = self.read(**arguments)['result']
                self.assertEqual(len(result['content']), 2)
                self.assertEqual(result['content'][1]['type'], 'image')
                self.assertEqual(result['structuredContent']['preview_mode'], expected)
                self.assertEqual(result['structuredContent']['original_sha256'], hashlib.sha256(self.raw).hexdigest())
                self.assertNotIn('data_base64', result['structuredContent'])
                data = base64.b64decode(result['content'][1]['data'])
                with Image.open(io.BytesIO(data)) as image:
                    self.assertLessEqual(max(image.size), 2048)
                self.assertLessEqual(len(canonical(result).encode()), MAX_ATTACHMENT_RPC_BYTES)
        self.assertEqual(before, snapshot())

    def test_invalid_page_values_and_extra_fields_fail_without_image(self):
        for value in (None, True, False, -1, 64, '0', 0.):
            with self.subTest(value=value):
                result = self.read(page=value)
                self.assertIn('error', result)
                self.assertNotIn('result', result)
        self.assertIn('error', self.read(page=0, extra='not_allowed'))

    def test_oversized_image_rpc_returns_bounded_error_without_sending(self):
        block = dict(type='image', mimeType='image/jpeg', data='A' * MAX_ATTACHMENT_RPC_BYTES)
        with patch.object(self.probe.media, 'read_image', return_value=({}, block)):
            result = self.read(page=0)
        self.assertEqual(result['error']['data']['reason'], 'attachment_response_too_large')
        self.assertLessEqual(len(canonical(result).encode()), MAX_ATTACHMENT_RPC_BYTES)
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
