"""Offline synthetic diagnostics only: no original image, sockets or messages."""
import io
import json
import socket
import struct
import tempfile
import unittest
import zlib
from unittest.mock import Mock, patch

from PIL import Image

from probe.media import (IMAGE_FORMATS, MAX_FILE, MAX_PIXELS, ImageValidationError,
                         download_cdn, image_info, validate_plain)
from probe.probe import Probe
from probe.weixin import WeixinError

OWNER, ACCOUNT = 'fixture-owner', 'fixture-account'
DIAGNOSTIC_FIELDS = {'reason', 'stage', 'width', 'height', 'pixels', 'format',
                     'observed_plain_bytes', 'max_pixels', 'max_plain_bytes'}


def picture(width=2, height=3, fmt='PNG'):
    result = io.BytesIO()
    Image.new('1', (width, height)).save(result, fmt)
    return result.getvalue()


def oversized_header(width, height):
    # Only a valid PNG header is necessary: the pixel guard must stop before
    # verify/decode. This fixture never allocates an oversized decoded raster.
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    return (b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 1, 0, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(b'\0\0')) + chunk(b'IEND', b''))


def corrupt_png_crc(raw):
    data = bytearray(raw)
    offset = raw.index(b'IDAT')
    length = struct.unpack('>I', raw[offset - 4:offset])[0]
    data[offset + 4 + length] ^= 1
    return bytes(data)


class NoNetworkClient:
    def poll(self, *args):
        raise AssertionError('no live polling in offline test')

    def send(self, *args):
        raise AssertionError('no message sends in offline test')

    send_items = send


class ImageDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1800000000.
        self.probe = self.open()
        self.addCleanup(lambda: self.probe.close())
        self.probe.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': ACCOUNT,
            'ilink_user_id': OWNER, 'bot_token': 'fixture-token'})
        self.index = 0

    def open(self):
        return Probe(self.temp.name, clock=lambda: self.now, weixin_client=NoNetworkClient())

    def ingest(self):
        self.index += 1
        source = 'fixture-image-' + str(self.index)
        self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs': [{
            'message_id': source, 'message_type': 1, 'from_user_id': OWNER, 'to_user_id': ACCOUNT,
            'item_list': [{'type': 2, 'image_item': {'media': {
                'full_url': 'https://novac2c.cdn.weixin.qq.com/fixture-private-reference'}}}]}]})
        return self.probe.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()[0]

    def failed(self, raw, reason, stage, *, exception=None):
        mid = self.ingest()
        download = Mock(return_value=raw)
        self.probe.media.download = download
        if exception is None:
            self.assertTrue(self.probe.media.process_once())
        else:
            with patch('PIL.Image.open', side_effect=exception):
                self.assertTrue(self.probe.media.process_once())
        row = self.probe.db.execute('SELECT * FROM weixin_attachments WHERE message_id=?', (mid,)).fetchone()
        self.assertEqual((row['status'], row['attempts'], row['error_kind']),
                         ('error', 1, 'invalid_or_oversize_image'))
        self.assertIsNone(row['ref_json'])
        for field in ('size', 'mime_type', 'sha256'):
            self.assertIsNone(row[field])
        self.assertFalse(self.probe.media.path(row['id']).exists())
        diagnostic = json.loads(row['provenance'])['image_validation']
        self.assertEqual(set(diagnostic), DIAGNOSTIC_FIELDS)
        self.assertEqual((diagnostic['reason'], diagnostic['stage']), (reason, stage))
        self.assertEqual(diagnostic['observed_plain_bytes'], len(raw))
        self.assertEqual((diagnostic['max_pixels'], diagnostic['max_plain_bytes']), (8_000_000, 20 * 1024 * 1024))
        self.now += 60
        self.probe.media.recover_inflight()
        self.assertFalse(self.probe.media.process_once())
        download.assert_called_once()
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)
        return mid, row['id'], diagnostic

    def test_exact_pixel_limit_and_long_narrow_images_still_pass(self):
        self.assertEqual(MAX_PIXELS, 8_000_000)
        for width, height in ((2000, 4000), (400, 20000), (1080, 7407), (64, 16000)):
            with self.subTest(width=width, height=height):
                raw = picture(width, height)
                self.assertLess(len(raw), MAX_FILE)
                self.assertEqual(image_info(raw), ('image/png', [width, height]))
                self.assertEqual(validate_plain(raw, {'kind': 'image'}), 'image/png')

    def test_one_pixel_above_limit_and_natural_long_geometry_are_distinct(self):
        for width, height in ((1, 8_000_001), (1080, 7408)):
            with self.subTest(width=width, height=height):
                raw = oversized_header(width, height)
                _, _, diagnostic = self.failed(raw, 'pixel_limit', 'dimensions')
                self.assertEqual((diagnostic['width'], diagnostic['height'], diagnostic['pixels']),
                                 (width, height, width * height))
                self.assertEqual(diagnostic['format'], 'PNG')

    def test_unsupported_format_is_safe_diagnostic_not_new_permission(self):
        _, _, diagnostic = self.failed(picture(fmt='BMP'), 'unsupported_format', 'format')
        self.assertEqual((diagnostic['width'], diagnostic['height'], diagnostic['pixels'], diagnostic['format']),
                         (2, 3, 6, 'BMP'))
        self.assertEqual(IMAGE_FORMATS, frozenset(('JPEG', 'PNG', 'WEBP', 'GIF')))

    def test_invalid_header_and_corrupt_verification_have_separate_reasons(self):
        _, _, diagnostic = self.failed(b'not an image: private fixture body', 'invalid_image_header', 'open')
        self.assertIsNone(diagnostic['width'])
        self.assertIsNone(diagnostic['format'])
        _, _, diagnostic = self.failed(corrupt_png_crc(picture()), 'image_verification_failed', 'verify')
        self.assertEqual((diagnostic['width'], diagnostic['height'], diagnostic['format']), (2, 3, 'PNG'))

    def test_actual_pillow_bomb_warning_and_error_remain_terminal(self):
        for width, height in ((10000, 10000), (20000, 10000)):
            with self.subTest(width=width, height=height):
                _, _, diagnostic = self.failed(oversized_header(width, height), 'decompression_bomb', 'open')
                self.assertIsNone(diagnostic['width'])
                self.assertIsNone(diagnostic['pixels'])

    def test_memory_and_unexpected_exception_text_never_persist(self):
        for exception, reason in ((MemoryError('private-exception-text'), 'memory_error'),
                                  (RuntimeError('private-exception-text'), 'image_validation_failed')):
            with self.subTest(reason=reason):
                mid, _, diagnostic = self.failed(picture(), reason, 'open', exception=exception)
                exposed = json.dumps(self.probe.weixin.read_message(mid), ensure_ascii=False)
                self.assertNotIn('private-exception-text', exposed)
                self.assertNotIn('fixture-private-reference', exposed)
                self.assertNotIn('fixture-token', exposed)
                self.assertEqual(diagnostic['reason'], reason)

    def test_metadata_and_both_rpc_projections_keep_same_safe_failure(self):
        mid, aid, diagnostic = self.failed(oversized_header(1080, 7408), 'pixel_limit', 'dimensions')
        for projection in ('full', 'reply'):
            with self.subTest(projection=projection):
                result = self.probe.rpc({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
                    'params': {'name': 'read_weixin_message', 'arguments': {
                        'message_id': mid, 'projection': projection}}})
                self.assertNotIn('error', result)
                meta = result['result']['structuredContent']['attachments'][0]
                self.assertEqual(meta['attachment_id'], aid)
                self.assertEqual(meta['image_validation'], diagnostic)
                self.assertEqual((meta['status'], meta['error_kind']), ('error', 'invalid_or_oversize_image'))
                self.assertIsNone(meta['size'])
                self.assertIsNone(meta['mime_type'])
                self.assertIsNone(meta['sha256'])
        self.probe.close()
        self.probe = self.open()
        self.probe.media.download = Mock(side_effect=AssertionError('old errors must not re-download'))
        self.probe.media.recover_inflight()
        self.assertFalse(self.probe.media.process_once())
        self.probe.media.download.assert_not_called()
        self.assertEqual(self.probe.weixin.read_message(mid)['attachments'][0]['image_validation'], diagnostic)

    def test_diagnostic_values_are_whitelisted_and_copy_is_not_mutable(self):
        error = ImageValidationError('pixel_limit', 'dimensions', 100,
            image_format='private-secret-format', width=True, height=-1)
        diagnostic = error.diagnostic
        self.assertEqual(set(diagnostic), DIAGNOSTIC_FIELDS)
        self.assertEqual(diagnostic['format'], 'OTHER')
        self.assertIsNone(diagnostic['width'])
        self.assertIsNone(diagnostic['height'])
        diagnostic['reason'] = 'changed'
        self.assertEqual(error.diagnostic['reason'], 'pixel_limit')
        self.assertEqual(str(error), 'invalid_or_oversize_image')

    def test_plain_byte_limit_is_unchanged_and_not_misreported_as_pixels(self):
        with self.assertRaisesRegex(WeixinError, '^invalid_media_size$'):
            validate_plain(b'x' * (MAX_FILE + 1), {'kind': 'image'})

    def test_incomplete_download_is_terminal_without_image_diagnostic_or_retry(self):
        mid = self.ingest()
        download = Mock(side_effect=WeixinError('media_download_incomplete'))
        self.probe.media.download = download
        self.assertTrue(self.probe.media.process_once())
        row = self.probe.db.execute('SELECT * FROM weixin_attachments WHERE message_id=?', (mid,)).fetchone()
        self.assertEqual((row['status'], row['attempts'], row['error_kind'], row['ref_json']),
                         ('error', 1, 'media_download_incomplete', None))
        self.assertNotIn('image_validation', json.loads(row['provenance']))
        self.now += 100
        self.probe.media.recover_inflight()
        self.assertFalse(self.probe.media.process_once())
        download.assert_called_once()


