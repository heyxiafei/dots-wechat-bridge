"""Bounded source inventories and crash windows; synthetic local data only."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from probe.probe import Probe
from probe.weixin import (MESSAGE_EVENT, WeixinError, describe_inbound, encoded,
                          MAX_INBOUND_TEXT_BYTES, MAX_INBOUND_MANIFEST_ITEMS)


class NoNetworkClient:
    def send(self, *args, **kwargs):
        raise AssertionError('No real or fake outbound sends in inbound tests')


class InboundCompletenessTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.getaddrinfo', 'socket.create_connection',
                       'socket.socket.connect', 'socket.socket.connect_ex'):
            blocker = patch(target, side_effect=AssertionError('network forbidden'))
            blocker.start()
            self.addCleanup(blocker.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.p = Probe(self.tmp.name, clock=lambda: self.now, weixin_client=NoNetworkClient())
        self.addCleanup(lambda: self.p.close())
        self.p.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'test-bot',
            'ilink_user_id': 'test-owner', 'bot_token': 'TOKEN_CANARY'})

    def message(self, source='source', items=None, **updates):
        msg = {'message_id': source, 'message_type': 1, 'from_user_id': 'test-owner',
               'to_user_id': 'test-bot', 'context_token': 'CONTEXT_CANARY',
               'item_list': [{'type': 1, 'text_item': {'text': 'fixture'}}] if items is None else items}
        msg.update(updates)
        return msg

    def ingest(self, msg, cursor='new-cursor'):
        return self.p.weixin.ingest(self.p.weixin.account(), {'msgs': [msg], 'get_updates_buf': cursor})

    def mid(self, source='source'):
        return self.p.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()[0]

    def count(self, table):
        return self.p.db.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def manifest(self, source='source'):
        return self.p.weixin.inbound_completeness(self.mid(source))

    def subscribe_locally(self):
        with self.p.db:
            self.p.db.execute('''INSERT INTO subscriptions
                (id,url,secret,rotate_until,expires,verified_at,name)
                VALUES('sub_fixture','https://fixture.invalid/callback','SYNTHETIC',0,?,?,?)''',
                (self.now + 100, self.now, MESSAGE_EVENT))

    def test_unknown_only_input_is_persisted_and_emits_one_local_event(self):
        self.subscribe_locally()
        self.assertEqual(self.ingest(self.message(items=[{'type': 911, 'body': 'UNTRUSTED_BODY'}])), 1)
        self.assertEqual(self.p.weixin.account()['cursor'], 'new-cursor')
        manifest = self.manifest()
        self.assertFalse(manifest['complete'])
        self.assertEqual(manifest['parts'][0]['item_type'], 911)
        self.assertEqual(manifest['parts'][0]['state'], 'unsupported')
        self.assertEqual(self.p.queue_weixin_messages(), 1)
        self.assertEqual(self.p.queue_weixin_messages(), 0)
        self.assertEqual(self.count('pings'), 1)
        self.assertEqual(self.count('weixin_outbox'), 0)
        self.assertNotIn('UNTRUSTED_BODY', encoded(manifest))

    def test_empty_invalid_and_malformed_items_keep_source_and_reason(self):
        inputs = [([], 'empty_item_list'), ({'url': 'UNTRUSTED'}, 'invalid_item_list'),
                  ([None], 'invalid_item'), ([{'type': 1}], 'invalid_text_item')]
        for i, (items, reason) in enumerate(inputs):
            with self.subTest(reason=reason):
                source = 'malformed-' + str(i)
                self.assertEqual(self.ingest(self.message(source, item_list=items)), 1)
                manifest = self.manifest(source)
                self.assertFalse(manifest['complete'])
                self.assertIn(reason, manifest['issue_codes'])
                self.assertTrue(manifest['parts'])
        self.assertEqual(self.count('weixin_inbound_manifests'), len(inputs))

    def test_mixed_unknown_preserves_normal_text_and_media_independently(self):
        self.ingest(self.message(items=[{'type': 1, 'text_item': {'text': 'retained text'}},
            {'type': 911, 'url': 'https://UNTRUSTED.invalid/private'},
            {'type': 4, 'file_item': {'file_name': 'same.txt'}}]))
        result = self.p.weixin.read_message(self.mid())
        self.assertEqual(result['text'], 'retained text')
        self.assertEqual(len(result['attachments']), 1)
        self.assertEqual(result['attachments'][0]['status'], 'error')
        manifest = result['inbound_completeness']
        self.assertEqual([p['kind'] for p in manifest['parts']], ['text', 'unknown', 'file'])
        self.assertNotIn('UNTRUSTED', encoded(manifest))
        for private in ('TOKEN_CANARY', 'CONTEXT_CANARY', 'retained text', 'test-owner'):
            self.assertNotIn(private, encoded(manifest))

    def test_eight_nine_and_many_same_named_attachments_keep_count_and_identity(self):
        for count in (8, 9, 90):
            with self.subTest(count=count):
                source = 'files-' + str(count)
                self.ingest(self.message(source, items=[{'type': 4, 'file_item': {'file_name': 'same.txt'}}] * count))
                manifest = self.manifest(source)
                rows = self.p.media.list_for_message(self.mid(source))
                self.assertEqual(len(rows), 8)
                self.assertEqual(len({r['attachment_id'] for r in rows}), 8)
                self.assertEqual(manifest['attachment_source_count'], count)
                self.assertEqual({p['attachment_id'] for p in manifest['parts'] if p.get('attachment_id')},
                                 {r['attachment_id'] for r in rows})
                self.assertEqual(manifest['complete'], count == 8)
                if count > 8:
                    self.assertIn('attachment_limit_exceeded', manifest['issue_codes'])
                    self.assertEqual(manifest['parts'][8]['state'], 'omitted')
                self.assertLessEqual(len(manifest['parts']), MAX_INBOUND_MANIFEST_ITEMS + 1)

    def test_large_item_inventory_has_exact_bounded_overflow_range(self):
        items = [{'type': 987654321}] * 10000
        self.ingest(self.message(items=items))
        manifest = self.manifest()
        self.assertEqual(len(manifest['parts']), 65)
        self.assertEqual(manifest['source_item_count'], 10000)
        self.assertEqual(manifest['items_omitted_count'], 9936)
        last = manifest['parts'][-1]
        self.assertEqual((last['item_index_start'], last['item_index_end'], last['item_count']), (64, 9999, 9936))
        self.assertLess(len(encoded(manifest).encode()), 22000)
        self.assertFalse(manifest['complete'])

    def test_text_limit_counts_utf8_bytes_without_splitting_characters(self):
        samples = [('minus', 'a' * 65535, 65535, False),
                   ('exact', 'a' * 65536, 65536, False),
                   ('plus', 'a' * 65537, 65536, True),
                   ('chinese-exact', 'a' + '你' * 21845, 65536, False),
                   ('chinese-over', 'aa' + '你' * 21845, 65534, True),
                   ('emoji-over', 'a' * 65535 + '😀', 65535, True)]
        for source, value, stored, truncated in samples:
            with self.subTest(source=source):
                self.ingest(self.message(source, items=[{'type': 1, 'text_item': {'text': value}}]))
                read = self.p.weixin.read_message(self.mid(source))
                manifest = read['inbound_completeness']
                self.assertEqual(len(read['text'].encode()), stored)
                self.assertTrue(value.startswith(read['text']))
                self.assertNotIn('\ufffd', read['text'])
                self.assertEqual(manifest['text_source_bytes'], len(value.encode()))
                self.assertEqual(manifest['text_stored_bytes'], stored)
                self.assertEqual(manifest['text_truncated'], truncated)
                self.assertEqual(manifest['complete'], not truncated)

    def test_later_text_does_not_fill_gap_after_split_utf8_prefix(self):
        value, _, manifest = describe_inbound('wx_fixture', [
            {'type': 1, 'text_item': {'text': 'a' * 65535 + '你'}},
            {'type': 1, 'text_item': {'text': 'b'}}])
        self.assertEqual(value, 'a' * 65535)
        self.assertEqual(manifest['text_source_bytes'], 65540)
        self.assertTrue(manifest['text_truncated'])
        self.assertEqual(manifest['parts'][1]['state'], 'truncated')

    def test_missing_separator_at_limit_is_attributed_to_later_empty_part(self):
        value, _, manifest = describe_inbound('wx_fixture', [
            {'type': 1, 'text_item': {'text': 'a' * 65536}},
            {'type': 1, 'text_item': {'text': ''}}])
        self.assertEqual(len(value.encode()), 65536)
        self.assertFalse(manifest['complete'])
        self.assertEqual(manifest['text_source_bytes'], 65537)
        self.assertEqual(manifest['parts'][1]['state'], 'truncated')

    def test_duplicate_keeps_first_source_inventory_and_no_extra_parts(self):
        msg = self.message(items=[{'type': 911}, {'type': 4, 'file_item': {'file_name': 'same.txt'}}])
        self.assertEqual(self.ingest(msg), 1)
        original = self.manifest()
        self.assertEqual(self.ingest(msg), 0)
        self.assertEqual(self.ingest(self.message(items=[{'type': 999}])), 0)
        self.assertEqual(self.manifest(), original)
        self.assertEqual(self.count('weixin_inbound_manifests'), 1)
        self.assertEqual(self.count('weixin_attachments'), 1)

    def test_unknown_type_identifier_is_bounded_and_does_not_copy_arbitrary_string(self):
        items = [{'type': 2 ** 100000}, {'type': 'https://UNTRUSTED.invalid/private'},
                 {'type': 'custom-v2'}, {'type': True}, {'type': 2.0}, {'type': [4]}]
        self.ingest(self.message(items=items))
        manifest = self.manifest()
        self.assertTrue(manifest['parts'][0]['item_type_out_of_range'])
        self.assertEqual(manifest['parts'][0]['item_type_kind'], 'integer')
        self.assertNotIn('item_type', manifest['parts'][0])
        self.assertNotIn('UNTRUSTED', encoded(manifest))
        self.assertEqual(manifest['parts'][2]['item_type'], 'custom-v2')
        self.assertEqual(self.count('weixin_attachments'), 0)

    def test_invalid_unicode_and_overlong_voice_cannot_poison_cursor(self):
        for source, text in [('invalid-voice', '\ud800'), ('long-voice', '你' * 30000)]:
            with self.subTest(source=source):
                self.assertEqual(self.ingest(self.message(source, items=[{'type': 3, 'voice_item': {'text': text}}])), 1)
                manifest = self.manifest(source)
                self.assertFalse(manifest['complete'])
                self.assertTrue(manifest['text_truncated'])
                self.assertEqual(self.p.media.list_for_message(self.mid(source))[0]['status'], 'transcript_unavailable')
        self.assertEqual(self.p.weixin.account()['cursor'], 'new-cursor')

    def test_manifest_write_failure_rolls_back_inbox_and_cursor(self):
        with self.p.db:
            self.p.db.execute("CREATE TRIGGER fail_manifest BEFORE INSERT ON weixin_inbound_manifests BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ingest(self.message())
        self.assertEqual(self.count('weixin_inbox'), 0)
        self.assertEqual(self.count('weixin_inbound_manifests'), 0)
        self.assertEqual(self.p.weixin.account()['cursor'], '')

    def test_later_media_failure_rolls_back_manifest_and_other_input_facts(self):
        with patch.object(self.p.media, 'add', side_effect=RuntimeError('synthetic media interruption')):
            with self.assertRaises(RuntimeError):
                self.ingest(self.message())
        self.assertEqual(self.count('weixin_inbox'), 0)
        self.assertEqual(self.count('weixin_inbound_manifests'), 0)
        self.assertEqual(self.p.weixin.account()['cursor'], '')

    def test_process_exit_before_cursor_or_after_commit_keeps_atomic_source_facts(self):
        # Each child uses only this temporary DB and exits at one local window.
        root = str(Path(__file__).resolve().parents[1])
        script = '''import os,socket,sys
from probe.probe import Probe
def forbidden(*a,**kw): raise AssertionError('network forbidden')
socket.getaddrinfo=socket.create_connection=forbidden
p=Probe(sys.argv[1],clock=lambda:1800000000.)
mode=sys.argv[2]
if mode=='before_cursor':
    def trace(sql):
        if sql.startswith('UPDATE weixin_accounts SET cursor='): os._exit(71)
    p.db.set_trace_callback(trace)
else:
    p.notify_events=lambda:os._exit(72)
p.weixin.ingest(p.weixin.account(),{'msgs':[{'message_id':mode,'message_type':1,'from_user_id':'test-owner','to_user_id':'test-bot','item_list':[{'type':911}]}],'get_updates_buf':mode})
raise AssertionError('exit hook missing')
'''
        for mode, code in [('before_cursor', 71), ('after_commit', 72)]:
            result = subprocess.run([sys.executable, '-B', '-c', script, self.tmp.name, mode],
                                    cwd=root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, code, result.stderr)
            rows = self.p.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (mode,)).fetchall()
            self.assertEqual(len(rows), 0 if mode == 'before_cursor' else 1)
        self.assertEqual(self.count('weixin_inbound_manifests'), 1)
        self.assertEqual(self.p.weixin.account()['cursor'], 'after_commit')
        self.assertEqual(self.ingest(self.message('after_commit', items=[{'type': 911}])), 0)

    def test_legacy_source_without_manifest_stays_explicitly_unrecorded(self):
        self.ingest(self.message())
        with self.p.db:
            self.p.db.execute('DROP TABLE weixin_inbound_manifests')
        self.p.close()
        self.p = Probe(self.tmp.name, clock=lambda: self.now, weixin_client=NoNetworkClient())
        read = self.manifest()
        self.assertFalse(read['recorded'])
        self.assertFalse(read['complete'])
        self.assertEqual(read['state'], 'legacy_unrecorded')
        self.assertEqual(self.ingest(self.message()), 0)
        self.assertEqual(self.count('weixin_inbound_manifests'), 0)

    def test_manifest_read_enforces_owner_and_does_not_mutate(self):
        self.ingest(self.message())
        before = self.p.db.total_changes
        first = self.manifest()
        self.assertEqual(self.manifest(), first)
        self.assertEqual(self.p.db.total_changes, before)
        with self.p.db:
            self.p.db.execute("UPDATE weixin_inbox SET peer='different-owner' WHERE id=?", (self.mid(),))
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.manifest()


if __name__ == '__main__':
    unittest.main()
