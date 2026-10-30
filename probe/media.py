"""Private owner-scoped Tencent CDN attachment transport, with no model runtime.

AES-128 ECB / PKCS7 follows Tencent's pic-decrypt.ts and aes-ecb.ts.
Opaque CDN references and keys remain inside the private SQLite database.
"""
import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import time
import unicodedata
import warnings
from types import MappingProxyType
from urllib.parse import urlencode, urlsplit

try:
    from .weixin import WeixinError, encoded, public_ip
except ImportError:
    from weixin import WeixinError, encoded, public_ip

MAX_FILE = 20 * 1024 * 1024
MAX_CACHE = 128 * 1024 * 1024
MAX_CHUNK = 65536
MAX_BLOCK = 5 * 1024 * 1024
MAX_DOWNLOAD_ATTEMPTS = 3
RETENTION = 7 * 86400
MAX_PIXELS = 8_000_000
CDN_HOSTS = frozenset(('novac2c.cdn.weixin.qq.com', 'ilinkai.weixin.qq.com',
                      'wx.qlogo.cn', 'thirdwx.qlogo.cn', 'res.wx.qq.com',
                      'mmbiz.qpic.cn', 'mmbiz.qlogo.cn'))
IMAGE_FORMATS = frozenset(('JPEG', 'PNG', 'WEBP', 'GIF'))
IMAGE_DIAGNOSTIC_FORMATS = IMAGE_FORMATS | frozenset(('BMP', 'TIFF', 'ICO', 'PPM',
    'PCX', 'TGA', 'PDF', 'EPS', 'AVIF', 'HEIF', 'MPO'))
IMAGE_FAILURE_REASONS = frozenset(('unsupported_format', 'pixel_limit',
    'decompression_bomb', 'invalid_image_header', 'image_verification_failed',
    'memory_error', 'image_validation_failed'))
IMAGE_VALIDATION_STAGES = frozenset(('open', 'format', 'dimensions', 'verify'))
IMAGE_PAGE_SIDE = 2048
IMAGE_PAGE_OVERLAP = 64
MAX_IMAGE_PAGES = 64
MAX_IMAGE_PREVIEW_BYTES = 4 * 1024 * 1024


def _image_page_grid(width, height):
    """Integer grid only; never allocate a list proportional to image length."""
    stride = IMAGE_PAGE_SIDE - IMAGE_PAGE_OVERLAP
    axis = lambda size: 1 + (max(0, size - IMAGE_PAGE_SIDE) + stride - 1) // stride
    columns, rows = axis(width), axis(height)
    return columns, rows, columns * rows


class _PreviewBuffer(io.BytesIO):
    def write(self, block):
        if self.tell() + len(block) > MAX_IMAGE_PREVIEW_BYTES:
            raise WeixinError('image_preview_too_large')
        return super().write(block)


class ImageValidationError(WeixinError):
    """Legacy public error plus strictly projected, non-content diagnostics.

    Never retain an exception object/string, input bytes, a URL or a key. This
    is evidence about this one failed validation, not a new retry permission.
    """
    def __init__(self, reason, stage, observed_plain_bytes, *, image_format=None,
                 width=None, height=None):
        super().__init__('invalid_or_oversize_image')
        if reason not in IMAGE_FAILURE_REASONS or stage not in IMAGE_VALIDATION_STAGES:
            raise ValueError('invalid_image_diagnostic_category')
        safe_int = lambda n, maximum: n if type(n) is int and 0 <= n <= maximum else None
        width, height = safe_int(width, 2 ** 31 - 1), safe_int(height, 2 ** 31 - 1)
        pixels = width * height if width is not None and height is not None else None
        self._diagnostic = MappingProxyType(dict(reason=reason, stage=stage,
            width=width, height=height, pixels=safe_int(pixels, 2 ** 53 - 1),
            format=(image_format if image_format in IMAGE_DIAGNOSTIC_FORMATS else
                    'OTHER' if isinstance(image_format, str) else None),
            observed_plain_bytes=safe_int(observed_plain_bytes, 2 ** 53 - 1),
            max_pixels=MAX_PIXELS, max_plain_bytes=MAX_FILE))

    @property
    def diagnostic(self):
        return dict(self._diagnostic)


