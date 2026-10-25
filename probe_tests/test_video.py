"""Offline native-video protocol fixtures; these bytes are not a playback test."""
import base64
import hashlib
import io
import json
import socket
import tempfile
import unittest
from unittest.mock import patch

from probe.probe import Probe
from probe.weixin import WeixinError
from probe.media import MAX_FILE, MAX_CHUNK, download_cdn, video_info
from probe_tests import test_media as incoming_fixture
from probe_tests import test_outbound as outgoing_fixture


class NativeVideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1800000000.
        self.client = outgoing_fixture.FakeClient()
        self.probe = Probe(self.temp.name, clock=lambda: self.now, weixin_client=self.client)
        self.addCleanup(self.probe.close)
        self.probe.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
            'ilink_user_id': 'fixture-owner', 'bot_token': 'fixture-token'})
        # Structural MP4 fixture only. Real decoding/playback needs a real clip.
        def box(kind, data):
            return (8 + len(data)).to_bytes(4, 'big') + kind + data
        handler = box(b'hdlr', b'\0' * 8 + b'vide' + b'\0' * 12)
        movie = box(b'moov', box(b'trak', box(b'mdia', handler)))
        self.raw = box(b'ftyp', b'mp42' + b'\0' * 4 + b'mp42isom') + movie + box(b'mdat', bytes(range(256)) * 300)
        self.downloads, self.uploads = [], []
        def download(url):
            self.downloads.append(url)
            return incoming_fixture.cipher(self.raw)
        self.probe.media.download = download
        def upload(url, source, key, size):
            raw = source.read()
            self.assertEqual((len(raw), size), (len(self.raw), len(self.raw)))
            self.uploads.append((raw, key))
            return 'fixture-video-download-param'
        self.probe.outbound.upload = upload

    def video_item(self, ascii_hex=True):
        key = incoming_fixture.KEY.hex().encode() if ascii_hex else incoming_fixture.KEY
        return {'type': 5, 'video_item': {'media': {
            'encrypt_query_param': 'fixture-video-param',
            'aes_key': base64.b64encode(key).decode(), 'encrypt_type': 1},
            # Optional upstream fields do not define plaintext-integrity contracts.
            'video_size': (len(self.raw) // 16 + 1) * 16,
            'video_md5': 'upstream-unspecified', 'play_length': 123,
            'thumb_media': {'full_url': 'https://untrusted.invalid/ignored-thumb'}}}

    def ingest(self, source='video-one', item=None, **changes):
        msg = {'message_id': source, 'message_type': 1, 'from_user_id': 'fixture-owner',
            'to_user_id': 'fixture-bot', 'context_token': 'fixture-context',
            'item_list': [self.video_item() if item is None else item]}
        msg.update(changes)
        self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs': [msg]})
        row = self.probe.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()
        return row['id'] if row else None

    def ready(self, kind='video', identity='upload-video'):
        uid = self.probe.outbound.begin(identity, 'fixture.mp4', kind, len(self.raw),
            hashlib.sha256(self.raw).hexdigest())['upload_id']
        for offset in range(0, len(self.raw), MAX_CHUNK):
            raw = self.raw[offset:offset + MAX_CHUNK]
            self.probe.outbound.chunk(uid, offset, base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.probe.outbound.finalize(uid)['status'], 'ready')
        return uid

    def test_native_inbound_preserves_original_both_aes_key_encodings_and_dedup(self):
        for index, ascii_hex in enumerate((False, True)):
            item = self.video_item(ascii_hex)
            mid = self.ingest(str(index), item)
            self.ingest(str(index), item)
            self.assertTrue(self.probe.media.process_once())
            self.assertFalse(self.probe.media.process_once())
            result = self.probe.weixin.read_message(mid)
            self.assertEqual((result['kind'], result['unsupported_types']), ('media', []))
            self.assertEqual(len(result['attachments']), 1)
            meta = result['attachments'][0]
            self.assertEqual((meta['kind'], meta['status'], meta['mime_type']), ('video', 'cached', 'video/mp4'))
            self.assertEqual(meta['filename'], 'video.mp4')
            parts = [base64.b64decode(self.probe.media.read_chunk(mid, meta['attachment_id'], offset, MAX_CHUNK)['data_base64'])
                     for offset in range(0, len(self.raw), MAX_CHUNK)]
            self.assertEqual(b''.join(parts), self.raw)
            self.assertEqual(meta['sha256'], hashlib.sha256(self.raw).hexdigest())
            with self.assertRaisesRegex(WeixinError, 'not_image'):
                self.probe.media.read_image(mid, meta['attachment_id'])
            safe = json.dumps(result)
            for secret in ('fixture-token', 'fixture-video-param', 'fixture-owner', 'ignored-thumb'):
                self.assertNotIn(secret, safe)
        self.assertEqual(len(self.downloads), 2)

    def test_native_inbound_owner_scope_safe_reference_and_bad_ciphertext(self):
        self.assertIsNone(self.ingest('other-owner', from_user_id='other'))
        item = self.video_item()
        item['video_item']['media']['full_url'] = 'https://untrusted.invalid/video'
        mid = self.ingest('bad-reference', item)
        self.assertEqual(self.probe.weixin.read_message(mid)['attachments'][0]['error_kind'], 'invalid_media_destination')
        self.assertEqual(self.downloads, [])
        mid = self.ingest('bad-cipher')
        self.probe.media.download = lambda url: b'bad-ciphertext'
        self.probe.media.process_once()
        self.assertEqual(self.probe.weixin.read_message(mid)['attachments'][0]['error_kind'], 'invalid_media_ciphertext')
        with patch('probe.media.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]), patch('probe.media.socket.create_connection') as connect:
            with self.assertRaisesRegex(WeixinError, 'non_public'):
                download_cdn('https://novac2c.cdn.weixin.qq.com/c2c/download?fixture')
            connect.assert_not_called()

    def test_native_inbound_missing_key_and_oversize_fail_without_payload_exposure(self):
        item = self.video_item()
        del item['video_item']['media']['aes_key']
        mid = self.ingest('missing-key', item)
        self.assertEqual(self.probe.weixin.read_message(mid)['attachments'][0]['error_kind'], 'missing_media_key')
        self.assertEqual(self.downloads, [])
        mid = self.ingest('oversize')
        self.probe.media.download = lambda url: b'x' * (MAX_FILE + 17)
        self.probe.media.process_once()
        self.assertEqual(self.probe.weixin.read_message(mid)['attachments'][0]['error_kind'], 'media_too_large')

    def test_native_outbound_uses_upload_two_message_five_and_ciphertext_size(self):
        mid = self.ingest()
        uid = self.ready()
        sid = self.probe.outbound.queue('send-video', uid, mid)['send_id']
        self.assertTrue(self.probe.weixin.send_once())
        ticket = self.client.tickets[-1]
        self.assertEqual((ticket['media_type'], ticket['rawsize'], ticket['rawfilemd5'], ticket['no_need_thumb']),
            (2, len(self.raw), hashlib.md5(self.raw).hexdigest(), True))
        cipher_size = (len(self.raw) // 16 + 1) * 16
        self.assertEqual(ticket['filesize'], cipher_size)
        item = self.client.sent[-1][1][0]
        self.assertEqual(item['type'], 5)
        self.assertEqual(set(item['video_item']), {'media', 'video_size'})
        self.assertEqual(item['video_item']['video_size'], cipher_size)
        media = item['video_item']['media']
        self.assertEqual(base64.b64decode(media['aes_key']).decode(), self.uploads[-1][1].hex())
        self.assertEqual(self.uploads[-1][0], self.raw)
        self.assertEqual(self.probe.outbound.queue('send-video', uid, mid)['send_id'], sid)
        self.assertFalse(self.probe.weixin.send_once())
        self.assertEqual(len(self.client.sent), 1)

    def test_native_unknown_is_not_resent_or_downgraded_to_file(self):
        mid = self.ingest()
        uid = self.ready()
        sid = self.probe.outbound.queue('send-video', uid, mid)['send_id']
        self.client.results = [WeixinError('api_timeout')]
        self.probe.weixin.send_once()
        self.assertEqual(self.probe.weixin.send_status(sid)['transport_status'], 'delivery_unknown')
        self.probe.outbound.queue('send-video', uid, mid)
        self.assertFalse(self.probe.weixin.send_once())
        self.assertEqual([x[1][0]['type'] for x in self.client.sent], [5])
        self.assertIsNone(self.probe.db.execute('SELECT item_json FROM weixin_outbox WHERE message_id=?', (sid,)).fetchone()[0])
        with self.assertRaisesRegex(WeixinError, 'identity_conflict'):
            self.probe.outbound.begin('upload-video', 'fixture.mp4', 'file', len(self.raw), hashlib.sha256(self.raw).hexdigest())

    def test_mp4_sent_as_file_keeps_distinct_file_protocol_and_size_limit(self):
        mid = self.ingest()
        uid = self.ready(kind='file')
        self.probe.outbound.queue('send-file', uid, mid)
        self.probe.weixin.send_once()
        self.assertEqual(self.client.tickets[-1]['media_type'], 3)
        self.assertEqual(self.client.sent[-1][1][0]['type'], 4)
        with self.assertRaisesRegex(WeixinError, 'invalid_upload_size'):
            self.probe.outbound.begin('too-large', 'fixture.mp4', 'video', MAX_FILE + 1, '0' * 64)
        schema = next(x for x in Probe.tools() if x['name'] == 'begin_weixin_upload')['inputSchema']
        self.assertIn('video', schema['properties']['kind']['enum'])

    def test_invalid_video_container_and_extension_fail_before_any_upload(self):
        for raw in (b'not video', self.raw[:30], self.raw.replace(b'vide', b'soun'),
                    self.raw.replace(b'mdat', b'free'), self.raw.replace(b'ftyp', b'free'),
                    b'\x7f\xff\xff\xfffree' + self.raw):
            with self.assertRaisesRegex(WeixinError, 'invalid_or_unsupported_video'):
                video_info(io.BytesIO(raw))
        with self.assertRaisesRegex(WeixinError, 'unsupported_video_extension'):
            self.probe.outbound.begin('wrong-extension', 'clip.txt', 'video', len(self.raw), hashlib.sha256(self.raw).hexdigest())
        bad = b'not a real mp4'
        uid = self.probe.outbound.begin('bad-video', 'clip.mp4', 'video', len(bad), hashlib.sha256(bad).hexdigest())['upload_id']
        self.probe.outbound.chunk(uid, 0, base64.b64encode(bad).decode(), hashlib.sha256(bad).hexdigest())
        self.assertEqual(self.probe.outbound.finalize(uid)['error_kind'], 'invalid_or_unsupported_video')
        mid = self.ingest('invalid-inbound')
        self.probe.media.download = lambda url: incoming_fixture.cipher(bad)
        self.probe.media.process_once()
        self.assertEqual(self.probe.weixin.read_message(mid)['attachments'][0]['error_kind'], 'invalid_or_unsupported_video')
        self.assertEqual((self.uploads, self.client.sent), ([], []))

    def test_mp4_box_boundaries_extended_header_tail_moov_and_eof_mdat(self):
        def box(kind, raw, extended=False):
            if extended:
                return b'\0\0\0\1' + kind + (16 + len(raw)).to_bytes(8, 'big') + raw
            return (8 + len(raw)).to_bytes(4, 'big') + kind + raw
        ftyp = box(b'ftyp', b'isom' + b'\0' * 4)  # Empty compatible-brand list is valid.
        hdlr = box(b'hdlr', b'\0' * 8 + b'vide' + b'\0' * 12, extended=True)
        moov = box(b'moov', box(b'trak', box(b'mdia', hdlr)))
        for raw in (ftyp + box(b'free', b'pad') + box(b'mdat', b'data') + moov,
                    ftyp + moov + b'\0\0\0\0mdat' + b'data'):
            stream = io.BytesIO(raw)
            stream.seek(3)
            self.assertEqual(video_info(stream), 'video/mp4')
            self.assertEqual(stream.tell(), 3)
        malformed = (ftyp + box(b'moov', b'\0\0\0\x64trak') + box(b'mdat', b'data'),
                     ftyp + moov + b'\0\0\0\1mdat' + b'\0' * 8,
                     ftyp + moov + box(b'mdat', b'data') + b'x',
                     ftyp + moov + box(b'mdat', b'data') + box(b'free', b'') * 4096)
        nested = b''
        for _ in range(8):
            nested = box(b'moov', nested)
        malformed += (ftyp + nested + box(b'mdat', b'data'),)
        for raw in malformed:
            with self.assertRaisesRegex(WeixinError, 'invalid_or_unsupported_video'):
                video_info(io.BytesIO(raw))


if __name__ == '__main__':
    unittest.main()
