"""Synthetic per-part intent tests. No remote writer or production fence proof."""
import copy
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from probe.probe import Probe
from probe.dotjournal import SOURCE, JOURNAL_VERSION, DotMirrorJournal, journal_tools
from probe.weixin import WeixinError, MESSAGE_EVENT


class SyntheticFenceVerifier:
    """Fixture-only adapter: never establishes real platform quiescence."""
    def verify(self, epoch_id, nonce, target_scope, proof_ref):
        return {'epoch_id': epoch_id, 'nonce': nonce, 'target_scope': target_scope,
                'old_writers_fenced': True, 'fence_persistent': True,
                'all_legacy_entrypoints_fenced': True, 'source_custody_complete': True,
                'inflight_count': 0, 'evidence_ref': 'synthetic-evidence-only'}


class DotJournalTests(unittest.TestCase):
    scope = 'synthetic-current-dot-room'
    target = 'Sentinel_' + '1' * 32

    def setUp(self):
        for target in ('socket.getaddrinfo', 'socket.create_connection',
                       'socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(target, side_effect=AssertionError('network forbidden in journal tests'))
            guard.start()
            self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.
        self.verifier = SyntheticFenceVerifier()
        self.p = self.open_probe()
        self.p.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
            'ilink_user_id': 'fixture-owner', 'bot_token': 'TOKEN_CANARY'})
        self.mid, self.event = self.inbound('old-one')

    def open_probe(self, verifier=True):
        p = Probe(self.tmp.name, clock=lambda: self.now,
                  mirror_fence_verifier=self.verifier if verifier else None)
        self.addCleanup(p.close)
        return p

    def inbound(self, source, linked=True, **changes):
        msg = {'message_id': source, 'message_type': 1, 'from_user_id': 'fixture-owner',
               'to_user_id': 'fixture-bot', 'context_token': 'CONTEXT_CANARY',
               'item_list': [{'type': 1, 'text_item': {'text': 'BODY_CANARY'}}]}
        msg.update(changes)
        self.p.weixin.ingest(self.p.weixin.account(), {'msgs': [msg]})
        row = self.p.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()
        if not row:
            return None, None
        mid, event = row['id'], 'evt_fixture_' + source
        if linked:
            with self.p.db:
                self.p.db.execute("INSERT INTO pings(id,nonce,sub_id,body,status,name,message_id) VALUES(?,'','fixture-sub','{}','delivered',?,?)", (event, MESSAGE_EVENT, mid))
                self.p.db.execute('UPDATE weixin_inbox SET event_id=? WHERE id=?', (event, mid))
        return mid, event

    def activate(self):
        staged = self.p.dot_journal.epochs.stage(self.scope, 'synthetic-cutover')
        return self.p.dot_journal.epochs.activate(staged['epoch_id'], 'synthetic-proof-reference')

    def admitted(self, source='future-one'):
        self.activate()
        self.mid, self.event = self.inbound(source)
        return self.prepare()

    def prepare(self, peer=None, **changes):
        args = {'source': SOURCE, 'message_id': self.mid, 'event_id': self.event,
                'part': 'original', 'target_scope': self.scope}
        args.update(changes)
        return (peer or self.p).dot_journal.prepare(**args)

    def authorize(self, prepared, peer=None, **changes):
        args = {'journal_id': prepared['journal_id'], 'attempt_id': prepared['attempt_id'],
                'event_id': prepared['event_id']}
        args.update(changes)
        return (peer or self.p).dot_journal.authorize(**args)

    def evidence(self, prepared, source='api_response', target=None):
        result = {'method': 'user_message.send_message' if source == 'api_response' else 'user_message.read_messages',
                  'target_scope': prepared['target_scope'],
                  'target_dot_message_id': target or self.target,
                  'message_metadata': copy.deepcopy(prepared['message_metadata'])}
        if source == 'history_readback':
            result['partial'] = False
        return result

    def commit(self, prepared, peer=None, source='api_response', target=None, **changes):
        args = {'journal_id': prepared['journal_id'], 'attempt_id': prepared['attempt_id'],
                'event_id': prepared['event_id'], 'target_dot_message_id': target or self.target,
                'receipt_source': source, 'evidence': self.evidence(prepared, source, target)}
        args.update(changes)
        return (peer or self.p).dot_journal.commit(**args)

    def row(self, prepared):
        return self.p.db.execute('SELECT * FROM weixin_dot_journal WHERE id=?',
                                 (prepared['journal_id'],)).fetchone()

    def attachment(self, aid, message_id=None, **changes):
        args = {'id': aid, 'message_id': message_id or self.mid, 'account': 'fixture-bot',
                'peer': 'fixture-owner'}
        args.update(changes)
        with self.p.db:
            self.p.db.execute("""INSERT INTO weixin_attachments
                (id,message_id,account,peer,kind,filename,status,created_at,expires_at,provenance)
                VALUES(:id,:message_id,:account,:peer,'image','synthetic.png','error',0,1,'{}')""", args)

    def test_prepare_only_persists_one_unique_intent_never_a_send_permit(self):
        first = self.prepare()
        second = self.prepare(lease_seconds=300)
        self.assertTrue(first['first_prepare'])
        self.assertFalse(second['first_prepare'])
        for field in ('journal_id', 'attempt_id', 'lease_until'):
            self.assertEqual(first[field], second[field])
        for result in (first, second):
            self.assertFalse(result['claim_granted'])
            self.assertFalse(result['external_write_allowed'])
            self.assertFalse(result['automatic_resend_allowed'])
            self.assertTrue(result['authorization_required'])
            self.assertEqual(result['state'], 'prepared')
        with self.assertRaises(sqlite3.IntegrityError), self.p.db:
            self.p.db.execute("""INSERT INTO weixin_dot_journal
                (id,source,account,owner,message_id,event_id,part,attachment_id,target_scope,
                 attempt_id,state,lease_until,created_at,updated_at)
                SELECT ?,source,account,owner,message_id,event_id,part,attachment_id,target_scope,
                 attempt_id,state,lease_until,created_at,updated_at FROM weixin_dot_journal WHERE id=?""",
                ('djm_' + 'f' * 32, first['journal_id']))
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_journal').fetchone()[0], 1)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_default_fence_is_pending_and_caller_cannot_assert_activation(self):
        default = self.open_probe(verifier=False)
        staged = default.dot_journal.epochs.stage(self.scope, 'default-pending')
        self.assertFalse(staged['verifier_available'])
        with self.assertRaisesRegex(WeixinError, 'activation_pending'):
            default.dot_journal.epochs.activate(staged['epoch_id'], 'caller-proof')
        prepared = self.prepare(peer=default)
        with self.assertRaisesRegex(WeixinError, 'admission_pending'):
            self.authorize(prepared, peer=default)
        self.assertEqual(self.row(prepared)['state'], 'prepared')

    def test_preexisting_source_never_becomes_future_from_an_empty_journal(self):
        prepared = self.prepare()
        self.activate()
        self.assertEqual(self.p.dot_journal.epochs.classify(self.mid)['classification'], 'legacy')
        with self.assertRaisesRegex(WeixinError, 'admission_pending'):
            self.authorize(prepared)
        self.assertIsNone(self.row(prepared)['authorization_id'])

    def test_active_future_source_with_wrong_target_scope_is_not_admitted(self):
        prepared = self.admitted()
        with self.p.db:
            self.p.db.execute('UPDATE weixin_dot_journal SET target_scope=? WHERE id=?',
                              ('different-dot-room', prepared['journal_id']))
        with self.assertRaisesRegex(WeixinError, 'admission_pending'):
            self.authorize(prepared)

    def test_lost_prepare_response_is_recoverable_without_duplicating_authorization(self):
        lost = self.admitted()
        recovered = self.p.dot_journal.for_message(self.mid)['records'][0]
        duplicate = self.prepare()
        self.assertEqual(recovered['attempt_id'], lost['attempt_id'])
        self.assertFalse(recovered['external_write_allowed'])
        self.assertFalse(duplicate['claim_granted'])
        first = self.authorize(duplicate)
        again = self.authorize(recovered)
        self.assertTrue(first['send_authorized'])
        self.assertFalse(again['send_authorized'])
        self.assertFalse(again['external_write_allowed'])

    def test_concurrent_preparations_share_one_record_and_attempt(self):
        peers = [self.open_probe() for _ in range(5)]
        barrier = threading.Barrier(len(peers))
        def prepare(peer):
            barrier.wait()
            return self.prepare(peer=peer)
        with ThreadPoolExecutor(max_workers=len(peers)) as pool:
            results = list(pool.map(prepare, peers))
        self.assertEqual(sum(r['first_prepare'] for r in results), 1)
        self.assertEqual(len({r['journal_id'] for r in results}), 1)
        self.assertEqual(len({r['attempt_id'] for r in results}), 1)
        self.assertFalse(any(r['external_write_allowed'] for r in results))

    def test_concurrent_authorizations_issue_only_one_local_permit(self):
        prepared = self.admitted()
        peers = [self.open_probe() for _ in range(5)]
        barrier = threading.Barrier(len(peers))
        def authorize(peer):
            barrier.wait()
            return self.authorize(prepared, peer=peer)
        with ThreadPoolExecutor(max_workers=len(peers)) as pool:
            results = list(pool.map(authorize, peers))
        self.assertEqual(sum(r['send_authorized'] for r in results), 1)
        self.assertEqual(sum(r['external_write_allowed'] for r in results), 1)
        self.assertTrue(all(not r['automatic_resend_allowed'] for r in results))
        self.assertEqual(self.row(prepared)['state'], 'authorized')

    def test_lost_authorization_response_never_becomes_another_permit(self):
        prepared = self.admitted()
        lost = self.authorize(prepared)
        self.assertTrue(lost['send_authorized'])
        duplicate = self.authorize(prepared)
        self.assertFalse(duplicate['send_authorized'])
        status = self.p.dot_journal.for_message(self.mid)['records'][0]
        self.assertFalse(status['external_write_allowed'])
        self.p.dot_journal.mark_unknown(prepared['journal_id'], prepared['attempt_id'])
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        self.assertEqual(self.prepare()['attempt_id'], prepared['attempt_id'])
        self.assertEqual(self.prepare()['state'], 'unknown')

    def test_expired_never_authorized_preparation_recovers_with_new_attempt(self):
        self.activate()
        self.mid, self.event = self.inbound('short-preparation')
        prepared = self.prepare(lease_seconds=1)
        self.now += 1
        duplicate = self.prepare(lease_seconds=300)
        self.assertEqual(duplicate['state'], 'prepared')
        self.assertFalse(duplicate['first_prepare'])
        self.assertTrue(duplicate['preparation_recovered'])
        self.assertFalse(duplicate['external_write_allowed'])
        self.assertEqual(duplicate['journal_id'], prepared['journal_id'])
        self.assertNotEqual(duplicate['attempt_id'], prepared['attempt_id'])
        self.assertEqual(duplicate['lease_until'], self.now + 300)
        self.assertIsNone(duplicate['authorization_id'])
        for operation in (lambda: self.authorize(prepared),
                          lambda: self.commit(prepared),
                          lambda: self.commit(prepared, source='history_readback'),
                          lambda: self.p.dot_journal.mark_unknown(prepared['journal_id'], prepared['attempt_id'])):
            with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
                operation()
        self.assertTrue(self.authorize(duplicate)['send_authorized'])
        self.assertFalse(self.authorize(duplicate)['send_authorized'])

    def test_observed_preparation_expiry_latches_until_reprepare_despite_clock_rollback(self):
        self.activate()
        self.mid, self.event = self.inbound('expired-before-authorize')
        prepared = self.prepare(lease_seconds=1)
        self.now += 1
        expired = self.authorize(prepared)
        self.assertFalse(expired['send_authorized'])
        self.assertEqual(expired['effective_state'], 'preparation_expired')
        self.assertTrue(expired['preparation_recovery_required'])
        with self.assertRaisesRegex(WeixinError, 'preparation_recovery_required'):
            self.commit(prepared, source='history_readback')
        self.now -= 1
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        recovered = self.prepare()
        self.assertNotEqual(recovered['attempt_id'], prepared['attempt_id'])
        self.assertTrue(self.authorize(recovered)['send_authorized'])

    def test_lost_reprepare_response_returns_same_new_attempt_without_second_recovery(self):
        self.activate()
        self.mid, self.event = self.inbound('lost-reprepare')
        original = self.prepare(lease_seconds=1)
        self.now += 2
        lost = self.prepare()
        status = self.p.dot_journal.for_message(self.mid)['records'][0]
        retry = self.prepare(lease_seconds=300)
        self.assertNotEqual(lost['attempt_id'], original['attempt_id'])
        self.assertTrue(lost['preparation_recovered'])
        self.assertFalse(retry['preparation_recovered'])
        self.assertEqual((lost['attempt_id'], lost['lease_until']),
                         (retry['attempt_id'], retry['lease_until']))
        self.assertEqual(status['attempt_id'], lost['attempt_id'])
        self.assertFalse(status['external_write_allowed'])
        self.assertTrue(self.authorize(retry)['send_authorized'])

    def test_explicit_unknown_without_authorization_is_never_recovered(self):
        prepared = self.admitted()
        self.p.dot_journal.mark_unknown(prepared['journal_id'], prepared['attempt_id'])
        self.now += 500
        peer = self.open_probe()
        peer.dot_journal.recover_inflight()
        retry = self.prepare(peer=peer)
        self.assertEqual(retry['state'], 'unknown')
        self.assertEqual(retry['attempt_id'], prepared['attempt_id'])
        self.assertIsNone(retry['authorization_id'])
        self.assertFalse(retry['preparation_recovered'])
        self.assertFalse(self.authorize(retry, peer=peer)['send_authorized'])

    def test_concurrent_repreparations_share_one_new_attempt_and_one_permit(self):
        self.activate()
        self.mid, self.event = self.inbound('parallel-reprepare')
        original = self.prepare(lease_seconds=1)
        self.now += 2
        peers = [self.open_probe() for _ in range(5)]
        barrier = threading.Barrier(len(peers))
        def recover(peer):
            barrier.wait()
            return self.prepare(peer=peer)
        with ThreadPoolExecutor(max_workers=len(peers)) as pool:
            recovered = list(pool.map(recover, peers))
        self.assertEqual(sum(r['preparation_recovered'] for r in recovered), 1)
        self.assertEqual(len({r['attempt_id'] for r in recovered}), 1)
        self.assertNotEqual(recovered[0]['attempt_id'], original['attempt_id'])
        barrier = threading.Barrier(len(peers))
        def authorize(peer):
            barrier.wait()
            return self.authorize(recovered[0], peer=peer)
        with ThreadPoolExecutor(max_workers=len(peers)) as pool:
            results = list(pool.map(authorize, peers))
        self.assertEqual(sum(r['send_authorized'] for r in results), 1)

    def test_reprepare_racing_stale_authorize_cannot_grant_old_attempt(self):
        self.activate()
        self.mid, self.event = self.inbound('reprepare-versus-stale-authorize')
        original = self.prepare(lease_seconds=1)
        self.now += 2
        peer = self.open_probe()
        barrier = threading.Barrier(2)
        def recover():
            barrier.wait()
            return self.prepare()
        def stale_authorize():
            barrier.wait()
            try:
                return self.authorize(original, peer=peer)['send_authorized']
            except WeixinError as error:
                self.assertEqual(error.kind, 'dot_journal_attempt_mismatch')
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            recovery = pool.submit(recover)
            stale = pool.submit(stale_authorize)
            prepared = recovery.result()
            self.assertFalse(stale.result())
        self.assertTrue(self.authorize(prepared)['send_authorized'])

    def test_authorization_id_is_sticky_even_if_state_were_prepared_again(self):
        prepared = self.admitted()
        permit = self.authorize(prepared)
        # The database itself rejects clearing the permit/history or reopening
        # its state, including SQL from code that predates the new protocol.
        for assignments in ("state='prepared'", "authorization_ever_issued=0",
                            "authorization_history_version=0",
                            "state='prepared',authorization_id=NULL,authorization_ever_issued=0"):
            with self.subTest(assignments=assignments), self.assertRaisesRegex(
                    sqlite3.IntegrityError, 'history_immutable'), self.p.db:
                self.p.db.execute('UPDATE weixin_dot_journal SET ' + assignments + ' WHERE id=?',
                                  (prepared['journal_id'],))
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        self.now += 500
        retry = self.prepare()
        self.assertEqual(retry['state'], 'unknown')
        self.assertEqual(retry['authorization_id'], permit['authorization_id'])
        self.assertEqual(retry['attempt_id'], prepared['attempt_id'])
        self.assertFalse(retry['preparation_recovered'])

    def test_unknown_without_permit_cannot_be_reopened_by_clearing_history(self):
        prepared = self.admitted()
        self.p.dot_journal.mark_unknown(prepared['journal_id'], prepared['attempt_id'])
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'history_immutable'), self.p.db:
            self.p.db.execute("UPDATE weixin_dot_journal SET state='prepared',authorization_id=NULL WHERE id=?",
                              (prepared['journal_id'],))
        self.assertFalse(self.prepare()['preparation_recovery_eligible'])
        self.assertFalse(self.authorize(prepared)['send_authorized'])

    def _old_schema_fixture(self):
        # Synthetic rollback/migration fixture only: retain the exact original
        # table constraints and indexes while removing additive v2 columns.
        with self.p.db:
            for name in ('history_guard', 'authorize_guard', 'attempt_guard'):
                self.p.db.execute('DROP TRIGGER weixin_dot_journal_' + name)
            for name in ('preparation_recovery_required', 'authorization_history_version',
                         'authorization_ever_issued'):
                self.p.db.execute('ALTER TABLE weixin_dot_journal DROP COLUMN ' + name)

    def test_legacy_schema_null_authorization_is_not_proof_of_never_authorized(self):
        prepared = self.admitted()
        self._old_schema_fixture()
        upgraded = self.open_probe()
        status = upgraded.dot_journal.for_message(self.mid)['records'][0]
        self.assertFalse(status['authorization_history_known'])
        self.assertIsNone(status['authorization_ever_issued'])
        self.assertEqual(status['effective_state'], 'authorization_history_unknown')
        self.assertFalse(self.authorize(prepared, peer=upgraded)['send_authorized'])
        retry = self.prepare(peer=upgraded)
        self.assertFalse(retry['preparation_recovered'])
        self.assertEqual(retry['attempt_id'], prepared['attempt_id'])
        # The old worker's exact authorization SQL must not bypass the check.
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'history_required'), upgraded.db:
            upgraded.db.execute("UPDATE weixin_dot_journal SET state='authorized',authorization_id=?,updated_at=? WHERE id=? AND state='prepared' AND attempt_id=?",
                                ('f' * 32, self.now, prepared['journal_id'], prepared['attempt_id']))
        upgraded.dot_journal.recover_inflight()
        again = self.open_probe()
        self.assertEqual(again.dot_journal.for_message(self.mid)['records'][0]['state'], 'unknown')
        self.assertFalse(self.prepare(peer=again)['preparation_recovered'])

    def test_legacy_authorized_and_committed_evidence_survive_repeated_migration(self):
        prepared = self.admitted()
        permit = self.authorize(prepared)
        self.commit(prepared)
        self._old_schema_fixture()
        for _ in range(2):
            peer = self.open_probe()
            status = peer.dot_journal.for_message(self.mid)['records'][0]
            self.assertTrue(status['authorization_ever_issued'])
            self.assertFalse(status['authorization_history_known'])
            self.assertEqual(status['authorization_id'], permit['authorization_id'])
            self.assertEqual(status['target_dot_message_id'], self.target)
            self.assertFalse(self.authorize(prepared, peer=peer)['send_authorized'])

    def test_additive_history_migration_rolls_back_on_interruption(self):
        prepared = self.prepare()
        self._old_schema_fixture()
        alters = []
        def deny_second_alter(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_ALTER_TABLE and arg2 == 'weixin_dot_journal':
                alters.append(arg2)
                if len(alters) == 2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.p.db.set_authorizer(deny_second_alter)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                DotMirrorJournal(self.p, fence_verifier=self.verifier)
        finally:
            self.p.db.set_authorizer(None)
        self.assertEqual(len(alters), 2)
        columns = {r['name'] for r in self.p.db.execute('PRAGMA table_info(weixin_dot_journal)')}
        self.assertTrue(columns.isdisjoint({'preparation_recovery_required',
                                           'authorization_history_version', 'authorization_ever_issued'}))
        peer = self.open_probe()
        self.assertEqual(peer.dot_journal.for_message(self.mid)['records'][0]['attempt_id'], prepared['attempt_id'])
        self.assertFalse(self.authorize(prepared, peer=peer)['send_authorized'])

    def test_old_authorize_sql_cannot_ignore_restart_invalidation(self):
        prepared = self.admitted()
        self.p.dot_journal.recover_inflight()
        # Include the sticky field to independently exercise the restart latch
        # guard; omitting it (the old SQL) is also rejected.
        for history_assignment in ('', ',authorization_ever_issued=1'):
            with self.subTest(history=history_assignment), self.assertRaisesRegex(
                    sqlite3.IntegrityError, 'history_required'), self.p.db:
                self.p.db.execute("UPDATE weixin_dot_journal SET state='authorized',authorization_id=?" +
                                  history_assignment + ' WHERE id=?', ('f' * 32, prepared['journal_id']))
        self.assertFalse(self.row(prepared)['authorization_ever_issued'])
        self.assertIsNone(self.row(prepared)['authorization_id'])

    def test_reprepare_transaction_interruption_preserves_original_identity(self):
        prepared = self.admitted()
        self.now += 121
        with self.p.db:
            self.p.db.execute("""CREATE TRIGGER synthetic_abort_reprepare
                BEFORE UPDATE OF attempt_id ON weixin_dot_journal
                BEGIN SELECT RAISE(ABORT,'synthetic_interruption'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic_interruption'):
            self.prepare()
        self.assertEqual(self.row(prepared)['attempt_id'], prepared['attempt_id'])
        self.assertFalse(self.row(prepared)['preparation_recovery_required'])
        self.assertIsNone(self.row(prepared)['authorization_id'])
        with self.p.db:
            self.p.db.execute('DROP TRIGGER synthetic_abort_reprepare')
        recovered = self.prepare()
        self.assertTrue(recovered['preparation_recovered'])
        with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
            self.authorize(prepared)
        self.assertTrue(self.authorize(recovered)['send_authorized'])

    def test_send_before_commit_restart_keeps_permit_and_late_positive_readback(self):
        prepared = self.admitted()
        evidence_path = Path(self.tmp.name) / 'synthetic-dot-send.json'
        # A separate interpreter commits authorization, receives one fake send
        # response, fsyncs synthetic evidence, then exits without running close
        # handlers or journal commit. Socket guards are installed in that
        # interpreter too; the fake adapter never uses a transport.
        script = r'''
import json, os, sys
from contextlib import ExitStack
from unittest.mock import patch
from probe.probe import Probe

request = json.load(sys.stdin)
with ExitStack() as guards:
    for target in ('socket.getaddrinfo', 'socket.create_connection',
                   'socket.socket.connect', 'socket.socket.connect_ex'):
        guards.enter_context(patch(target, side_effect=AssertionError('network forbidden in crash fixture')))
    probe = Probe(request['directory'], clock=lambda: request['now'])
    identity = request['prepared']
    permit = probe.dot_journal.authorize(identity['journal_id'], identity['attempt_id'], identity['event_id'])
    assert permit['send_authorized'] is True
    class FakeDotAdapter:
        calls = 0
        def send(self, metadata):
            self.calls += 1
            assert self.calls == 1
            return {'target_dot_message_id': request['target'], 'message_metadata': metadata}
    adapter = FakeDotAdapter()
    response = adapter.send(identity['message_metadata'])
    assert response['target_dot_message_id'] == request['target']
    row = probe.db.execute('SELECT state,target_dot_message_id FROM weixin_dot_journal WHERE id=?',
                           (identity['journal_id'],)).fetchone()
    assert row['state'] == 'authorized' and row['target_dot_message_id'] is None
    with open(request['evidence_path'], 'x') as evidence:
        json.dump({'fixture_only': True, 'send_count': adapter.calls,
                   'send_returned': True, 'commit_called': False,
                   'authorization_id': permit['authorization_id'], 'response': response}, evidence)
        evidence.flush()
        os.fsync(evidence.fileno())
    os._exit(73)
'''
        exited = subprocess.run([sys.executable, '-B', '-c', script],
            cwd=Path(__file__).resolve().parents[1],
            input=json.dumps({'directory': self.tmp.name, 'now': self.now, 'prepared': prepared,
                              'target': self.target, 'evidence_path': str(evidence_path)}),
            text=True, capture_output=True, timeout=15)
        self.assertEqual(exited.returncode, 73, exited.stderr)
        sent_bytes = evidence_path.read_bytes()
        sent = json.loads(sent_bytes)
        self.assertEqual(sent['send_count'], 1)
        self.assertTrue(sent['send_returned'])
        self.assertFalse(sent['commit_called'])
        self.assertEqual(sent['response']['message_metadata'], prepared['message_metadata'])
        self.assertEqual(self.row(prepared)['state'], 'authorized')
        self.assertIsNone(self.row(prepared)['target_dot_message_id'])
        peer = self.open_probe()
        peer.dot_journal.recover_inflight()
        peer.dot_journal.recover_inflight()
        status = peer.dot_journal.for_message(self.mid)['records'][0]
        self.assertEqual(status['state'], 'unknown')
        self.assertTrue(status['authorization_ever_issued'])
        self.assertEqual(status['authorization_id'], sent['authorization_id'])
        self.assertFalse(self.prepare(peer=peer)['preparation_recovered'])
        self.assertFalse(self.authorize(prepared, peer=peer)['send_authorized'])
        with self.assertRaisesRegex(WeixinError, 'positive_readback_required'):
            self.commit(prepared, peer=peer)
        readback = self.commit(prepared, peer=peer, source='history_readback')
        self.assertEqual(readback['target_dot_message_id'], sent['response']['target_dot_message_id'])
        self.assertTrue(readback['positive_readback_recorded'])
        self.assertFalse(self.authorize(prepared, peer=peer)['send_authorized'])
        self.assertEqual(evidence_path.read_bytes(), sent_bytes)

    def test_seeded_protocol_sequences_issue_at_most_one_permit_per_business_key(self):
        # Use the explicitly bounded synthetic admission mode here so restart,
        # close and re-enable can all interleave. Verified epochs intentionally
        # suspend forever on restart and would make every later enable invalid.
        cutover = self.p.dot_journal.cutover
        boundary = cutover.hold('seeded-sequence-fixture')
        cutover.enable(boundary['boundary_id'], self.scope, boundary['admission_revision'])
        rng = random.Random(20261023)
        counts = {'restart': 0, 'close': 0, 'enable': 0, 'stale_enable_rejected': 0,
                  'stale_close_replayed': 0, 'permits': 0}
        old_enable_revisions, old_close_requests = [], []
        for source_index in range(16):
            self.mid, self.event = self.inbound('sequence-' + str(source_index))
            identities = [self.prepare(lease_seconds=2)]
            permits = 0
            prefix = ['restart', 'prepare', 'close', 'stale_enable', 'enable', 'authorize']
            operations = prefix + [rng.choice(('prepare', 'advance', 'authorize', 'unknown', 'commit',
                                               'restart', 'close', 'enable', 'stale_enable', 'stale_close'))
                                   for _ in range(24)]
            for step, operation in enumerate(operations):
                chosen = identities[-1] if step < len(prefix) else rng.choice(identities)
                before = self.p.dot_journal.cutover.status()
                try:
                    if operation == 'prepare':
                        identities.append(self.prepare(lease_seconds=2))
                    elif operation == 'advance':
                        self.now += 3
                    elif operation == 'authorize':
                        granted = self.authorize(chosen)['send_authorized']
                        if before['state'] == 'closed':
                            self.assertFalse(granted)
                        permits += int(granted)
                        counts['permits'] += int(granted)
                    elif operation == 'unknown':
                        self.p.dot_journal.mark_unknown(chosen['journal_id'], chosen['attempt_id'])
                    elif operation == 'commit':
                        self.commit(chosen, source='history_readback',
                                    target='Sentinel_' + format(source_index + 1, '032x'))
                    elif operation == 'restart':
                        self.p.close()
                        self.p = self.open_probe()
                        self.p.dot_journal.recover_inflight()
                        counts['restart'] += 1
                        self.assertEqual(self.p.dot_journal.cutover.status(), before)
                    elif operation == 'close':
                        old_enable_revisions.append(before['admission_revision'])
                        request_id = 'seeded-close-' + str(counts['close'])
                        self.p.dot_journal.cutover.close(boundary['boundary_id'], request_id)
                        old_close_requests.append(request_id)
                        counts['close'] += 1
                    elif operation == 'enable':
                        self.p.dot_journal.cutover.enable(boundary['boundary_id'], self.scope,
                                                         before['admission_revision'])
                        counts['enable'] += 1
                    elif operation == 'stale_enable' and old_enable_revisions:
                        with self.assertRaisesRegex(WeixinError, 'revision_conflict'):
                            self.p.dot_journal.cutover.enable(boundary['boundary_id'], self.scope,
                                                             rng.choice(old_enable_revisions))
                        counts['stale_enable_rejected'] += 1
                        self.assertEqual(self.p.dot_journal.cutover.status(), before)
                    elif operation == 'stale_close' and old_close_requests:
                        replay = self.p.dot_journal.cutover.close(boundary['boundary_id'],
                                                                 rng.choice(old_close_requests))
                        self.assertTrue(replay['close_request_replayed'])
                        counts['stale_close_replayed'] += 1
                        self.assertEqual(self.p.dot_journal.cutover.status(), before)
                except WeixinError as error:
                    # Only the protocol's explicit stale/state/admission
                    # rejections are acceptable; unrelated failures fail tests.
                    self.assertIn(error.kind, {'dot_journal_attempt_mismatch',
                        'dot_journal_preparation_recovery_required', 'dot_journal_positive_readback_required',
                        'dot_journal_already_committed', 'dot_journal_fence_or_source_admission_pending',
                        'dot_cutover_authorized_intent_pending'})
                self.assertLessEqual(permits, 1, (source_index, operation))
            self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_journal WHERE message_id=?',
                                               (self.mid,)).fetchone()[0], 1)
        for operation, count in counts.items():
            self.assertGreater(count, 0, (operation, counts))

    def test_expired_authorization_rejects_late_api_response_but_records_real_readback(self):
        self.activate()
        self.mid, self.event = self.inbound('short-authorization')
        prepared = self.prepare(lease_seconds=1)
        self.authorize(prepared)
        self.now += 1
        with self.assertRaisesRegex(WeixinError, 'positive_readback_required'):
            self.commit(prepared)
        self.assertEqual(self.row(prepared)['state'], 'unknown')
        self.now -= 1
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        self.now += 1
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        committed = self.commit(prepared, source='history_readback')
        self.assertEqual(committed['state'], 'committed')
        self.assertTrue(committed['positive_readback_recorded'])

    def test_unknown_requires_a_positive_correlated_readback_and_never_missing_history(self):
        prepared = self.admitted()
        self.authorize(prepared)
        self.p.dot_journal.mark_unknown(prepared['journal_id'], prepared['attempt_id'])
        with self.assertRaisesRegex(WeixinError, 'positive_readback_required'):
            self.commit(prepared)
        proof = self.evidence(prepared, 'history_readback')
        for bad in (None, {}, {**proof, 'partial': True}, {k: v for k, v in proof.items() if k != 'partial'}):
            with self.subTest(bad=bad), self.assertRaises(WeixinError):
                self.commit(prepared, source='history_readback', evidence=bad)
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        self.assertEqual(self.commit(prepared, source='history_readback')['state'], 'committed')

    def test_evidence_must_match_scope_target_metadata_and_bounded_keys(self):
        prepared = self.admitted()
        self.authorize(prepared)
        proof = self.evidence(prepared)
        cases = [{**proof, 'target_scope': 'other-room'},
                 {**proof, 'target_dot_message_id': 'Sentinel_' + '2' * 32},
                 {**proof, 'method': 'private.internal.lookup'},
                 {**proof, 'body': 'SECRET_BODY_CANARY'},
                 {**proof, 'message_metadata': {}}]
        for field, value in [('message_id', 'wx_missing'), ('part', 'final'),
                             ('attachment_id', 'att_' + 'c' * 32), ('attempt_id', 'a' * 32),
                             ('source', 'other_source'), ('suppress_reflection', False)]:
            changed = copy.deepcopy(proof)
            changed['message_metadata']['weixin_dot_journal'][field] = value
            cases.append(changed)
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaisesRegex(WeixinError, 'evidence_mismatch'):
                self.commit(prepared, evidence=bad)
        self.assertIsNone(self.row(prepared)['receipt_evidence'])

    def test_unauthed_api_commit_cannot_forge_a_first_authorization(self):
        prepared = self.admitted()
        with self.assertRaisesRegex(WeixinError, 'positive_readback_required'):
            self.commit(prepared)
        self.assertEqual(self.row(prepared)['state'], 'prepared')
        self.assertTrue(self.authorize(prepared)['send_authorized'])

    def test_canonical_receipt_metadata_distinguishes_true_from_integer_one(self):
        prepared = self.admitted()
        self.authorize(prepared)
        proof = self.evidence(prepared)
        proof['message_metadata']['weixin_dot_journal']['suppress_reflection'] = 1
        with self.assertRaisesRegex(WeixinError, 'evidence_mismatch'):
            self.commit(prepared, evidence=proof)
        self.assertEqual(self.row(prepared)['state'], 'authorized')

    def test_target_id_cannot_be_reassociated_with_another_source_part(self):
        prepared = self.admitted()
        self.authorize(prepared)
        self.commit(prepared)
        other_mid, other_event = self.inbound('future-other-target')
        other = self.prepare(message_id=other_mid, event_id=other_event)
        self.authorize(other)
        with self.assertRaisesRegex(WeixinError, 'target_already_associated'):
            self.commit(other)
        self.assertEqual(self.row(other)['state'], 'authorized')
        self.assertIsNone(self.row(other)['target_dot_message_id'])
        result = self.commit(other, target='Sentinel_' + '2' * 32)
        self.assertEqual(result['state'], 'committed')

    def test_api_commit_then_exact_readback_is_idempotent_without_remote_verification_claim(self):
        prepared = self.admitted()
        self.authorize(prepared)
        first = self.commit(prepared)
        self.assertEqual(first, self.commit(prepared))
        self.assertFalse(first['positive_readback_recorded'])
        second = self.commit(prepared, source='history_readback')
        self.assertTrue(second['positive_readback_recorded'])
        self.assertEqual(second['receipt_source'], 'api_response')
        self.assertEqual(second, self.commit(prepared, source='history_readback'))
        self.assertFalse(second['target_id_verified_by_bridge'])
        self.assertFalse(second['dot_visible_verified_by_bridge'])
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        with self.assertRaisesRegex(WeixinError, 'receipt_conflict'):
            self.commit(prepared, target='Sentinel_' + '2' * 32)
        with self.assertRaisesRegex(WeixinError, 'already_committed'):
            self.p.dot_journal.mark_unknown(prepared['journal_id'], prepared['attempt_id'])

    def test_concurrent_commit_retries_keep_one_real_target(self):
        prepared = self.admitted()
        self.authorize(prepared)
        peers = [self.open_probe() for _ in range(4)]
        barrier = threading.Barrier(len(peers))
        def commit(peer):
            barrier.wait()
            return self.commit(prepared, peer=peer)
        with ThreadPoolExecutor(max_workers=len(peers)) as pool:
            results = list(pool.map(commit, peers))
        self.assertTrue(all(r['state'] == 'committed' for r in results))
        self.assertEqual(len({r['target_dot_message_id'] for r in results}), 1)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_journal').fetchone()[0], 1)

    def test_lost_commit_response_readback_does_not_authorize_resending(self):
        prepared = self.admitted()
        self.authorize(prepared)
        self.commit(prepared)
        recovered = self.p.dot_journal.for_message(self.mid)['records'][0]
        self.assertEqual(recovered['target_dot_message_id'], self.target)
        self.assertFalse(recovered['external_write_allowed'])
        self.assertEqual(self.commit(prepared)['target_dot_message_id'], self.target)
        self.assertFalse(self.authorize(prepared)['send_authorized'])

    def test_restart_recovery_suspends_gate_and_preserves_committed_and_unknown(self):
        prepared = self.admitted()
        self.authorize(prepared)
        second_mid, second_event = self.inbound('future-two')
        committed = self.prepare(message_id=second_mid, event_id=second_event)
        self.authorize(committed)
        self.commit(committed)
        third_mid, third_event = self.inbound('future-three')
        unauthed = self.prepare(message_id=third_mid, event_id=third_event)
        peer = self.open_probe()
        # Merely constructing a reader does not recover or imply quiescence.
        self.assertEqual(peer.dot_journal.for_message(self.mid)['records'][0]['state'], 'authorized')
        peer.dot_journal.recover_inflight()
        self.assertEqual(peer.dot_journal.for_message(self.mid)['records'][0]['state'], 'unknown')
        unauthed_status = peer.dot_journal.for_message(third_mid)['records'][0]
        self.assertEqual(unauthed_status['state'], 'prepared')
        self.assertEqual(unauthed_status['effective_state'], 'preparation_expired')
        self.assertTrue(unauthed_status['preparation_recovery_required'])
        self.assertEqual(peer.dot_journal.for_message(second_mid)['records'][0]['state'], 'committed')
        self.assertFalse(self.authorize(prepared, peer=peer)['send_authorized'])
        self.assertFalse(self.authorize(unauthed, peer=peer)['send_authorized'])
        with self.assertRaisesRegex(WeixinError, 'preparation_recovery_required'):
            self.commit(unauthed, peer=peer, source='history_readback')
        recovered = self.prepare(peer=peer, message_id=third_mid, event_id=third_event)
        self.assertNotEqual(recovered['attempt_id'], unauthed['attempt_id'])
        with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
            self.authorize(unauthed, peer=peer)
        # Preparation recovery does not reopen a suspended admission gate.
        with self.assertRaisesRegex(WeixinError, 'admission_pending'):
            self.authorize(recovered, peer=peer)
        self.assertEqual(peer.dot_journal.epochs.classify(self.mid)['state'], 'suspended')

    def test_stale_attempt_and_wrong_event_cannot_authorize_mark_or_commit(self):
        prepared = self.admitted()
        for changes in ({'attempt_id': 'f' * 32}, {'event_id': 'evt-other'}):
            with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
                self.authorize(prepared, **changes)
            with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
                self.commit(prepared, source='history_readback', **changes)
        with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
            self.p.dot_journal.mark_unknown(prepared['journal_id'], 'f' * 32)
        self.assertIsNone(self.row(prepared)['authorization_id'])

    def test_each_attachment_and_original_text_have_independent_states(self):
        text = self.admitted()
        aids = ['att_' + 'a' * 32, 'att_' + 'b' * 32]
        for aid in aids:
            self.attachment(aid)
        first, second = [self.prepare(attachment_id=aid) for aid in aids]
        self.assertEqual(len({r['journal_id'] for r in (text, first, second)}), 3)
        self.authorize(first)
        self.commit(first)
        self.p.dot_journal.mark_unknown(second['journal_id'], second['attempt_id'])
        records = self.p.dot_journal.for_message(self.mid)['records']
        states = {r['attachment_id']: r['state'] for r in records}
        self.assertEqual(states, {None: 'prepared', aids[0]: 'committed', aids[1]: 'unknown'})
        self.assertTrue(self.authorize(text)['send_authorized'])
        self.assertFalse(self.authorize(second)['send_authorized'])

    def test_reply_parts_need_original_committed_reply_slots(self):
        self.admitted()
        with self.assertRaisesRegex(WeixinError, 'reply_not_committed'):
            self.prepare(part='final')
        self.p.weixin.queue_reply_phase(self.mid, 'ack', 'synthetic ack')
        ack = self.prepare(part='ack')
        self.p.weixin.queue_reply_phase(self.mid, 'final', 'synthetic final')
        final = self.prepare(part='final')
        self.assertNotEqual(ack['journal_id'], final['journal_id'])
        self.authorize(ack)
        self.assertTrue(self.authorize(final)['send_authorized'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 2)

    def test_owner_source_event_and_attachment_identity_are_rechecked(self):
        for changes in ({'source': 'dot'}, {'message_id': 'wx_missing'}, {'event_id': 'wrong-event'},
                        {'part': 'notice'}, {'attachment_id': 'att_' + 'a' * 32}):
            with self.subTest(changes=changes), self.assertRaises(WeixinError):
                self.prepare(**changes)
        unlinked, _ = self.inbound('unlinked', linked=False)
        with self.assertRaisesRegex(WeixinError, 'event_mismatch'):
            self.prepare(message_id=unlinked)
        self.assertEqual(self.inbound('foreign', from_user_id='foreign-owner'), (None, None))
        prepared = self.prepare()
        with self.p.db:
            self.p.db.execute("UPDATE weixin_accounts SET owner='changed-owner'")
        for operation in (lambda: self.authorize(prepared),
                          lambda: self.p.dot_journal.for_message(self.mid)):
            with self.assertRaises(WeixinError):
                operation()

    def test_multiple_bindings_deny_all_new_identity_metadata(self):
        prepared = self.prepare()
        with self.p.db:
            self.p.db.execute("INSERT INTO weixin_accounts SELECT 'other-bot',owner,token,base_url,cursor,status,last_poll,last_error FROM weixin_accounts LIMIT 1")
        with self.assertRaisesRegex(WeixinError, 'owner_unavailable'):
            self.authorize(prepared)
        with self.assertRaisesRegex(WeixinError, 'owner_unavailable'):
            self.p.dot_journal.for_message(self.mid)

    def test_broken_event_association_hides_attempt_identity_in_status(self):
        prepared = self.prepare()
        with self.p.db:
            self.p.db.execute('DELETE FROM pings WHERE id=?', (self.event,))
        status = self.p.dot_journal.for_message(self.mid)['records'][0]
        self.assertFalse(status['available'])
        self.assertNotIn('attempt_id', status)
        self.assertFalse(status['external_write_allowed'])
        with self.assertRaisesRegex(WeixinError, 'event_mismatch'):
            self.authorize(prepared)

    def test_legacy_prepare_is_fenced_for_same_key_and_after_staging(self):
        self.prepare()
        with self.assertRaisesRegex(WeixinError, 'legacy_prepare_fenced'):
            self.p.dot_mirrors.prepare(self.mid, self.event, 'original')
        other_mid, other_event = self.inbound('before-stage-two')
        self.p.dot_journal.epochs.stage(self.scope, 'stage-blocks-v1')
        with self.assertRaisesRegex(WeixinError, 'legacy_prepare_fenced'):
            self.p.dot_mirrors.prepare(other_mid, other_event, 'original')
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_mirrors').fetchone()[0], 0)

    def test_legacy_records_keep_their_original_receipts_and_block_new_intent(self):
        legacy = self.p.dot_mirrors.prepare(self.mid, self.event, 'original')
        self.p.dot_mirrors.commit(legacy['mirror_id'], legacy['attempt_id'], self.event,
                                  self.target, 'api_response')
        with self.assertRaisesRegex(WeixinError, 'legacy_record_requires_reconciliation'):
            self.prepare()
        status = self.p.dot_journal.for_message(self.mid)
        self.assertEqual(status['records'], [])
        self.assertEqual(status['legacy_receipts']['records'][0]['target_dot_message_id'], self.target)
        self.assertFalse(status['external_write_allowed'])

    def test_legacy_and_journal_competing_first_insert_cannot_both_win(self):
        peer = self.open_probe()
        barrier = threading.Barrier(2)
        def legacy():
            barrier.wait()
            try:
                self.p.dot_mirrors.prepare(self.mid, self.event, 'original')
                return 'legacy'
            except WeixinError:
                return None
        def journal():
            barrier.wait()
            try:
                self.prepare(peer=peer)
                return 'journal'
            except WeixinError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(legacy), pool.submit(journal)]
            results = [future.result() for future in futures]
        self.assertEqual(sum(x is not None for x in results), 1)
        counts = sum(self.p.db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                     for table in ('weixin_dot_mirrors', 'weixin_dot_journal'))
        self.assertEqual(counts, 1)

    def test_pending_attachment_source_is_captured_before_cutover(self):
        old_mid, old_event = self.mid, self.event
        self.activate()
        aid = 'att_' + 'd' * 32
        self.attachment(aid, message_id=old_mid)
        old_attachment = self.prepare(message_id=old_mid, event_id=old_event, attachment_id=aid)
        with self.assertRaisesRegex(WeixinError, 'admission_pending'):
            self.authorize(old_attachment)
        future_mid, future_event = self.inbound('new-after-cutover')
        fresh = self.prepare(message_id=future_mid, event_id=future_event)
        self.assertTrue(self.authorize(fresh)['send_authorized'])

    def test_status_is_read_only_and_expiry_projection_does_not_renew(self):
        prepared = self.prepare(lease_seconds=1)
        self.now += 2
        before = self.p.db.total_changes
        statements = []
        self.p.db.set_trace_callback(statements.append)
        try:
            status = self.p.dot_journal.for_message(self.mid)
        finally:
            self.p.db.set_trace_callback(None)
        self.assertEqual(before, self.p.db.total_changes)
        record = status['records'][0]
        self.assertEqual((record['state'], record['effective_state']), ('prepared', 'preparation_expired'))
        self.assertTrue(record['preparation_recovery_required'])
        self.assertFalse(record['external_write_allowed'])
        self.assertEqual(record['lease_until'], prepared['lease_until'])
        self.assertFalse(any(s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for s in statements))

    def test_scope_is_immutable_and_does_not_create_another_unique_key(self):
        prepared = self.prepare()
        with self.assertRaisesRegex(WeixinError, 'scope_or_event_conflict'):
            self.prepare(target_scope='another-dot-room')
        self.assertEqual(self.row(prepared)['target_scope'], self.scope)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_journal').fetchone()[0], 1)

    def test_strict_arguments_metadata_privacy_and_tool_catalog(self):
        for lease in (0, 301, True, 1.5, '120'):
            with self.subTest(lease=lease), self.assertRaises(WeixinError):
                self.prepare(lease_seconds=lease)
        for scope in ('', 'room with space', 'https://example.invalid', 'a' * 257):
            with self.subTest(scope=scope), self.assertRaises(WeixinError):
                self.prepare(target_scope=scope)
        prepared = self.prepare()
        status = self.p.dot_journal.for_message(self.mid)
        encoded = json.dumps(prepared) + json.dumps(status)
        for canary in ('BODY_CANARY', 'TOKEN_CANARY', 'CONTEXT_CANARY', 'fixture-owner', 'fixture-bot'):
            self.assertNotIn(canary, encoded)
        columns = {r['name'] for r in self.p.db.execute('PRAGMA table_info(weixin_dot_journal)')}
        self.assertTrue(columns.isdisjoint({'body', 'text', 'token', 'secret', 'url', 'payload'}))
        names = {t['name'] for t in journal_tools()}
        self.assertEqual(len(names), 5)
        self.assertTrue(names <= {t['name'] for t in Probe.tools()})
        self.assertEqual(status['journal_version'], JOURNAL_VERSION)
        args = {'source': SOURCE, 'message_id': self.mid, 'event_id': self.event,
                'part': 'original', 'target_scope': self.scope}
        for extra in ({'body': 'not-allowed'}, {'fence_verifier': True}, {'suppress_reflection': False}):
            with self.assertRaises(WeixinError):
                self.p.dot_journal.call('prepare_weixin_dot_journal', {**args, **extra})


if __name__ == '__main__':
    unittest.main()