def safe_name(value, fallback='attachment.bin'):
    if not isinstance(value, str):
        return fallback
    value = unicodedata.normalize('NFKC', value).replace('\\', '/').split('/')[-1]
    value = ''.join(c for c in value if not unicodedata.category(c).startswith('C'))
    value = value.strip(' .')[:120]
    return value or fallback


def cdn_parts(value):
    try:
        p = urlsplit(value)
        if (not isinstance(value, str) or len(value) > 16384 or any(c in value for c in '\r\n\x00')
                or p.scheme != 'https' or p.hostname not in CDN_HOSTS
                or p.port not in (None, 443) or p.username or p.password or p.fragment):
            raise ValueError()
        return p
    except (ValueError, TypeError):
        raise WeixinError('invalid_media_destination') from None


def download_cdn(value):
    """Pinned public DNS address, TLS original host, bounded GET; no redirects."""
    p = cdn_parts(value)
    conn = None
    deadline = time.monotonic() + 35
    try:
        addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)))
        if not addresses or not all(public_ip(a) for a in addresses):
            raise WeixinError('non_public_media_address')
        conn = http.client.HTTPSConnection(p.hostname, timeout=10, context=ssl.create_default_context())
        sock = socket.create_connection((addresses[0], 443), timeout=10)
        try:
            conn.sock = conn._context.wrap_socket(sock, server_hostname=p.hostname)
        except BaseException:
            sock.close()
            raise
        conn.request('GET', (p.path or '/') + ('?' + p.query if p.query else ''),
                     headers={'Accept': 'application/octet-stream', 'Accept-Encoding': 'identity'})
        response = conn.getresponse()
        if not 200 <= response.status < 300:
            raise WeixinError('media_http_error', response.status)
        length = response.getheader('Content-Length')
        if length and (not length.isdigit() or int(length) > MAX_FILE + 16):
            raise WeixinError('media_too_large')
        expected_length = int(length) if length else None
        chunks, size = [], 0
        while True:
            if time.monotonic() >= deadline:
                raise WeixinError('media_timeout')
            if conn.sock:
                conn.sock.settimeout(max(.1, min(10, deadline - time.monotonic())))
            block = response.read(65536)
            if not block:
                break
            size += len(block)
            if size > MAX_FILE + 16:
                raise WeixinError('media_too_large')
            chunks.append(block)
        if expected_length is not None and size != expected_length:
            # A completed bounded read with a mismatched declared length is
            # terminal integrity failure, not a transient GET retry category.
            raise WeixinError('media_download_incomplete')
        return b''.join(chunks)
    except WeixinError:
        raise
    except (socket.timeout, TimeoutError):
        raise WeixinError('media_timeout') from None
    except Exception:
        raise WeixinError('media_network_error') from None
    finally:
        if conn:
            conn.close()


def key_bytes(ref):
    try:
        if ref.get('hex_key'):
            if not re.fullmatch(r'[a-fA-F0-9]{32}', ref['hex_key']):
                raise ValueError()
            return bytes.fromhex(ref['hex_key'])
        key = base64.b64decode(ref['key'], validate=True)
        if len(key) == 32:
            key = bytes.fromhex(key.decode('ascii'))
        if len(key) != 16:
            raise ValueError()
        return key
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise WeixinError('invalid_media_key') from None


def decrypt(raw, ref):
    if len(raw) > MAX_FILE + 16:
        raise WeixinError('media_too_large')
    if not ref.get('key') and not ref.get('hex_key'):
        if ref['kind'] == 'image':
            return raw
        raise WeixinError('missing_media_key')
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = key_bytes(ref)
    if not raw or len(raw) % 16:
        raise WeixinError('invalid_media_ciphertext')
    try:
        decoder = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        plain = decoder.update(raw) + decoder.finalize()
        unpad = padding.PKCS7(128).unpadder()
        result = unpad.update(plain) + unpad.finalize()
    except ValueError:
        raise WeixinError('invalid_media_padding') from None
    if len(result) > MAX_FILE:
        raise WeixinError('media_too_large')
    return result