class DownloadLengthIntegrityTests(unittest.TestCase):
    def fake_get(self, declared, blocks):
        response = Mock(status=200)
        response.getheader.return_value = declared
        response.read.side_effect = [*blocks, b'']
        context, sock, connection = Mock(), Mock(), Mock()
        context.wrap_socket.return_value = sock
        connection._context = context
        connection.getresponse.return_value = response
        with patch('probe.media.socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]), \
             patch('probe.media.socket.create_connection', return_value=sock), \
             patch('probe.media.http.client.HTTPSConnection', return_value=connection):
            try:
                return download_cdn('https://novac2c.cdn.weixin.qq.com/fixture')
            finally:
                connection.close.assert_called_once()
                self.assertTrue(all(call.args == (65536,) for call in response.read.call_args_list))

    def test_declared_short_and_excess_body_are_terminal_integrity_failures(self):
        for declared, blocks in (('5', [b'abc']), ('2', [b'abc'])):
            with self.subTest(declared=declared):
                with self.assertRaisesRegex(WeixinError, '^media_download_incomplete$'):
                    self.fake_get(declared, blocks)

    def test_matching_declared_length_and_unspecified_length_still_work(self):
        self.assertEqual(self.fake_get('3', [b'a', b'bc']), b'abc')
        self.assertEqual(self.fake_get(None, [b'abc']), b'abc')

    def test_existing_declared_size_limit_is_not_relaxed(self):
        with self.assertRaisesRegex(WeixinError, '^media_too_large$'):
            self.fake_get(str(MAX_FILE + 17), [])


if __name__ == '__main__':
    unittest.main()
