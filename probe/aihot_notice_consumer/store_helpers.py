"""Reviewed monitor helpers, with a separate hardened lock; no network imports."""

import contextlib

import datetime as dt

import fcntl

import json

import os

import re
import stat

import tempfile

class Invalid(Exception):
    pass

def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Invalid('duplicate_json_key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(Invalid('nonfinite')))

def atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

@contextlib.contextmanager
def lock(data):
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = data.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise Invalid('private_lock_directory')
    fd = os.open(data / 'dual-notifications.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise Invalid('private_lock_file')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

def event_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value):
        raise Invalid('event_id')
    return value

def notification_text(stage, source_at, source):
    beijing = dt.datetime.fromtimestamp(source_at, dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    if stage == 'announced':
        return 'AIHOT：Codex 新重置预告。\n来源发布时间：' + beijing + '（北京时间）。\n具体生效以官方确认及账号实际为准。\n' + source
    return 'AIHOT：Codex 新重置确认（来源公告）。\n来源发布时间：' + beijing + '（北京时间）。\n个人账号到账及适用范围请以账号实际为准。\n' + source