def image_info(raw):
    from PIL import Image, UnidentifiedImageError
    stage, image_format, width, height = 'open', None, None, None

    def failure(reason):
        return ImageValidationError(reason, stage, len(raw), image_format=image_format,
                                    width=width, height=height)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                image_format, width, height = image.format, image.width, image.height
                stage = 'format'
                if image_format not in IMAGE_FORMATS:
                    raise failure('unsupported_format')
                stage = 'dimensions'
                if width * height > MAX_PIXELS:
                    raise failure('pixel_limit')
                mime, dimensions = Image.MIME[image.format], [image.width, image.height]
                stage = 'verify'
                image.verify()
                return mime, dimensions
    except ImageValidationError:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise failure('decompression_bomb') from None
    except MemoryError:
        raise failure('memory_error') from None
    except UnidentifiedImageError:
        raise failure('invalid_image_header') from None
    except (OSError, SyntaxError, ValueError):
        raise failure('image_verification_failed' if stage == 'verify' else 'invalid_image_header') from None
    except Exception:
        raise failure('image_validation_failed') from None


def video_info(source):
    """Bounded MP4 box validation, not codec decoding or a playback guarantee."""
    position = source.tell()
    try:
        source.seek(0, os.SEEK_END)
        total = source.tell()
        if not 1 <= total <= MAX_FILE:
            raise ValueError()
        found = {'ftyp': False, 'moov': False, 'mdat': False, 'video': False}
        count = 0
        brands = {b'isom', b'iso2', b'iso3', b'iso4', b'iso5', b'iso6',
                  b'mp41', b'mp42', b'avc1', b'dash', b'M4V ', b'MSNV'}

        def boxes(start, end, parents=()):
            nonlocal count
            if len(parents) > 6:
                raise ValueError()
            offset = start
            while offset < end:
                count += 1
                if count > 4096 or end - offset < 8:
                    raise ValueError()
                source.seek(offset)
                header = source.read(8)
                if len(header) != 8:
                    raise ValueError()
                size, kind, header_size = int.from_bytes(header[:4], 'big'), header[4:], 8
                if size == 1:
                    extended = source.read(8)
                    if len(extended) != 8:
                        raise ValueError()
                    size, header_size = int.from_bytes(extended, 'big'), 16
                elif size == 0:
                    size = end - offset
                if size < header_size or offset + size > end:
                    raise ValueError()
                payload, limit = offset + header_size, offset + size
                if kind == b'ftyp' and not parents:
                    length = limit - payload
                    if found['ftyp'] or not 8 <= length <= 4096 or length % 4:
                        raise ValueError()
                    source.seek(payload)
                    data = source.read(length)
                    declared = {data[:4], *(data[i:i + 4] for i in range(8, length, 4))}
                    if not declared & brands:
                        raise ValueError()
                    found['ftyp'] = True
                if kind == b'moov' and not parents:
                    if found['moov']:
                        raise ValueError()
                    found['moov'] = True
                if kind == b'mdat' and not parents and limit > payload:
                    found['mdat'] = True
                if kind in (b'moov', b'trak', b'mdia'):
                    boxes(payload, limit, parents + (kind,))
                elif kind == b'hdlr' and parents == (b'moov', b'trak', b'mdia'):
                    if limit - payload < 24:
                        raise ValueError()
                    source.seek(payload)
                    if source.read(12)[8:12] == b'vide':
                        found['video'] = True
                offset = limit

        boxes(0, total)
        if not all(found.values()):
            raise ValueError()
        return 'video/mp4'
    except (ValueError, OverflowError):
        raise WeixinError('invalid_or_unsupported_video') from None
    finally:
        source.seek(position)


