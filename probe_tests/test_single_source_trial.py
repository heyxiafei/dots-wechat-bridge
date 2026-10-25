"""Single-source trial safety; synthetic SQLite, fake clocks and no network.

The actual older rollback process is loaded from its existing source directory,
never reconstructed by copying selected modules. No real send method is called.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

import test_dotjournal as fixtures
from probe.dotcutover import CUTOVER_VERSION, TRIAL_TEXT_MAX_BYTES
from probe.probe import Probe
from probe.weixin import WeixinError


class SingleSourceTrialTests(unittest.TestCase):
    scope = fixtures.DotJournalTests.scope
    target = fixtures.DotJournalTests.target
    setUp = fixtures.DotJournalTests.setUp
    open_probe = fixtures.DotJournalTests.open_probe
    inbound = fixtures.DotJournalTests.inbound
    prepare = fixtures.DotJournalTests.prepare
    authorize = fixtures.DotJournalTests.authorize
    evidence = fixtures.DotJournalTests.evidence
    commit = fixtures.DotJournalTests.commit
    row = fixtures.DotJournalTests.row
    attachment = fixtures.DotJournalTests.attachment
    text = 'BODY_CANARY'

    def fresh_database(self):
        """A separate synthetic DB for incompatible one-shot subcases."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = self.open_probe()
        self.p.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
            'ilink_user_id': 'fixture-owner', 'bot_token': 'TOKEN_CANARY'})
        self.mid, self.event = self.inbound('old-one')

    def status(self, peer=None):
        return (peer or self.p).dot_journal.cutover.status()

    def arm_args(self, text=None):
        boundary = self.p.dot_journal.cutover.hold('single-trial-boundary')
        return {'boundary_id': boundary['boundary_id'], 'target_scope': self.scope,
                'request_id': 'single-trial-request',
                'expected_text_sha256': hashlib.sha256((self.text if text is None else text).encode()).hexdigest(),
                'expected_revision': boundary['admission_revision']}

    def arm(self, text=None):
        self.arm_request = self.arm_args(text)
        return self.p.dot_journal.cutover.arm_trial(**self.arm_request)

    def select_args(self, mid, armed=None):
        armed = armed or self.status()
        return {'boundary_id': armed['boundary_id'], 'request_id': 'single-trial-request',
                'message_id': mid, 'expected_revision': armed['single_source_trial']['arm_revision']}

    def select(self, mid, armed=None, peer=None):
        return (peer or self.p).dot_journal.cutover.select_trial(**self.select_args(mid, armed))

    def close_trial(self, peer=None):
        store = (peer or self.p).dot_journal.cutover
        return store.close(store.status()['boundary_id'], 'single-trial-close')

    def prepare_pair(self, pair, peer=None, **changes):
        return self.prepare(peer=peer, message_id=pair[0], event_id=pair[1], **changes)

    def denied_permit(self, prepared, peer=None):
        try:
            value = self.authorize(prepared, peer=peer)
        except WeixinError:
            pass
        else:
            self.assertFalse(value['send_authorized'])
        self.assertIsNone(self.row(prepared)['authorization_id'])

    def reject_selection_unchanged(self, mid):
        before = self.status()
        with self.assertRaises(WeixinError):
            self.select(mid)
        self.assertEqual(self.status(), before)

    def rpc(self, name, arguments):
        return self.p.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': name, 'arguments': arguments}})

    def ordered_transactions(self, first_peer, first_call, second_peer, second_call):
        """Prove which SQLite connection owns the write lock, without sleeps."""
        owns_lock, release, second_begin = threading.Event(), threading.Event(), threading.Event()
        first_begin = threading.Event()
        barrier = threading.Barrier(2)
        def first_trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                first_begin.set()
            if first_begin.is_set() and sql.lstrip().upper().startswith('SELECT ') and not owns_lock.is_set():
                owns_lock.set()
                release.wait(5)
        def second_trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                second_begin.set()
        def call(fn, second=False):
            barrier.wait(timeout=5)
            if second:
                self.assertTrue(owns_lock.wait(5))
            try:
                return fn()
            except WeixinError as error:
                return error.kind
        first_peer.db.set_trace_callback(first_trace)
        second_peer.db.set_trace_callback(second_trace)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(call, first_call)
                second = pool.submit(call, second_call, True)
                self.assertTrue(second_begin.wait(5), 'second writer never attempted its transaction')
                release.set()
                return first.result(timeout=10), second.result(timeout=10)
        finally:
            release.set()
            first_peer.db.set_trace_callback(None)
            second_peer.db.set_trace_callback(None)

    def test_rpc_arm_contract_strict_arguments_and_permanent_binding(self):
        self.assertEqual(CUTOVER_VERSION, 'local-ownership-v3-singletrial')
        definitions = {t['name']: t for t in Probe.tools()}
        args = self.arm_args()
        tool = 'arm_weixin_dot_single_source_trial'
        self.assertEqual(set(definitions[tool]['inputSchema']['required']), set(args))
        self.assertFalse(definitions[tool]['inputSchema']['additionalProperties'])
        before = self.status()
        # SQLite CHECK must reject an incomplete selection, not accept NULL as
        # an unknown truth value. This insert is only into the synthetic DB.
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'CHECK constraint failed'), self.p.db:
            self.p.db.execute('''INSERT INTO weixin_dot_single_trial
                (singleton,boundary_id,request_id,target_scope,expected_text_sha256,
                 arm_revision,created_at,selected_message_id,selection_revision,selected_at)
                VALUES(1,?,?,?,?,1,?,?,NULL,?)''',
                (args['boundary_id'], args['request_id'], self.scope,
                 args['expected_text_sha256'], self.now, self.mid, self.now))
        self.assertEqual(self.status(), before)
        for changes in ({'expected_revision': True}, {'expected_revision': -1},
                        {'expected_revision': 0.0}, {'expected_text_sha256': 'z' * 64},
                        {'request_id': ''}, {'extra': 'not allowed'}):
            with self.subTest(changes=changes):
                self.assertIn('error', self.rpc(tool, dict(args, **changes)))
                self.assertEqual(self.status(), before)
        response = self.rpc(tool, args)
        self.assertNotIn('error', response)
        armed = response['result']['structuredContent']
        self.assertEqual(armed['admission_revision'], args['expected_revision'] + 1)
        self.assertEqual(armed['state'], 'closed')
        self.assertFalse(armed['any_new_admission_enabled'])
        self.assertFalse(armed['single_source_trial']['selection_active'])
        self.assertEqual(self.rpc(tool, args)['result']['structuredContent'], armed)
        for changes in ({'request_id': 'replacement'}, {'target_scope': 'another-room'},
                        {'expected_text_sha256': 'f' * 64}):
            with self.assertRaises(WeixinError):
                self.p.dot_journal.cutover.arm_trial(**dict(args, **changes))
            self.assertEqual(self.status(), armed)
        with self.assertRaisesRegex(WeixinError, 'dot_cutover_single_source_trial_required'):
            self.p.dot_journal.cutover.enable(armed['boundary_id'], self.scope, armed['admission_revision'])
        pair = self.inbound('rpc-selected')
        selected = self.rpc('select_weixin_dot_single_source_trial', self.select_args(pair[0]))
        self.assertNotIn('error', selected)
        self.assertTrue(selected['result']['structuredContent']['single_source_trial']['selection_active'])

    def test_excludes_23_reserved_legacy_duplicates_and_other_future_sources(self):
        old = (self.mid, self.event)
        self.p.dot_journal.cutover.hold('single-trial-boundary')
        reserved = [self.inbound('reserved-' + str(i)) for i in range(23)]
        self.assertEqual(self.status()['reserved_new_count'], 23)
        self.arm()
        for pair in [old] + reserved:
            self.reject_selection_unchanged(pair[0])
        # A refreshed upstream timestamp and body do not make an old ID a new INSERT.
        manifest = self.p.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (old[0],)).fetchone()[0]
        added = self.p.weixin.ingest(self.p.weixin.account(), {'msgs': [{
            'message_id': 'old-one', 'message_type': 1, 'from_user_id': 'fixture-owner',
            'to_user_id': 'fixture-bot', 'create_time_ms': (self.now + 100) * 1000,
            'item_list': [{'type': 1, 'text_item': {'text': 'replacement body'}}]}]})
        self.assertEqual(added, 0)
        self.assertEqual(self.p.weixin.read_message(old[0])['text'], self.text)
        self.assertEqual(self.p.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (old[0],)).fetchone()[0], manifest)
        self.reject_selection_unchanged(old[0])
        # Same fake clock, older upstream creation time: the local INSERT is new.
        fresh = self.inbound('fresh-with-old-timestamp', create_time_ms=(self.now - 10000) * 1000)
        other = self.inbound('other-matching-future')
        selected = self.select(fresh[0])
        self.assertFalse(selected['new_admission_enabled'])
        self.assertTrue(selected['single_source_admission_enabled'])
        for pair in reserved + [other, self.inbound('later-matching-future')]:
            self.denied_permit(self.prepare_pair(pair))
            self.reject_selection_unchanged(pair[0])
        self.assertTrue(self.authorize(self.prepare_pair(fresh))['send_authorized'])

    def test_selection_rejects_wrong_hash_attachments_incomplete_and_nonowner(self):
        self.arm()
        cases = [
            ('wrong-marker', [{'type': 1, 'text_item': {'text': 'wrong marker'}}]),
            ('with-file', [{'type': 1, 'text_item': {'text': self.text}}, {'type': 4, 'file_item': {'file_name': 'synthetic.txt'}}]),
            ('unknown-item', [{'type': 1, 'text_item': {'text': self.text}}, {'type': 999}]),
        ]
        for name, items in cases:
            self.reject_selection_unchanged(self.inbound(name, item_list=items)[0])
        for name, sql in (
            ('missing-capture', 'DELETE FROM weixin_inbound_manifests WHERE message_id=?'),
            ('corrupt-capture', "UPDATE weixin_inbound_manifests SET metadata_json='invalid-json' WHERE message_id=?"),
            ('wrong-owner', "UPDATE weixin_inbox SET peer='foreign-owner' WHERE id=?")):
            pair = self.inbound(name)
            with self.p.db:
                self.p.db.execute(sql, (pair[0],))
            self.reject_selection_unchanged(pair[0])
        pair = self.inbound('only-complete-text')
        self.assertTrue(self.select(pair[0])['single_source_trial']['selection_active'])

    def test_short_text_bound_is_utf8_bytes_and_never_truncates_to_match(self):
        self.assertEqual(TRIAL_TEXT_MAX_BYTES, 256)
        for text, allowed in [('a' + '你' * 85, True), ('aa' + '你' * 85, False), ('', False)]:
            with self.subTest(byte_length=len(text.encode())):
                self.fresh_database()
                self.arm(text)
                pair = self.inbound('utf8-boundary', item_list=[{'type': 1, 'text_item': {'text': text}}])
                if allowed:
                    self.assertTrue(self.select(pair[0])['single_source_trial']['selection_active'])
                else:
                    self.reject_selection_unchanged(pair[0])

    def test_two_concurrent_matching_sources_cannot_both_be_selected(self):
        armed = self.arm()
        pairs = [self.inbound('matching-a'), self.inbound('matching-b')]
        peers = [self.open_probe(), self.open_probe()]
        barrier = threading.Barrier(2)
        def select(index):
            barrier.wait(timeout=5)
            try:
                return self.select(pairs[index][0], armed, peers[index])
            except WeixinError as error:
                return error.kind
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(select, range(2)))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        state = self.status()
        self.assertEqual(state['admission_revision'], armed['admission_revision'] + 1)
        winner = state['single_source_trial']['selected_message_id']
        for pair in pairs:
            prepared = self.prepare_pair(pair)
            if pair[0] == winner:
                self.assertTrue(self.authorize(prepared)['send_authorized'])
                self.assertFalse(self.authorize(prepared)['send_authorized'])
            else:
                self.denied_permit(prepared)

    def test_select_close_race_both_orders_and_late_retry_cannot_reopen(self):
        for close_first in (True, False):
            with self.subTest(close_first=close_first):
                self.fresh_database()
                self.arm()
                pair = self.inbound('select-close-race')
                peer = self.open_probe()
                args = self.select_args(pair[0])
                close = lambda: self.p.dot_journal.cutover.close(args['boundary_id'], 'single-trial-close')
                select = lambda: peer.dot_journal.cutover.select_trial(**args)
                if close_first:
                    closed, selection = self.ordered_transactions(self.p, close, peer, select)
                    self.assertIsInstance(selection, str)
                else:
                    selection, closed = self.ordered_transactions(peer, select, self.p, close)
                    self.assertTrue(selection['single_source_trial']['selection_active'])
                state = self.status()
                self.assertFalse(state['single_source_trial']['selection_active'])
                if close_first:
                    self.reject_selection_unchanged(pair[0])
                else:
                    self.assertEqual(self.select(pair[0]), state)
                self.assertEqual(self.p.dot_journal.cutover.arm_trial(**self.arm_request), state)
                self.assertEqual(self.status()['admission_revision'], closed['admission_revision'])
                self.denied_permit(self.prepare_pair(pair))

    def test_close_authorize_race_does_not_revoke_preexisting_permit(self):
        for close_first in (True, False):
            with self.subTest(close_first=close_first):
                self.fresh_database()
                self.arm()
                pair = self.inbound('authorize-close-race')
                self.select(pair[0])
                prepared = self.prepare_pair(pair)
                peer = self.open_probe()
                boundary = self.status()['boundary_id']
                close = lambda: self.p.dot_journal.cutover.close(boundary, 'single-trial-close')
                authorize = lambda: self.authorize(prepared, peer=peer)
                if close_first:
                    closed, permission = self.ordered_transactions(self.p, close, peer, authorize)
                    self.assertIsInstance(permission, str)
                    self.assertIsNone(self.row(prepared)['authorization_id'])
                else:
                    permission, closed = self.ordered_transactions(peer, authorize, self.p, close)
                    self.assertTrue(permission['send_authorized'])
                    self.assertEqual(self.row(prepared)['authorization_id'], permission['authorization_id'])
                    self.assertFalse(self.authorize(prepared)['send_authorized'])
                    self.assertEqual(self.commit(prepared)['target_dot_message_id'], self.target)
                self.assertFalse(closed['previously_granted_permits_revoked'])
                self.assertFalse(self.status()['single_source_trial']['selection_active'])

    def test_any_prior_unknown_or_authorization_fact_blocks_selection(self):
        self.arm()
        for name, part, fact in [('unknown-original', 'original', 'unknown'),
                                 ('unknown-final', 'final', 'unknown'),
                                 ('ever-issued', 'original', 'ever')]:
            pair = self.inbound(name)
            if part == 'final':
                self.p.weixin.queue_reply(pair[0], 'synthetic reply kept local')
            prepared = self.prepare_pair(pair, part=part)
            if fact == 'unknown':
                self.p.dot_journal.mark_unknown(prepared['journal_id'], prepared['attempt_id'])
            else:
                # Conservative persisted history fact, not a granted fake permit.
                with self.p.db:
                    self.p.db.execute('UPDATE weixin_dot_journal SET authorization_ever_issued=1 WHERE id=?', (prepared['journal_id'],))
            self.reject_selection_unchanged(pair[0])
        pair = self.inbound('clean-selected')
        self.select(pair[0])
        prepared = self.prepare_pair(pair)
        permit = self.authorize(prepared)  # pretend this response was lost
        self.assertTrue(permit['send_authorized'])
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        peer = self.open_probe()
        peer.dot_journal.recover_inflight()
        self.assertEqual(self.row(prepared)['state'], 'unknown')
        self.assertEqual(self.row(prepared)['authorization_id'], permit['authorization_id'])
        self.assertFalse(self.prepare_pair(pair, peer=peer)['preparation_recovered'])
        self.assertFalse(self.authorize(prepared, peer=peer)['send_authorized'])
        self.select(pair[0], peer=peer)  # idempotent selection does not revive the intent
        self.assertFalse(self.authorize(prepared, peer=peer)['send_authorized'])

    def test_interrupted_arm_and_select_transactions_leave_no_half_decision(self):
        args = self.arm_args()
        before = self.status()
        def deny_trial_insert(action, table, column, database, trigger):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == 'weixin_dot_single_trial' else sqlite3.SQLITE_OK
        self.p.db.set_authorizer(deny_trial_insert)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.p.dot_journal.cutover.arm_trial(**args)
        finally:
            self.p.db.set_authorizer(None)
        self.assertEqual(self.status(), before)
        armed = self.p.dot_journal.cutover.arm_trial(**args)
        pair = self.inbound('interrupted-selection')
        before_selection = self.status()
        def deny_revision_update(action, table, column, database, trigger):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE and table == 'weixin_dot_cutover' and column == 'admission_revision' else sqlite3.SQLITE_OK
        self.p.db.set_authorizer(deny_revision_update)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.select(pair[0])
        finally:
            self.p.db.set_authorizer(None)
        self.assertEqual(self.status(), before_selection)
        peer = self.open_probe()
        self.assertEqual(self.status(peer), before_selection)
        self.assertTrue(self.select(pair[0], peer=peer)['single_source_trial']['selection_active'])

    def test_lost_arm_and_select_responses_are_read_only_after_close(self):
        armed = self.arm()
        pair = self.inbound('lost-select-response')
        args = self.select_args(pair[0], armed)
        selected = self.p.dot_journal.cutover.select_trial(**args)
        closed = self.close_trial()
        current = self.status()
        self.assertEqual(self.p.dot_journal.cutover.arm_trial(**self.arm_request), current)
        self.assertEqual(self.p.dot_journal.cutover.select_trial(**args), current)
        self.assertEqual(current['single_source_trial']['selection_revision'], selected['admission_revision'])
        self.assertEqual(current['admission_revision'], closed['admission_revision'])
        self.assertFalse(current['any_new_admission_enabled'])
        with self.assertRaisesRegex(WeixinError, 'dot_cutover_single_source_trial_required'):
            self.p.dot_journal.cutover.enable(current['boundary_id'], self.scope, current['admission_revision'])
        with self.assertRaises(WeixinError):
            self.p.dot_journal.cutover.arm_trial(**dict(self.arm_request, request_id='replacement-trial', expected_revision=current['admission_revision']))
        self.denied_permit(self.prepare_pair(pair))

    def test_normal_receiving_and_reply_remain_independent_and_real_final_slot_is_eligible(self):
        self.arm()
        selected = self.inbound('selected-with-final')
        unrelated = self.inbound('normal-unselected')
        self.select(selected[0])
        original = self.prepare_pair(selected)
        self.assertTrue(self.authorize(original)['send_authorized'])
        with self.assertRaisesRegex(WeixinError, 'reply_not_committed'):
            self.prepare_pair(selected, part='final')
        self.p.weixin.queue_reply(selected[0], 'synthetic final for selected source')
        final = self.prepare_pair(selected, part='final')
        self.assertTrue(self.authorize(final)['send_authorized'])
        self.close_trial()
        self.assertEqual(self.p.weixin.read_message(unrelated[0])['text'], self.text)
        reply = self.p.weixin.queue_reply(unrelated[0], 'synthetic normal independent reply')
        self.assertEqual(reply['send_id'], unrelated[0])
        self.denied_permit(self.prepare_pair(unrelated))
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox WHERE status=?', ('queued',)).fetchone()[0], 2)
        self.assertFalse(self.status()['any_new_admission_enabled'])

    def test_actual_old_rollback_process_and_restart_preserve_trial_guards(self):
        self.arm()
        selected, other = self.inbound('selected-for-old-process'), self.inbound('other-for-old-process')
        self.select(selected[0])
        records = [self.prepare_pair(pair) for pair in (selected, other)]
        root = Path(__file__).resolve().parent / 'fixtures' / 'guarded_rollback'
        self.assertTrue((root / 'probe' / 'probe.py').is_file(), 'actual rollback package is required, not a skip')
        expected_hashes = {name: hashlib.sha256((root / 'probe' / (name + '.py')).read_bytes()).hexdigest()
                           for name in ('probe', 'dotcutover', 'dotjournal')}
        script = r'''
import hashlib, json, socket, sqlite3, sys
from pathlib import Path
def forbidden(*args, **kwargs):
    raise AssertionError('network forbidden in old rollback trial test')
socket.getaddrinfo = socket.create_connection = forbidden
socket.socket.connect = socket.socket.connect_ex = forbidden
request = json.load(sys.stdin)
from probe.probe import Probe, BUILD_VERSION
from probe.weixin import WeixinError
assert BUILD_VERSION.endswith('.guardedrollback1'), BUILD_VERSION
actual = {}
for name in ('probe', 'dotcutover', 'dotjournal'):
    path = Path(sys.modules['probe.' + name].__file__).resolve()
    assert path == Path(request['root']) / 'probe' / (name + '.py'), str(path)
    actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
assert actual == request['hashes']
p = Probe(request['state'], clock=lambda: request['now'])
before = dict(p.db.execute('SELECT * FROM weixin_dot_single_trial').fetchone())
assert p.dot_journal.cutover.status()['state'] == 'closed'
denied = []
for record in request['records']:
    try:
        result = p.dot_journal.authorize(record['journal_id'], record['attempt_id'], record['event_id'])
    except WeixinError as error:
        assert error.kind == 'dot_journal_fence_or_source_admission_pending'
    else:
        assert result['send_authorized'] is False
    denied.append(record['message_id'])
# Execute the actual revision-aware old SQL, bypassing its Python classify.
other = request['records'][1]
try:
    with p.db:
        p.db.execute("UPDATE weixin_dot_journal SET state='authorized',authorization_id=?,authorization_ever_issued=1,updated_at=? WHERE id=? AND state='prepared' AND attempt_id=?", ('e' * 32, request['now'], other['journal_id'], other['attempt_id']))
except sqlite3.IntegrityError as error:
    assert 'single_trial' in str(error), str(error)
else:
    raise AssertionError('old direct authorization SQL bypassed selection')
# A direct INSERT must not manufacture an already-authorized intent either.
try:
    with p.db:
        p.db.execute("""INSERT INTO weixin_dot_journal
            (id,source,account,owner,message_id,event_id,part,attachment_id,target_scope,
             attempt_id,state,lease_until,authorization_id,created_at,updated_at,
             authorization_history_version,authorization_ever_issued)
            SELECT ?,source,account,owner,?,event_id,part,attachment_id,target_scope,
                ?,'authorized',lease_until,?,created_at,updated_at,1,1
            FROM weixin_dot_journal WHERE id=?""",
            ('djm_' + 'f' * 32, 'wx_' + 'f' * 32, 'f' * 32, 'f' * 32, other['journal_id']))
except sqlite3.IntegrityError as error:
    assert 'single_trial_prepare_required' in str(error), str(error)
else:
    raise AssertionError('old direct INSERT manufactured an authorized intent')
assert p.db.execute('SELECT count(*) FROM weixin_dot_journal').fetchone()[0] == 2
def old_enable_denied():
    status = p.dot_journal.cutover.status()
    try:
        p.dot_journal.cutover.enable(status['boundary_id'], request['scope'], status['admission_revision'])
    except sqlite3.IntegrityError as error:
        assert 'single_source_trial_required' in str(error), str(error)
    else:
        raise AssertionError('old revision-aware enable reopened bulk admission')
    assert p.dot_journal.cutover.status() == status
old_enable_denied()
status = p.dot_journal.cutover.status()
closed = p.dot_journal.cutover.close(status['boundary_id'], 'old-process-close')
old_enable_denied()
assert dict(p.db.execute('SELECT * FROM weixin_dot_single_trial').fetchone()) == before
assert all(r[0] is None for r in p.db.execute('SELECT authorization_id FROM weixin_dot_journal'))
assert p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0] == 0
p.close()
print(json.dumps({'loaded_hashes': actual, 'denied_message_ids': denied, 'closed_revision': closed['admission_revision']}))
'''
        env = dict(os.environ, PYTHONPATH='', PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPYCACHEPREFIX=str(Path(self.tmp.name) / 'unused-pyc-cache'))
        child = subprocess.run([sys.executable, '-B', '-c', script], cwd=root, env=env,
            input=json.dumps({'root': str(root), 'hashes': expected_hashes, 'state': self.tmp.name,
                              'now': self.now, 'scope': self.scope, 'records': records}),
            text=True, capture_output=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual(result['loaded_hashes'], expected_hashes)
        self.assertEqual(result['denied_message_ids'], [selected[0], other[0]])
        peer = self.open_probe()
        peer.dot_journal.recover_inflight()
        state = self.status(peer)
        self.assertEqual(state['admission_revision'], result['closed_revision'])
        self.assertEqual(state['single_source_trial']['selected_message_id'], selected[0])
        self.assertFalse(state['single_source_trial']['selection_active'])
        self.assertEqual(self.select(selected[0], peer=peer), state)
        for pair, retired in zip((selected, other), records):
            recovered = self.prepare_pair(pair, peer=peer)
            self.assertNotEqual(recovered['attempt_id'], retired['attempt_id'])
            self.denied_permit(recovered, peer=peer)


if __name__ == '__main__':
    unittest.main()
