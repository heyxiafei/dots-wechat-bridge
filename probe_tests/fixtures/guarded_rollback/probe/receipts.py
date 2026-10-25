"""Opt-in owner transport receipts, independent of model/webhook execution."""
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import uuid

try:
    from .weixin import WeixinError, encoded
except ImportError:
    from weixin import WeixinError, encoded

RECEIPT_TEXT = '已收到。（系统收件确认）'
_PENDING = "('queued','preparing','sending','context_required','auth_required')"


class OwnerReceiptStore:
    def __init__(self, probe):
        self.probe, self.db, self.lock, self.clock = probe, probe.db, probe.lock, probe.clock
        self.running, self.activation_id = False, None
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS owner_receipt_activations (
                id TEXT PRIMARY KEY,account TEXT NOT NULL,owner TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active','disabled')),
                activated_at REAL NOT NULL,grace_ms INTEGER NOT NULL,
                baseline_count INTEGER NOT NULL,baseline_hash TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS owner_receipt_one_active
                ON owner_receipt_activations(state) WHERE state='active';
            CREATE TABLE IF NOT EXISTS owner_receipt_baseline (
                activation_id TEXT NOT NULL,message_id TEXT NOT NULL,
                PRIMARY KEY(activation_id,message_id));
            CREATE TABLE IF NOT EXISTS owner_receipt_intents (
                message_id TEXT PRIMARY KEY,activation_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('waiting','queued','suppressed','existing','cancelled')),
                observed_at REAL NOT NULL,due_at REAL NOT NULL,next_check_at REAL NOT NULL,
                updated_at REAL NOT NULL,decision TEXT NOT NULL);
        ''')
        self.db.commit()

    def _owner(self):
        rows = self.db.execute('SELECT id,owner FROM weixin_accounts LIMIT 2').fetchall()
        if len(rows) != 1:
            raise WeixinError('receipt_owner_unavailable')
        return rows[0]

    def _config(self):
        path = Path(self.probe.directory) / 'owner-receipts.json'
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise WeixinError('unsafe_receipt_policy') from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                raise WeixinError('unsafe_receipt_policy')
            data = os.read(fd, 4097)
            if len(data) > 4096:
                raise WeixinError('unsafe_receipt_policy')
            def pairs(items):
                result = {}
                for k, v in items:
                    if k in result:
                        raise ValueError()
                    result[k] = v
                return result
            config = json.loads(data, object_pairs_hook=pairs)
            if (not isinstance(config, dict) or set(config) != {'version','enabled','activation_id','grace_ms'}
                    or type(config['version']) is not int or config['version'] != 1
                    or type(config['enabled']) is not bool
                    or type(config['grace_ms']) is not int or not 0 <= config['grace_ms'] <= 1000
                    or not isinstance(config['activation_id'], str)
                    or str(uuid.UUID(config['activation_id'])) != config['activation_id']):
                raise ValueError()
            return config
        except (ValueError, TypeError, UnicodeError):
            raise WeixinError('unsafe_receipt_policy') from None
        finally:
            os.close(fd)

    def _disable(self):
        self.db.execute("UPDATE owner_receipt_intents SET state='cancelled',decision='policy_disabled',updated_at=? WHERE state='waiting'", (self.clock(),))
        # Exact common ACK identity and origin fence: never touch model ACKs,
        # finals, POSTs in flight, or attempts whose outcome may be unknown.
        self.db.execute("""UPDATE weixin_outbox SET status='cancelled',error_kind='receipt_policy_disabled'
            WHERE kind='reply_ack' AND ack_origin='system_receipt'
                AND status IN ('queued','context_required','auth_required') AND attempts=0
            AND source_message_id IN (SELECT message_id FROM owner_receipt_intents)""")
        self.db.execute("""UPDATE owner_receipt_intents SET state='cancelled',decision='policy_disabled',updated_at=?
            WHERE state='queued' AND message_id IN (SELECT source_message_id FROM weixin_outbox
                WHERE kind='reply_ack' AND ack_origin='system_receipt' AND status='cancelled'
                    AND error_kind='receipt_policy_disabled')""", (self.clock(),))
        self.db.execute("UPDATE owner_receipt_activations SET state='disabled' WHERE state='active'")

    def _unsafe(self, mid, owner):
        if self.db.execute("SELECT 1 FROM weixin_outbox WHERE account=? AND peer=? AND status='delivery_unknown' AND (source_message_id=? OR message_id=?)",
                           (owner['id'], owner['owner'], mid, mid)).fetchone():
            return True
        return bool(self.db.execute("SELECT 1 FROM weixin_dot_mirrors WHERE message_id=? AND phase IN ('ack','final') AND state IN ('prepared','unknown')", (mid,)).fetchone())

    def start_policy(self):
        self.running = False
        try:
            config = self._config()
        except WeixinError:
            with self.lock, self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self._disable()
            raise
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if config is None or not config['enabled']:
                self._disable()
                self.activation_id = None
            else:
                owner = self._owner()
                rows = self.db.execute('SELECT * FROM owner_receipt_activations').fetchall()
                if any((r['account'], r['owner']) != (owner['id'], owner['owner']) for r in rows):
                    raise WeixinError('receipt_binding_changed')
                old = next((r for r in rows if r['id'] == config['activation_id']), None)
                if old:
                    latest = self.db.execute('SELECT id FROM owner_receipt_activations ORDER BY rowid DESC LIMIT 1').fetchone()
                    if latest['id'] != old['id']:
                        raise WeixinError('receipt_activation_retired')
                    if old['grace_ms'] != config['grace_ms']:
                        raise WeixinError('receipt_activation_config_changed')
                else:
                    if (self.db.execute("SELECT 1 FROM owner_receipt_intents WHERE state='waiting'").fetchone()
                            or self.db.execute("SELECT 1 FROM weixin_outbox WHERE account=? AND peer=? AND (status IN ('sending','delivery_unknown') OR (ack_origin='system_receipt' AND status IN ('queued','preparing','context_required','auth_required')))", (owner['id'], owner['owner'])).fetchone()
                            or self.db.execute("SELECT 1 FROM weixin_dot_mirrors m JOIN weixin_inbox i ON i.id=m.message_id WHERE i.account=? AND i.peer=? AND m.state IN ('prepared','unknown')", (owner['id'], owner['owner'])).fetchone()):
                        raise WeixinError('receipt_activation_not_quiescent')
                    self.db.execute("UPDATE owner_receipt_activations SET state='disabled' WHERE state='active'")
                    ids = [r['id'] for r in self.db.execute('SELECT id FROM weixin_inbox WHERE account=? AND peer=? ORDER BY id', (owner['id'], owner['owner']))]
                    digest = hashlib.sha256(encoded(ids).encode()).hexdigest()
                    self.db.execute("INSERT INTO owner_receipt_activations VALUES(?,?,?,'active',?,?,?,?)",
                                    (config['activation_id'], owner['id'], owner['owner'], self.clock(), config['grace_ms'], len(ids), digest))
                    self.db.executemany('INSERT INTO owner_receipt_baseline VALUES(?,?)',
                                        ((config['activation_id'], mid) for mid in ids))
                self.db.execute("UPDATE owner_receipt_activations SET state='disabled' WHERE state='active' AND id!=?", (config['activation_id'],))
                self.db.execute("UPDATE owner_receipt_activations SET state='active' WHERE id=?", (config['activation_id'],))
                self.activation_id = config['activation_id']
        self.running = bool(config and config['enabled'])
        return self.status()

    def _active(self):
        if not self.running or self.activation_id is None:
            return None
        owner = self._owner()
        return self.db.execute("SELECT * FROM owner_receipt_activations WHERE id=? AND account=? AND owner=? AND state='active'",
                               (self.activation_id, owner['id'], owner['owner'])).fetchone()

    def observe_new_in_transaction(self, message_id):
        # The ingest caller alone establishes INSERT changed==1 and owns commit.
        if not self.db.in_transaction:
            raise WeixinError('receipt_observe_requires_ingest_transaction')
        with self.lock:
            active = self._active()
            if not active:
                return False
            if not self.db.execute('SELECT 1 FROM weixin_inbox WHERE id=? AND account=? AND peer=?',
                                   (message_id, active['account'], active['owner'])).fetchone():
                return False
            if self.db.execute('SELECT 1 FROM owner_receipt_baseline WHERE message_id=?', (message_id,)).fetchone():
                return False
            now = self.clock()
            due = now + active['grace_ms'] / 1000
            return self.db.execute("INSERT OR IGNORE INTO owner_receipt_intents VALUES(?,?,'waiting',?,?,?,?,'grace')",
                                   (message_id, active['id'], now, due, due, now)).rowcount == 1

    def materialize_due(self):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            active = self._active()
            if not active:
                return False
            now = self.clock()
            row = self.db.execute("SELECT * FROM owner_receipt_intents WHERE activation_id=? AND state='waiting' AND next_check_at<=? ORDER BY due_at,rowid LIMIT 1",
                                  (active['id'], now)).fetchone()
            if not row:
                return False
            mid = row['message_id']
            owned = self.db.execute('SELECT 1 FROM weixin_inbox WHERE id=? AND account=? AND peer=?', (mid, active['account'], active['owner'])).fetchone()
            ack = self.probe.weixin.ack_send_id(mid)
            state, decision = None, None
            if not owned:
                state, decision = 'cancelled', 'owner_message_missing'
            elif self.db.execute("SELECT 1 FROM weixin_outbox WHERE message_id=?", (mid,)).fetchone():
                state, decision = 'suppressed', 'final_already_committed'
            elif self.db.execute('SELECT 1 FROM weixin_outbox WHERE message_id=?', (ack,)).fetchone():
                state, decision = 'existing', 'ack_already_committed'
            elif self._unsafe(mid, {'id': active['account'], 'owner': active['owner']}):
                decision = 'uncertain_source'
            elif self.db.execute('SELECT count(*) FROM weixin_outbox WHERE status IN ' + _PENDING).fetchone()[0] >= 32:
                decision = 'queue_full'
            else:
                self.db.execute("""INSERT INTO weixin_outbox
                    (message_id,account,peer,text,client_id,status,created_at,kind,source_message_id,ack_origin)
                    VALUES(?,?,?,?,?,'queued',?,'reply_ack',?,'system_receipt')""",
                    (ack, active['account'], active['owner'], RECEIPT_TEXT, 'dots_' + ack, now, mid))
                state, decision = 'queued', 'system_receipt_committed'
            if state:
                self.db.execute('UPDATE owner_receipt_intents SET state=?,decision=?,updated_at=? WHERE message_id=?', (state, decision, now, mid))
            else:
                self.db.execute('UPDATE owner_receipt_intents SET decision=?,next_check_at=?,updated_at=? WHERE message_id=?', (decision, now + .5, now, mid))
            return state is not None

    def next_wait(self, maximum=.5):
        if type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum <= 0:
            raise ValueError('maximum must be positive and finite')
        with self.lock:
            active = self._active()
            if not active:
                return maximum
            row = self.db.execute("SELECT min(next_check_at) FROM owner_receipt_intents WHERE activation_id=? AND state='waiting'", (active['id'],)).fetchone()
            return maximum if row[0] is None else min(maximum, max(.01, row[0] - self.clock()))

    def pending_receipt(self):
        with self.lock:
            active = self._active()
            if not active:
                return False
            row = self.db.execute("SELECT message_id,decision FROM owner_receipt_intents WHERE activation_id=? AND state='waiting' ORDER BY due_at,rowid LIMIT 1", (active['id'],)).fetchone()
            if not row or row['decision'] in ('queue_full','uncertain_source'):
                return False
            return self.db.execute('SELECT count(*) FROM weixin_outbox WHERE status IN ' + _PENDING).fetchone()[0] < 32

    def for_message(self, message_id):
        with self.lock:
            owner = self._owner()
            if not self.db.execute('SELECT 1 FROM weixin_inbox WHERE id=? AND account=? AND peer=?', (message_id, owner['id'], owner['owner'])).fetchone():
                raise WeixinError('unknown_owner_message')
            row = self.db.execute('SELECT state,observed_at,due_at,updated_at,decision FROM owner_receipt_intents WHERE message_id=?', (message_id,)).fetchone()
            ack = self.db.execute('SELECT status,ack_origin,attempts,accepted_at FROM weixin_outbox WHERE message_id=?', (self.probe.weixin.ack_send_id(message_id),)).fetchone()
            return {'intent': dict(row) if row else None, 'ack': dict(ack) if ack else None,
                    'baseline': bool(self.db.execute('SELECT 1 FROM owner_receipt_baseline WHERE message_id=?', (message_id,)).fetchone()),
                    'receipt_basis': 'durable_local_ingest', 'dot_processing_confirmed': False,
                    'phone_delivery_confirmed': False}

    def status(self):
        with self.lock:
            row = self.db.execute('SELECT id,activated_at,grace_ms,baseline_count,baseline_hash,state FROM owner_receipt_activations WHERE id=?', (self.activation_id,)).fetchone()
            counts = {r['state']: r['n'] for r in self.db.execute('SELECT state,count(*) n FROM owner_receipt_intents GROUP BY state')}
            return {'running': self.running, 'activation': dict(row) if row else None,
                    'counts': counts, 'receipt_basis': 'durable_local_ingest',
                    'dot_processing_confirmed': False, 'phone_delivery_confirmed': False}