def validate_plain(raw, ref):
    if not raw or len(raw) > MAX_FILE:
        raise WeixinError('invalid_media_size')
    if ref.get('expected_size') is not None and len(raw) != ref['expected_size']:
        raise WeixinError('media_size_mismatch')
    if ref.get('md5') and hashlib.md5(raw).hexdigest() != ref['md5'].lower():
        raise WeixinError('media_md5_mismatch')
    if ref['kind'] == 'image':
        return image_info(raw)[0]
    if ref['kind'] == 'video':
        return video_info(io.BytesIO(raw))
    if ref['kind'] == 'voice':
        if raw.startswith((b'#!SILK_V3', b'\x02#!SILK_V3')):
            return 'audio/silk'
        if raw.startswith(b'RIFF') and raw[8:12] == b'WAVE':
            return 'audio/wav'
        if raw.startswith(b'OggS'):
            return 'audio/ogg'
        if raw.startswith(b'ID3') or raw[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'):
            return 'audio/mpeg'
        if raw.startswith(b'#!AMR'):
            return 'audio/amr'
        raise WeixinError('unsupported_voice_encoding')
    if raw.startswith(b'%PDF-'):
        return 'application/pdf'
    if raw.startswith(b'PK\x03\x04'):
        return 'application/zip'
    try:
        raw.decode('utf-8')
        if b'\x00' not in raw:
            return 'text/plain'
    except UnicodeError:
        pass
    return 'application/octet-stream'


def extract_ref(item):
    kind = {2: 'image', 3: 'voice', 4: 'file', 5: 'video'}[item['type']]
    data = item.get(kind + '_item')
    if not isinstance(data, dict) or not isinstance(data.get('media'), dict):
        raise WeixinError('missing_media_reference')
    media = data['media']
    url = media.get('full_url')
    if not url:
        query = media.get('encrypt_query_param')
        if not isinstance(query, str) or not query or len(query) > 12000:
            raise WeixinError('missing_media_reference')
        url = 'https://novac2c.cdn.weixin.qq.com/c2c/download?' + urlencode({'encrypted_query_param': query})
    cdn_parts(url)
    ref = {'kind': kind, 'url': url, 'key': media.get('aes_key')}
    if kind == 'image' and data.get('aeskey'):
        ref['hex_key'] = data['aeskey']
    if ref.get('key') or ref.get('hex_key'):
        key_bytes(ref)
    elif kind != 'image':
        raise WeixinError('missing_media_key')
    if kind == 'file' and data.get('len') is not None:
        try:
            length = int(data['len'])
            if isinstance(data['len'], bool) or length < 0 or str(length) != str(data['len']):
                raise ValueError()
            if length > MAX_FILE:
                raise WeixinError('media_too_large')
            ref['expected_size'] = length
        except (ValueError, TypeError):
            raise WeixinError('invalid_media_size') from None
    if kind == 'file' and data.get('md5'):
        if not isinstance(data['md5'], str) or not re.fullmatch(r'[a-fA-F0-9]{32}', data['md5']):
            raise WeixinError('invalid_media_md5')
        ref['md5'] = data['md5']
    return ref


class MediaStore:
    def __init__(self, probe, downloader=None):
        self.probe, self.db, self.lock, self.clock = probe, probe.db, probe.lock, probe.clock
        self.download = downloader or download_cdn
        self.directory = Path(probe.directory) / 'attachments'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.next_cleanup = 0
        # A cache hit is valid only while the opened file's identity and change
        # timestamps still match the bytes verified against the database hash.
        self._verified_chunks = {}
        self.db.executescript('''CREATE TABLE IF NOT EXISTS weixin_attachments (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL, account TEXT NOT NULL, peer TEXT NOT NULL,
            kind TEXT NOT NULL, filename TEXT NOT NULL, ref_json TEXT,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
            size INTEGER, mime_type TEXT, sha256 TEXT, error_kind TEXT,
            created_at REAL NOT NULL, expires_at REAL NOT NULL, provenance TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS attachment_message ON weixin_attachments(message_id);
        ''')
        self.db.commit()
        # Voice policy: only Tencent's upstream transcript, no eager raw audio.
        # Preserve prior cached user samples until ordinary retention expiry.
        with self.lock, self.db:
            for row in self.db.execute("SELECT id,provenance FROM weixin_attachments WHERE kind='voice' AND status IN ('queued','downloading')").fetchall():
                provenance = json.loads(row['provenance'])
                has_text = isinstance(provenance.get('upstream_transcript'), str) and bool(provenance['upstream_transcript'].strip())
                self.db.execute("UPDATE weixin_attachments SET status=?,error_kind=?,ref_json=NULL WHERE id=?",
                    ('transcript_only' if has_text else 'transcript_unavailable', None if has_text else 'no_upstream_transcript', row['id']))

    def add(self, message_id, account, peer, items):
        """Called inside owner-filtered inbox transaction; never commits by itself."""
        count = 0
        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get('type') not in (2, 3, 4, 5):
                continue
            count += 1
            if count > 8:
                break
            kind = {2: 'image', 3: 'voice', 4: 'file', 5: 'video'}[item['type']]
            data = item.get(kind + '_item')
            data = data if isinstance(data, dict) else {}
            name = safe_name(data.get('file_name'), kind + ('.silk' if kind == 'voice' else '.bin'))
            provenance = {}
            if kind == 'voice':
                for key in ('encode_type', 'sample_rate', 'bits_per_sample', 'playtime'):
                    if type(data.get(key)) is int and 0 <= data[key] <= 86400000:
                        provenance[key] = data[key]
                transcript = data.get('text')
                if isinstance(transcript, str) and len(transcript.encode()) <= 65536:
                    provenance['upstream_transcript'] = transcript
                    provenance['transcript_source'] = 'weixin_upstream'
            aid = 'att_' + hashlib.sha256(encoded([message_id, index]).encode()).hexdigest()[:32]
            status, error, ref = 'queued', None, None
            if kind == 'voice':
                transcript = provenance.get('upstream_transcript')
                has_text = isinstance(transcript, str) and bool(transcript.strip())
                status = 'transcript_only' if has_text else 'transcript_unavailable'
                error = None if has_text else 'no_upstream_transcript'
            else:
                try:
                    ref = extract_ref(item)
                except WeixinError as exc:
                    status, error = 'error', exc.kind
            if status == 'queued' and self.db.execute("SELECT count(*) FROM weixin_attachments WHERE status IN ('queued','downloading')").fetchone()[0] >= 32:
                status, error, ref = 'error', 'media_queue_busy', None
            now = self.clock()
            self.db.execute('''INSERT OR IGNORE INTO weixin_attachments
                (id,message_id,account,peer,kind,filename,ref_json,status,error_kind,created_at,expires_at,provenance)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (aid, message_id, account, peer, kind, name, encoded(ref) if ref else None,
                 status, error, now, now + RETENTION, encoded(provenance)))

    def recover_inflight(self):
        with self.lock, self.db:
            # CDN GET may safely retry; no message sends are performed here.
            self.db.execute("""UPDATE weixin_attachments SET status='error',
                error_kind='media_attempts_exhausted',ref_json=NULL
                WHERE status IN ('queued','downloading') AND attempts>=?""", (MAX_DOWNLOAD_ATTEMPTS,))
            self.db.execute("UPDATE weixin_attachments SET status='queued' WHERE status='downloading' AND attempts<?",
                            (MAX_DOWNLOAD_ATTEMPTS,))

    def cleanup(self):
        now = self.clock()
        if now < self.next_cleanup:
            return
        self.next_cleanup = now + 60
        with self.lock, self.db:
            rows = self.db.execute("SELECT id FROM weixin_attachments WHERE expires_at<=? AND status!='expired'", (now,)).fetchall()
            for row in rows:
                self.path(row['id']).unlink(missing_ok=True)
            self.db.execute("UPDATE weixin_attachments SET status='expired',ref_json=NULL WHERE expires_at<=?", (now,))
        # Remove only our own incomplete files left by interrupted writes.
        for path in self.directory.glob('att_*.part'):
            if now - path.stat().st_mtime > 3600:
                path.unlink(missing_ok=True)

    def path(self, aid):
        if not isinstance(aid, str) or not re.fullmatch(r'att_[a-f0-9]{32}', aid):
            raise WeixinError('invalid_attachment_id')
        return self.directory / aid

    def cache_usage(self):
        """Caller holds lock; outgoing reservations share the existing 128MiB budget."""
        total = self.db.execute("SELECT coalesce(sum(size),0) FROM weixin_attachments WHERE status='cached'").fetchone()[0]
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='weixin_uploads'").fetchone():
            total += self.db.execute("SELECT coalesce(sum(size),0) FROM weixin_uploads WHERE status IN ('uploading','ready','committed')").fetchone()[0]
        return total

    def process_once(self):
        self.cleanup()
        now = self.clock()
        with self.lock, self.db:
            # Also retire legacy/stale queued rows if a caller invokes the
            # worker without first running startup recovery.
            exhausted = self.db.execute("""UPDATE weixin_attachments SET status='error',
                error_kind='media_attempts_exhausted',ref_json=NULL
                WHERE status='queued' AND attempts>=?""", (MAX_DOWNLOAD_ATTEMPTS,)).rowcount
            row = self.db.execute("SELECT * FROM weixin_attachments WHERE status='queued' AND attempts<? AND next_at<=? ORDER BY created_at LIMIT 1",
                                  (MAX_DOWNLOAD_ATTEMPTS, now)).fetchone()
            if row:
                self.db.execute("UPDATE weixin_attachments SET status='downloading',attempts=attempts+1 WHERE id=?", (row['id'],))
        if exhausted:
            self.probe.notify_events()
        if not row:
            return bool(exhausted)
        temp = self.path(row['id']).with_suffix('.part')
        try:
            ref = json.loads(row['ref_json'])
            raw = decrypt(self.download(ref['url']), ref)
            mime = validate_plain(raw, ref)
            with self.lock, self.db:
                if self.cache_usage() + len(raw) > MAX_CACHE:
                    raise WeixinError('media_cache_full')
                fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    output.write(raw); output.flush(); os.fsync(output.fileno())
                os.replace(temp, self.path(row['id']))
                filename = row['filename']
                if row['kind'] == 'video' and Path(filename).suffix.lower() not in ('.mp4', '.m4v'):
                    filename = 'video.mp4'
                self.db.execute("UPDATE weixin_attachments SET status='cached',size=?,mime_type=?,sha256=?,filename=?,error_kind=NULL,ref_json=NULL WHERE id=?",
                                (len(raw), mime, hashlib.sha256(raw).hexdigest(), filename, row['id']))
        except Exception as exc:
            kind = exc.kind if isinstance(exc, WeixinError) else 'media_processing_error'
            retry = kind in ('media_timeout', 'media_network_error') and row['attempts'] + 1 < MAX_DOWNLOAD_ATTEMPTS
            diagnostic_provenance = None
            if row['kind'] == 'image' and isinstance(exc, ImageValidationError):
                try:
                    provenance = json.loads(row['provenance'])
                    if isinstance(provenance, dict):
                        provenance['image_validation'] = exc.diagnostic
                        diagnostic_provenance = encoded(provenance)
                except (ValueError, TypeError):
                    # Diagnostics must not turn an existing terminal failure
                    # into a stuck downloading row or repair unrelated metadata.
                    pass
            with self.lock, self.db:
                self.db.execute("UPDATE weixin_attachments SET status=?,next_at=?,error_kind=?,ref_json=CASE WHEN ? THEN ref_json ELSE NULL END,provenance=coalesce(?,provenance) WHERE id=?",
                                ('queued' if retry else 'error', now + 5 * (row['attempts'] + 1), kind, retry, diagnostic_provenance, row['id']))
        finally:
            temp.unlink(missing_ok=True)
        # Success or terminal error can release the parent event. Wake only
        # after the attachment transaction is committed.
        self.probe.notify_events()
        return True

    def metadata(self, row):
        return {'attachment_id': row['id'], 'message_id': row['message_id'], 'kind': row['kind'],
                'filename': row['filename'], 'status': row['status'], 'size': row['size'],
                'mime_type': row['mime_type'], 'sha256': row['sha256'], 'error_kind': row['error_kind'],
                'expires_at': self.probe.iso(row['expires_at']),
                **({'voice_policy': 'weixin_upstream_only', 'audio_downloaded': row['status'] == 'cached'} if row['kind'] == 'voice' else {}),
                **json.loads(row['provenance'])}

    def list_for_message(self, mid):
        with self.lock:
            rows = self.db.execute('SELECT * FROM weixin_attachments WHERE message_id=? ORDER BY rowid', (mid,)).fetchall()
        return [self.metadata(row) for row in rows]

    def authorized(self, mid, aid):
        account = self.probe.weixin.account()
        self.path(aid)
        if not account:
            raise WeixinError('not_bound')
        with self.lock:
            row = self.db.execute('''SELECT a.* FROM weixin_attachments a JOIN weixin_inbox i ON i.id=a.message_id
                WHERE a.id=? AND a.message_id=? AND a.account=? AND a.peer=? AND i.account=a.account AND i.peer=a.peer''',
                (aid, mid, account['id'], account['owner'])).fetchone()
        if not row:
            raise WeixinError('unknown_owner_attachment')
        if row['expires_at'] <= self.clock():
            raise WeixinError('attachment_expired')
        if row['status'] != 'cached':
            raise WeixinError('attachment_not_cached')
        return row

    def _read_verified(self, mid, aid, offset, length, maximum, include_block=False):
        if type(offset) is not int or type(length) is not int or offset < 0 or not 1 <= length <= maximum:
            raise WeixinError('invalid_attachment_range')
        row = self.authorized(mid, aid)
        if type(row['size']) is not int or not 0 <= row['size'] <= MAX_FILE or offset > row['size']:
            raise WeixinError('invalid_attachment_range')
        try:
            fd = os.open(self.path(aid), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                def identity():
                    stat = os.fstat(source.fileno())
                    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise WeixinError('attachment_integrity_error')
                fingerprint = identity()
                if fingerprint[2] != row['size'] or fingerprint[2] > MAX_FILE:
                    raise WeixinError('attachment_integrity_error')
                with self.lock:
                    verified = self._verified_chunks.get(aid) == (row['sha256'], fingerprint)
                if not verified:
                    digest = hashlib.sha256()
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(block)
                    if identity() != fingerprint or digest.hexdigest() != row['sha256']:
                        raise WeixinError('attachment_integrity_error')
                    with self.lock:
                        if len(self._verified_chunks) >= 128:
                            self._verified_chunks.pop(next(iter(self._verified_chunks)))
                        self._verified_chunks[aid] = (row['sha256'], fingerprint)
                source.seek(offset)
                expected = min(length, row['size'] - offset)
                raw = source.read(expected)
                if len(raw) != expected or identity() != fingerprint:
                    raise WeixinError('attachment_integrity_error')
        except OSError:
            raise WeixinError('attachment_cache_unavailable') from None
        current = self.authorized(mid, aid)
        identity_fields = ('account', 'peer', 'message_id', 'size', 'sha256', 'expires_at')
        if tuple(current[key] for key in identity_fields) != tuple(row[key] for key in identity_fields):
            raise WeixinError('attachment_integrity_error')
        result = {'message_id': mid, 'attachment_id': aid, 'offset': offset, 'size': len(raw),
                'total_size': row['size'], 'mime_type': row['mime_type'], 'sha256': row['sha256'],
                'data_base64': base64.b64encode(raw).decode(), 'eof': offset + len(raw) == row['size'],
                'filename': row['filename'], 'expires_at': self.probe.iso(row['expires_at'])}
        if include_block:
            result.update(transfer_version='attachment-block-v1',
                          block_sha256=hashlib.sha256(raw).hexdigest(),
                          next_offset=offset + len(raw), max_raw_bytes=MAX_BLOCK)
        return result

    def read_chunk(self, mid, aid, offset, length):
        return self._read_verified(mid, aid, offset, length, MAX_CHUNK)

    def read_block(self, mid, aid, offset, length):
        return self._read_verified(mid, aid, offset, length, MAX_BLOCK, include_block=True)

    def _read_image_verified_bytes(self, mid, aid):
        """Bounded original bytes with the chunk reader's FD/binding rules.

        This internal path does not Base64-encode the original or create a new
        cache. The opened descriptor must be regular, its identity must remain
        stable across the read, and its bytes must match the retained hash.
        """
        row = self.authorized(mid, aid)
        if row['kind'] != 'image':
            raise WeixinError('attachment_is_not_image')
        if type(row['size']) is not int or not 1 <= row['size'] <= MAX_FILE:
            raise WeixinError('attachment_integrity_error')
        try:
            fd = os.open(self.path(aid), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                def identity():
                    info = os.fstat(source.fileno())
                    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise WeixinError('attachment_integrity_error')
                fingerprint = identity()
                if fingerprint[2] != row['size']:
                    raise WeixinError('attachment_integrity_error')
                raw = source.read(row['size'])
                if (len(raw) != row['size'] or identity() != fingerprint
                        or hashlib.sha256(raw).hexdigest() != row['sha256']):
                    raise WeixinError('attachment_integrity_error')
        except OSError:
            raise WeixinError('attachment_cache_unavailable') from None
        self._recheck_image_binding(mid, aid, row)
        return row, raw

    def _recheck_image_binding(self, mid, aid, original):
        current = self.authorized(mid, aid)
        fields = ('account', 'peer', 'message_id', 'kind', 'size', 'sha256', 'expires_at')
        if tuple(current[k] for k in fields) != tuple(original[k] for k in fields):
            raise WeixinError('attachment_integrity_error')

    def read_image(self, mid, aid, page=None):
        """One lossy overview (legacy default) or one clear source region.

        Paging retains the 8M-pixel/20MiB guards. Pillow still fully decodes the
        admitted original before EXIF transpose/region crop; this is not stream
        cropping and this process has no per-decode RSS budget.
        """
        if page is not None and (type(page) is not int or page < 0):
            raise WeixinError('invalid_image_page')
        row, raw = self._read_image_verified_bytes(mid, aid)
        # Revalidate cached/legacy files before any full decode; a crop must
        # never be used to bypass the original-size or bomb protections.
        _, dimensions = image_info(raw)
        _, _, header_count = _image_page_grid(*dimensions)
        if page is not None:
            if header_count > MAX_IMAGE_PAGES:
                raise WeixinError('page_limit_error')
            if page >= header_count:
                raise WeixinError('image_page_out_of_range')
        from PIL import Image, ImageOps
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with io.BytesIO(raw) as input_file, Image.open(input_file) as source:
                    if source.format not in IMAGE_FORMATS or source.width * source.height > MAX_PIXELS:
                        raise WeixinError('invalid_or_oversize_image')
                    with contextlib.ExitStack() as resources:
                        # Pillow's image context closes the file; explicitly
                        # close each decoded raster instead of relying on GC.
                        resources.callback(source.close)
                        oriented = ImageOps.exif_transpose(source)
                        resources.callback(oriented.close)
                        full = oriented.convert('RGB')
                        resources.callback(full.close)
                        source_dimensions = list(full.size)
                        columns, rows, count = _image_page_grid(*full.size)
                        pagination_available = count <= MAX_IMAGE_PAGES
                        if page is None:
                            source_box = [0, 0, *source_dimensions]
                            full.thumbnail((IMAGE_PAGE_SIDE, IMAGE_PAGE_SIDE))
                            image = full
                        else:
                            if not pagination_available:
                                raise WeixinError('page_limit_error')
                            if page >= count:
                                raise WeixinError('image_page_out_of_range')
                            stride = IMAGE_PAGE_SIDE - IMAGE_PAGE_OVERLAP
                            left, top = (page % columns) * stride, (page // columns) * stride
                            source_box = [left, top, min(left + IMAGE_PAGE_SIDE, full.width),
                                          min(top + IMAGE_PAGE_SIDE, full.height)]
                            image = full.crop(tuple(source_box))
                            resources.callback(image.close)
                        preview_dimensions = list(image.size)
                        output = resources.enter_context(_PreviewBuffer())
                        image.save(output, 'JPEG', quality=85)
                        preview = output.getvalue()
        except WeixinError:
            raise
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise WeixinError('invalid_or_oversize_image') from None
        except MemoryError:
            raise WeixinError('image_preview_resource_error') from None
        except Exception:
            raise WeixinError('image_preview_failed') from None
        if len(preview) > MAX_IMAGE_PREVIEW_BYTES:
            raise WeixinError('image_preview_too_large')
        # Decode/encode may take time. Recheck the exact current owner, TTL and
        # retained identity before returning any image body to the caller.
        self._recheck_image_binding(mid, aid, row)
        metadata = {**self.metadata(row), 'preview': True, 'preview_mime_type': 'image/jpeg',
            'preview_mode': 'overview' if page is None else 'region',
            'source_dimensions': source_dimensions, 'preview_dimensions': preview_dimensions,
            'page_index': page, 'page_count': count if pagination_available else None,
            'page_grid': {'columns': columns, 'rows': rows} if pagination_available else None,
            'source_box': source_box, 'overlap': 0 if page is None else IMAGE_PAGE_OVERLAP,
            'pagination_available': pagination_available, 'max_pages': MAX_IMAGE_PAGES,
            'original_sha256': row['sha256'], 'preview_sha256': hashlib.sha256(preview).hexdigest(),
            'is_original': False, 'is_complete_source': False, 'lossy_preview': True,
            'covers_entire_source': page is None, 'full_decode_required': True,
            'rss_budget_enforced': False, 'first_frame_only': True}
        return metadata, {'type': 'image', 'mimeType': 'image/jpeg', 'data': base64.b64encode(preview).decode()}
