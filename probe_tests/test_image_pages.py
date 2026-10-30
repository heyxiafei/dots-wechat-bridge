"""Offline paging fixtures only; no user images, network, uploads or sends."""
import base64
import hashlib
import io
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

from PIL import Image, ImageDraw, ImageOps

from probe.media import (IMAGE_PAGE_OVERLAP, IMAGE_PAGE_SIDE, MAX_FILE, MAX_IMAGE_PAGES,
                         MAX_IMAGE_PREVIEW_BYTES, MAX_PIXELS, RETENTION)
from probe.probe import Probe
from probe.weixin import WeixinError
from probe import media as media_module

OWNER, ACCOUNT = 'fixture-owner', 'fixture-account'


def synthetic_image(width, height, orientation=None):
    image = Image.new('L', (width, height), 240)
    draw = ImageDraw.Draw(image)
    for y in range(0, height, 128):
        draw.rectangle((0, y, width - 1, min(y + 63, height - 1)), fill=(y // 128 * 29) % 200)
    for x in range(0, width, 128):
        draw.rectangle((x, 0, min(x + 7, width - 1), height - 1), fill=17)
    output = io.BytesIO()
    if orientation is None:
        image.save(output, 'PNG')
    else:
        exif = Image.Exif()
        exif[274] = orientation
        image.save(output, 'JPEG', quality=95, exif=exif)
    return output.getvalue()


def header_only(width, height):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 1, 0, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(b'\0\0')) + chunk(b'IEND', b''))


def expected_preview(raw, box=None):
    with Image.open(io.BytesIO(raw)) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
        if box is None:
            image.thumbnail((2048, 2048))
        else:
            image = image.crop(box)
        result = io.BytesIO()
        image.save(result, 'JPEG', quality=85)
        return result.getvalue()


class NoNetwork:
    def forbidden(self, *args, **kwargs):
        raise AssertionError('offline fixture must not poll/upload/send')

    poll = send = send_items = get_upload_url = forbidden


class ImagePageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1800000000.
        self.probe = Probe(self.temp.name, clock=lambda: self.now, weixin_client=NoNetwork())
        self.addCleanup(self.probe.close)
        self.probe.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': ACCOUNT,
            'ilink_user_id': OWNER, 'bot_token': 'fixture-token'})
        self.index = 0

    def message(self, kind=2):
        self.index += 1
        source = 'synthetic-source-' + str(self.index)
        item = {'type': kind, ('image_item' if kind == 2 else 'file_item'): {'media': {
            'full_url': 'https://novac2c.cdn.weixin.qq.com/synthetic-only'}}}
        self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs': [{
            'message_id': source, 'message_type': 1, 'from_user_id': OWNER,
            'to_user_id': ACCOUNT, 'item_list': [item]}]})
        mid = self.probe.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()[0]
        aid = self.probe.db.execute('SELECT id FROM weixin_attachments WHERE message_id=?', (mid,)).fetchone()[0]
        return mid, aid

    def cached(self, raw):
        mid, aid = self.message()
        self.probe.media.download = Mock(return_value=raw)
        self.assertTrue(self.probe.media.process_once())
        self.assertEqual(self.probe.media.list_for_message(mid)[0]['status'], 'cached')
        self.probe.media.download = Mock(side_effect=AssertionError('cached preview must not download'))
        return mid, aid

    def legacy_cached(self, raw):
        # Synthetic representation of a legacy cached file; verifies the new
        # preview does not trust a cached flag to bypass current header guards.
        mid, aid = self.message()
        path = self.probe.media.path(aid)
        path.write_bytes(raw)
        path.chmod(0o600)
        with self.probe.db:
            self.probe.db.execute("UPDATE weixin_attachments SET status='cached',size=?,mime_type='image/png',sha256=?,ref_json=NULL WHERE id=?",
                (len(raw), hashlib.sha256(raw).hexdigest(), aid))
        self.probe.media.download = Mock(side_effect=AssertionError('no legacy re-download'))
        return mid, aid

    def read(self, mid, aid, page=None):
        metadata, block = self.probe.media.read_image(mid, aid, page=page)
        self.assertEqual((block['type'], block['mimeType']), ('image', 'image/jpeg'))
        raw = base64.b64decode(block['data'], validate=True)
        self.assertLessEqual(len(raw), MAX_IMAGE_PREVIEW_BYTES)
        self.assertEqual(metadata['preview_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertTrue(metadata['preview'])
        self.assertFalse(metadata['is_original'])
        self.assertFalse(metadata['is_complete_source'])
        self.assertTrue(metadata['lossy_preview'])
        self.assertTrue(metadata['full_decode_required'])
        self.assertFalse(metadata['rss_budget_enforced'])
        self.assertTrue(metadata['first_frame_only'])
        self.probe.media.download.assert_not_called()
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)
        return metadata, raw

    def test_default_overview_keeps_legacy_pixels_encoding_and_original_hash(self):
        original = synthetic_image(3000, 1000)
        mid, aid = self.cached(original)
        metadata, block = self.probe.media.read_image(mid, aid)
        result = base64.b64decode(block['data'])
        self.assertEqual(result, expected_preview(original))
        self.assertEqual(metadata['preview_dimensions'], [2048, 683])
        self.assertEqual(metadata['source_dimensions'], [3000, 1000])
        self.assertEqual(metadata['source_box'], [0, 0, 3000, 1000])
        self.assertEqual(metadata['preview_mode'], 'overview')
        self.assertIsNone(metadata['page_index'])
        self.assertEqual(metadata['sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(metadata['original_sha256'], metadata['sha256'])

    def test_vertical_eight_million_pixels_first_middle_and_last_regions_are_clear(self):
        original = synthetic_image(400, 20000)
        self.assertEqual(400 * 20000, MAX_PIXELS)
        mid, aid = self.cached(original)
        for page, box in ((0, [0, 0, 400, 2048]), (1, [0, 1984, 400, 4032]),
                          (10, [0, 19840, 400, 20000])):
            with self.subTest(page=page):
                metadata, result = self.read(mid, aid, page)
                self.assertEqual(metadata['preview_mode'], 'region')
                self.assertEqual(metadata['source_dimensions'], [400, 20000])
                self.assertEqual((metadata['page_index'], metadata['page_count']), (page, 11))
                self.assertEqual(metadata['page_grid'], {'columns': 1, 'rows': 11})
                self.assertEqual(metadata['source_box'], box)
                self.assertEqual(metadata['preview_dimensions'], [box[2] - box[0], box[3] - box[1]])
                self.assertEqual(metadata['overlap'], 64)
                self.assertEqual(result, expected_preview(original, tuple(box)))
        overview, _ = self.read(mid, aid)
        self.assertEqual(overview['preview_dimensions'], [41, 2048])
        self.assertEqual(overview['page_count'], 11)

    def test_horizontal_image_and_two_axis_grid_are_row_major(self):
        original = synthetic_image(20000, 400)
        mid, aid = self.cached(original)
        metadata, result = self.read(mid, aid, 10)
        self.assertEqual(metadata['source_box'], [19840, 0, 20000, 400])
        self.assertEqual(metadata['page_grid'], {'columns': 11, 'rows': 1})
        self.assertEqual(result, expected_preview(original, (19840, 0, 20000, 400)))
        grid = synthetic_image(2400, 2400)
        mid, aid = self.cached(grid)
        expected = ([0, 0, 2048, 2048], [1984, 0, 2400, 2048],
                    [0, 1984, 2048, 2400], [1984, 1984, 2400, 2400])
        for page, box in enumerate(expected):
            with self.subTest(grid_page=page):
                metadata, result = self.read(mid, aid, page)
                self.assertEqual(metadata['page_count'], 4)
                self.assertEqual(metadata['source_box'], box)
                self.assertEqual(result, expected_preview(grid, tuple(box)))

    def test_exif_rotation_plans_oriented_coordinates_and_strips_metadata(self):
        original = synthetic_image(600, 2400, orientation=6)
        mid, aid = self.cached(original)
        metadata, result = self.read(mid, aid, 1)
        self.assertEqual(metadata['source_dimensions'], [2400, 600])
        self.assertEqual(metadata['source_box'], [1984, 0, 2400, 600])
        self.assertEqual(metadata['preview_dimensions'], [416, 600])
        self.assertEqual(metadata['page_grid'], {'columns': 2, 'rows': 1})
        self.assertEqual(result, expected_preview(original, (1984, 0, 2400, 600)))
        with Image.open(io.BytesIO(result)) as preview:
            self.assertNotIn(274, preview.getexif())

    def test_invalid_page_types_reject_before_original_read(self):
        mid, aid = self.cached(synthetic_image(4, 5))
        with patch.object(self.probe.media, '_read_image_verified_bytes', side_effect=AssertionError('must reject before read')) as reader:
            for page in (-1, True, False, 0., '0', [], {}):
                with self.subTest(kind=type(page).__name__):
                    with self.assertRaisesRegex(WeixinError, '^invalid_image_page$'):
                        self.probe.media.read_image(mid, aid, page=page)
            reader.assert_not_called()

    def test_page_range_and_page_cap_reject_before_full_decode_but_overview_still_works(self):
        mid, aid = self.cached(synthetic_image(4, 5))
        with patch('PIL.ImageOps.exif_transpose', side_effect=AssertionError('no decode for out of range')):
            with self.assertRaisesRegex(WeixinError, '^image_page_out_of_range$'):
                self.probe.media.read_image(mid, aid, page=1)
        mid, aid = self.cached(synthetic_image(1, 200000))
        with patch('PIL.ImageOps.exif_transpose', side_effect=AssertionError('no decode above page cap')):
            with self.assertRaisesRegex(WeixinError, '^page_limit_error$'):
                self.probe.media.read_image(mid, aid, page=0)
        metadata, _ = self.read(mid, aid)
        self.assertEqual(metadata['preview_mode'], 'overview')
        self.assertFalse(metadata['pagination_available'])
        self.assertIsNone(metadata['page_count'])
        self.assertIsNone(metadata['page_grid'])

    def test_oversized_cached_legacy_header_cannot_bypass_guards_by_paging(self):
        for width, height in ((1, 8_000_001), (10000, 10000)):
            with self.subTest(width=width, height=height):
                mid, aid = self.legacy_cached(header_only(width, height))
                with patch('PIL.ImageOps.exif_transpose', side_effect=AssertionError('no oversized decode')):
                    with self.assertRaisesRegex(WeixinError, '^invalid_or_oversize_image$'):
                        self.probe.media.read_image(mid, aid, page=0)

    def test_changed_hash_owner_message_or_expired_attachment_rejects_before_decode(self):
        mid, aid = self.cached(synthetic_image(4, 5))
        second, _ = self.message()
        with patch('PIL.Image.open', side_effect=AssertionError('authorization must precede decode')):
            with self.assertRaisesRegex(WeixinError, '^unknown_owner_attachment$'):
                self.probe.media.read_image(second, aid, page=0)
            with self.probe.db:
                self.probe.db.execute("UPDATE weixin_accounts SET owner='changed-owner'")
            with self.assertRaisesRegex(WeixinError, '^unknown_owner_attachment$'):
                self.probe.media.read_image(mid, aid, page=0)
        with self.probe.db:
            self.probe.db.execute('UPDATE weixin_accounts SET owner=?', (OWNER,))
        self.now += RETENTION + 1
        with patch('PIL.Image.open', side_effect=AssertionError('expired cache must not decode')):
            with self.assertRaisesRegex(WeixinError, '^attachment_expired$'):
                self.probe.media.read_image(mid, aid, page=0)

    def test_tamper_symlink_fifo_and_declared_oversize_cache_are_rejected(self):
        original = synthetic_image(4, 5)
        mid, aid = self.cached(original)
        path = self.probe.media.path(aid)
        path.write_bytes(b'bad' + original[3:])
        with self.assertRaisesRegex(WeixinError, '^attachment_integrity_error$'):
            self.probe.media.read_image(mid, aid, page=0)
        path.unlink()
        path.symlink_to(Path(self.temp.name) / 'probe.sqlite')
        with self.assertRaisesRegex(WeixinError, '^attachment_cache_unavailable$'):
            self.probe.media.read_image(mid, aid, page=0)
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaisesRegex(WeixinError, '^attachment_integrity_error$'):
            self.probe.media.read_image(mid, aid, page=0)
        path.unlink()
        path.write_bytes(original)
        with self.probe.db:
            self.probe.db.execute('UPDATE weixin_attachments SET size=? WHERE id=?', (MAX_FILE + 1, aid))
        with patch('PIL.Image.open', side_effect=AssertionError('size must precede decode')):
            with self.assertRaisesRegex(WeixinError, '^attachment_integrity_error$'):
                self.probe.media.read_image(mid, aid, page=0)

    def test_fd_identity_change_during_read_is_not_a_valid_preview(self):
        original = synthetic_image(4, 5)
        mid, aid = self.cached(original)
        path = self.probe.media.path(aid)
        real_fdopen = os.fdopen

        class RacingReader:
            def __init__(self, source): self.source = source
            def __enter__(self): return self
            def __exit__(self, *args): self.source.close()
            def fileno(self): return self.source.fileno()
            def read(self, length):
                result = self.source.read(length)
                path.write_bytes(original[:-1])
                return result

        with patch('probe.media.os.fdopen', side_effect=lambda fd, *args: RacingReader(real_fdopen(fd, *args))):
            with self.assertRaisesRegex(WeixinError, '^attachment_integrity_error$'):
                self.probe.media.read_image(mid, aid, page=0)

    def test_owner_ttl_and_metadata_are_rechecked_after_decode(self):
        original = synthetic_image(4, 5)
        transpose = ImageOps.exif_transpose
        for change, expected in (('owner', 'unknown_owner_attachment'),
                                 ('expiry', 'attachment_expired'),
                                 ('hash', 'attachment_integrity_error')):
            with self.subTest(change=change):
                mid, aid = self.cached(original)
                now = self.now
                def changed(source):
                    result = transpose(source)
                    if change == 'expiry':
                        self.now += RETENTION + 1
                    else:
                        with self.probe.db:
                            if change == 'owner':
                                self.probe.db.execute("UPDATE weixin_accounts SET owner='changed-owner'")
                            else:
                                self.probe.db.execute('UPDATE weixin_attachments SET sha256=? WHERE id=?', ('0' * 64, aid))
                    return result
                with patch('PIL.ImageOps.exif_transpose', side_effect=changed):
                    with self.assertRaisesRegex(WeixinError, '^' + expected + '$'):
                        self.probe.media.read_image(mid, aid, page=0)
                self.now = now
                with self.probe.db:
                    self.probe.db.execute('UPDATE weixin_accounts SET owner=?', (OWNER,))

    def test_preview_output_is_bounded_and_memory_failure_is_sanitized(self):
        mid, aid = self.cached(synthetic_image(128, 128))
        with patch('probe.media.MAX_IMAGE_PREVIEW_BYTES', 64):
            with self.assertRaisesRegex(WeixinError, '^image_preview_too_large$'):
                self.probe.media.read_image(mid, aid, page=0)
        with patch('PIL.ImageOps.exif_transpose', side_effect=MemoryError('private-fixture-memory-text')):
            with self.assertRaisesRegex(WeixinError, '^image_preview_resource_error$') as raised:
                self.probe.media.read_image(mid, aid, page=0)
            self.assertNotIn('private-fixture-memory-text', str(raised.exception))

    def test_preview_is_readonly_and_never_base64_encodes_the_original(self):
        original = synthetic_image(2400, 1200)
        mid, aid = self.cached(original)
        path = self.probe.media.path(aid)
        changes = self.probe.db.total_changes
        files = set(path.parent.iterdir())
        real_encode = base64.b64encode
        encoded_inputs = []
        def checked_encode(raw):
            self.assertNotEqual(raw, original)
            encoded_inputs.append(len(raw))
            return real_encode(raw)
        with patch('probe.media.base64.b64encode', side_effect=checked_encode):
            metadata, _ = self.probe.media.read_image(mid, aid, page=1)
        self.assertEqual(len(encoded_inputs), 1)
        self.assertEqual(self.probe.db.total_changes, changes)
        self.assertEqual(set(path.parent.iterdir()), files)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(metadata['original_sha256'], hashlib.sha256(original).hexdigest())
        self.probe.media.download.assert_not_called()
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_paging_constants_keep_existing_guard_and_resource_contract(self):
        self.assertEqual((MAX_FILE, MAX_PIXELS), (20 * 1024 * 1024, 8_000_000))
        self.assertEqual((IMAGE_PAGE_SIDE, IMAGE_PAGE_OVERLAP, MAX_IMAGE_PAGES), (2048, 64, 64))
        self.assertEqual(MAX_IMAGE_PREVIEW_BYTES, 4 * 1024 * 1024)

    def test_decode_copies_crop_and_output_are_explicitly_closed(self):
        mid, aid = self.cached(synthetic_image(2400, 600))
        transpose, convert, crop = ImageOps.exif_transpose, Image.Image.convert, Image.Image.crop
        buffer_type = media_module._PreviewBuffer
        images, buffers = [], []
        def observed_transpose(image):
            result = transpose(image)
            images.append(result)
            return result
        def observed_convert(image, *args, **kwargs):
            result = convert(image, *args, **kwargs)
            images.append(result)
            return result
        def observed_crop(image, *args, **kwargs):
            result = crop(image, *args, **kwargs)
            images.append(result)
            return result
        def observed_buffer():
            result = buffer_type()
            buffers.append(result)
            return result
        with patch('PIL.ImageOps.exif_transpose', side_effect=observed_transpose), \
             patch.object(Image.Image, 'convert', observed_convert), \
             patch.object(Image.Image, 'crop', observed_crop), \
             patch('probe.media._PreviewBuffer', side_effect=observed_buffer):
            self.read(mid, aid, 1)
        self.assertEqual(len(images), 3)
        self.assertTrue(all(buffer.closed for buffer in buffers))
        for image in images:
            with self.assertRaises(ValueError):
                image.getpixel((0, 0))


if __name__ == '__main__':
    unittest.main()
