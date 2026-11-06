"""Private task/routing ledger, separate from the existing Weixin inbox/outbox.

No Weixin credentials, poller, sender, network or model are constructed here.
The lifetime flock excludes a second router using this same ledger. It is not a
claim that another directory or an old machine cannot consume the same Bot.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import uuid

from .routing import valid_name


class FoundationStore:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._mutex = threading.RLock()
        self._lock = None
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("unsafe_state_path")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".router.lock")
        if lock_path.is_symlink():
            raise ValueError("unsafe_state_path")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        self._lock = os.fdopen(fd, "r+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            self._lock = None
            raise RuntimeError("router_already_active") from None
        try:
            if not self.path.exists():
                dbfd = os.open(self.path, flags | os.O_EXCL, 0o600)
                os.close(dbfd)
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("private_state_required")
            with self._db() as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                expected = {'metadata', 'route', 'generations', 'sessions', 'tasks', 'callback_events'}
                if tables and tables != expected:
                    # A demo/optional router must never initialize tables inside
                    # an existing Weixin or unrelated production database.
                    raise ValueError('foreign_state_database')
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata(version INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS route(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        backend_id TEXT NOT NULL, model TEXT, revision INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS generations(conversation_id TEXT PRIMARY KEY, generation INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS sessions(session_key TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL, backend_id TEXT NOT NULL, model_key TEXT NOT NULL,
                        generation INTEGER NOT NULL, backend_session TEXT,
                        UNIQUE(conversation_id,backend_id,model_key,generation));
                    CREATE TABLE IF NOT EXISTS tasks(request_id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE,
                        conversation_id TEXT NOT NULL, backend_id TEXT NOT NULL, model TEXT,
                        config_revision INTEGER NOT NULL, session_key TEXT NOT NULL, generation INTEGER NOT NULL,
                        source_digest TEXT NOT NULL, deadline_at REAL NOT NULL,
                        backend_status TEXT NOT NULL, backend_task_id TEXT,
                        result_text TEXT, error_code TEXT, usage_json TEXT NOT NULL DEFAULT '{}',
                        send_status TEXT NOT NULL, created_at REAL NOT NULL, finished_at REAL,
                        cancel_status TEXT NOT NULL DEFAULT 'not_requested');
                    CREATE TABLE IF NOT EXISTS callback_events(event_id TEXT PRIMARY KEY,
                        request_id TEXT NOT NULL, backend_id TEXT NOT NULL, task_id TEXT NOT NULL);
                """)
                versions = db.execute("SELECT version FROM metadata").fetchall()
                if not versions:
                    db.execute("INSERT INTO metadata VALUES(1)")
                elif len(versions) != 1 or versions[0][0] != 1:
                    raise ValueError("unsupported_state_version")
                db.execute("INSERT OR IGNORE INTO route VALUES(1,'dot',NULL,1)")
                # This happens only while holding the lifetime router lock.
                db.execute("UPDATE tasks SET backend_status='unknown',error_code='router_interrupted' WHERE backend_status='running'")
                db.execute("UPDATE tasks SET send_status='unknown' WHERE send_status='sending'")
                db.execute("UPDATE tasks SET cancel_status='unknown' WHERE cancel_status='requested'")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._lock is not None:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _db(self):
        if self._lock is None:
            raise RuntimeError("store_closed")
        with self._mutex:
            db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def route(self):
        with self._db() as db:
            return dict(db.execute("SELECT backend_id,model,revision FROM route WHERE singleton=1").fetchone())

    def set_default(self, backend_id, model=None):
        if not valid_name(backend_id) or model is not None and not valid_name(model):
            raise ValueError("invalid_route")
        with self._db() as db:
            db.execute("UPDATE route SET backend_id=?,model=?,revision=revision+1 WHERE singleton=1", (backend_id, model))
            return dict(db.execute("SELECT backend_id,model,revision FROM route WHERE singleton=1").fetchone())

    def new_session(self, conversation_id):
        if not isinstance(conversation_id, str) or not conversation_id or len(conversation_id) > 256:
            raise ValueError("invalid_conversation")
        with self._db() as db:
            db.execute("INSERT INTO generations VALUES(?,1) ON CONFLICT(conversation_id) DO UPDATE SET generation=generation+1", (conversation_id,))
            return db.execute("SELECT generation FROM generations WHERE conversation_id=?", (conversation_id,)).fetchone()[0]

    def claim(self, message_id, conversation_id, source_digest, deadline_at, *, management=False):
        """Atomically freeze the route. Repeated original IDs never restart work."""
        with self._db() as db:
            old = db.execute("SELECT * FROM tasks WHERE message_id=?", (message_id,)).fetchone()
            if old:
                return dict(old), False
            route = db.execute("SELECT * FROM route WHERE singleton=1").fetchone()
            gen = db.execute("SELECT generation FROM generations WHERE conversation_id=?", (conversation_id,)).fetchone()
            generation = gen[0] if gen else 0
            model_key = route['model'] or ''
            session = db.execute("SELECT * FROM sessions WHERE conversation_id=? AND backend_id=? AND model_key=? AND generation=?",
                                 (conversation_id, route['backend_id'], model_key, generation)).fetchone()
            if not session:
                session_key = 'session_' + uuid.uuid4().hex
                db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,NULL)", (session_key, conversation_id, route['backend_id'], model_key, generation))
            else:
                session_key = session['session_key']
            # Only one unfinished job per backend/conversation lane. Read-only
            # default-route changes are not an excuse to migrate running work.
            busy = None if management else db.execute("SELECT 1 FROM tasks WHERE conversation_id=? AND backend_id=? AND backend_status IN ('running','pending','unknown') LIMIT 1",
                              (conversation_id, route['backend_id'])).fetchone()
            state, code = ('failed', 'backend_lane_busy') if busy else ('running', None)
            db.execute("""INSERT INTO tasks(request_id,message_id,conversation_id,backend_id,model,config_revision,
                session_key,generation,source_digest,deadline_at,backend_status,error_code,send_status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (message_id, message_id, conversation_id, route['backend_id'], route['model'], route['revision'],
                    session_key, generation, source_digest, deadline_at, state, code, 'not_started', self.clock()))
            return dict(db.execute("SELECT * FROM tasks WHERE message_id=?", (message_id,)).fetchone()), True

    def task(self, request_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def backend_session(self, session_key):
        with self._db() as db:
            row = db.execute("SELECT backend_session FROM sessions WHERE session_key=?", (session_key,)).fetchone()
            return row[0] if row else None

    def finish(self, request_id, result, *, callback=None, backend_id=None):
        """Only the exact admitted task may complete; callback identity is host data."""
        with self._db() as db:
            row = db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise ValueError("unknown_request")
            if result.request_id != request_id:
                raise ValueError("result_request_mismatch")
            if callback is not None:
                if backend_id != row['backend_id'] or callback.task_id != row['backend_task_id']:
                    raise ValueError("callback_binding_mismatch")
                old = db.execute("SELECT * FROM callback_events WHERE event_id=?", (callback.event_id,)).fetchone()
                if old:
                    if (old['request_id'], old['backend_id'], old['task_id']) != (request_id, backend_id, callback.task_id):
                        raise ValueError("callback_identity_conflict")
                    return dict(row), False
                db.execute("INSERT INTO callback_events VALUES(?,?,?,?)", (callback.event_id, request_id, backend_id, callback.task_id))
            if row['backend_status'] not in ('running', 'pending', 'unknown'):
                return dict(row), False
            if result.status == 'pending' and (not isinstance(result.task_id, str) or not result.task_id or len(result.task_id) > 256):
                raise ValueError("pending_task_identity_missing")
            if result.backend_session is not None and (not isinstance(result.backend_session, str) or not result.backend_session or len(result.backend_session) > 256):
                raise ValueError("invalid_backend_session")
            if result.text is not None and len(result.text.encode('utf-8')) > 65536:
                raise ValueError("result_too_large")
            if any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in result.usage.items()):
                raise ValueError("invalid_usage")
            db.execute("""UPDATE tasks SET backend_status=?,backend_task_id=COALESCE(?,backend_task_id),
                result_text=?,error_code=?,usage_json=?,finished_at=? WHERE request_id=?""",
                (result.status, result.task_id, result.text, result.error_code, json.dumps(dict(result.usage), sort_keys=True),
                 None if result.status == 'pending' else self.clock(), request_id))
            if result.status == 'succeeded' and result.backend_session is not None:
                db.execute("UPDATE sessions SET backend_session=? WHERE session_key=?", (result.backend_session, row['session_key']))
            return dict(db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()), True

    def claim_send(self, request_id):
        with self._db() as db:
            changed = db.execute("UPDATE tasks SET send_status='sending' WHERE request_id=? AND send_status='not_started'", (request_id,)).rowcount
            return changed == 1

    def mark_send(self, request_id, status):
        if status not in ('accepted', 'queued', 'rejected', 'unknown', 'external_committed', 'source_unverified', 'not_requested'):
            raise ValueError("invalid_send_status")
        with self._db() as db:
            db.execute("UPDATE tasks SET send_status=? WHERE request_id=? AND send_status IN ('not_started','sending')", (status, request_id))

    def claim_cancel(self, request_id):
        with self._db() as db:
            return db.execute("UPDATE tasks SET cancel_status='requested' WHERE request_id=? AND backend_status='pending' AND cancel_status='not_requested'", (request_id,)).rowcount == 1

    def mark_cancel(self, request_id, status):
        if status not in ('confirmed', 'unknown'):
            raise ValueError('invalid_cancel_status')
        with self._db() as db:
            db.execute("UPDATE tasks SET cancel_status=? WHERE request_id=? AND cancel_status='requested'", (status, request_id))

    def summary(self):
        """Only fixed state counts; no message body, credentials or session values."""
        with self._db() as db:
            states = {row[0]: row[1] for row in db.execute("SELECT backend_status,COUNT(*) FROM tasks GROUP BY backend_status")}
            sends = {row[0]: row[1] for row in db.execute("SELECT send_status,COUNT(*) FROM tasks GROUP BY send_status")}
            route = dict(db.execute("SELECT backend_id,model,revision FROM route WHERE singleton=1").fetchone())
            return {'schema_version': 1, 'route': route, 'task_states': states, 'send_states': sends}

    def overdue(self, now):
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks WHERE backend_status='pending' AND deadline_at<=?", (now,))]

    @staticmethod
    def read_summary(path):
        """Read-only CLI view, even while the existing router owns its lock."""
        path = Path(path)
        if not path.exists():
            return {'state': 'not_initialized'}
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('unsafe_state_path')
        db = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN')
            versions = db.execute('SELECT version FROM metadata').fetchall()
            if len(versions) != 1 or versions[0][0] != 1:
                raise ValueError('unsupported_state_version')
            return {'schema_version': 1,
                    'route': dict(db.execute('SELECT backend_id,model,revision FROM route WHERE singleton=1').fetchone()),
                    'task_states': {row[0]: row[1] for row in db.execute('SELECT backend_status,COUNT(*) FROM tasks GROUP BY backend_status')},
                    'send_states': {row[0]: row[1] for row in db.execute('SELECT send_status,COUNT(*) FROM tasks GROUP BY send_status')}}
        finally:
            db.close()
