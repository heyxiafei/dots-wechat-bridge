"""Future-only steady admission after a closed trial, using synthetic local state.

No Dot/WeChat transport is called. The tests exercise persistent SQLite guards,
source generations, journal attempts, and ordinary reply independence.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

import test_dotjournal as journal_fixture
import test_single_source_trial as trial_fixture
from probe.probe import Probe
from probe.weixin import WeixinError


class SteadyFutureAdmissionTests(unittest.TestCase):
    scope = journal_fixture.DotJournalTests.scope
    target = journal_fixture.DotJournalTests.target
    setUp = journal_fixture.DotJournalTests.setUp
    open_probe = journal_fixture.DotJournalTests.open_probe
    inbound = journal_fixture.DotJournalTests.inbound
    prepare = journal_fixture.DotJournalTests.prepare
    authorize = journal_fixture.DotJournalTests.authorize
    evidence = journal_fixture.DotJournalTests.evidence
    commit = journal_fixture.DotJournalTests.commit
    row = journal_fixture.DotJournalTests.row
    attachment = journal_fixture.DotJournalTests.attachment
    fresh_database = trial_fixture.SingleSourceTrialTests.fresh_database
    status = trial_fixture.SingleSourceTrialTests.status
    arm_args = trial_fixture.SingleSourceTrialTests.arm_args
    arm = trial_fixture.SingleSourceTrialTests.arm
    select_args = trial_fixture.SingleSourceTrialTests.select_args
    select = trial_fixture.SingleSourceTrialTests.select
    close_trial = trial_fixture.SingleSourceTrialTests.close_trial
    prepare_pair = trial_fixture.SingleSourceTrialTests.prepare_pair
    denied_permit = trial_fixture.SingleSourceTrialTests.denied_permit
    rpc = trial_fixture.SingleSourceTrialTests.rpc
    ordered_transactions = trial_fixture.SingleSourceTrialTests.ordered_transactions
    text = 'BODY_CANARY'

    def closed_trial(self, verified=True):
        """Build the real selected trial rows; no fake state toggles."""
        self.arm()
        pair = self.inbound('trial-source')
        self.select(pair[0])
        if verified:
            original = self.prepare_pair(pair)
            self.assertTrue(self.authorize(original)['send_authorized'])
            self.commit(original)
            self.commit(original, source='history_readback')
            self.p.weixin.queue_reply(pair[0], 'synthetic final')
            final = self.prepare_pair(pair, part='final')
            self.assertTrue(self.authorize(final)['send_authorized'])
            self.commit(final, target='Sentinel_' + '2' * 32)
            self.commit(final, source='history_readback', target='Sentinel_' + '2' * 32)
        closed = self.close_trial()
        self.assertFalse(closed['any_new_admission_enabled'])
        return pair

    def enable(self, request_id='steady-generation-one', max_sources=10, peer=None, **changes):
        store = (peer or self.p).dot_journal.cutover
        current = store.status()
        args = {'boundary_id': current['boundary_id'], 'target_scope': self.scope,
                'request_id': request_id, 'expected_revision': current['admission_revision'],
                'max_sources': max_sources}
        args.update(changes)
        return store.enable_steady(**args)

    def pause(self, request_id='steady-pause-one', peer=None, **changes):
        store = (peer or self.p).dot_journal.cutover
        current = store.status()
        args = {'boundary_id': current['boundary_id'], 'request_id': request_id,
                'expected_revision': current['admission_revision']}
        args.update(changes)
        return store.pause_steady(**args)

    def assert_no_permit(self, pair, peer=None, **changes):
        prepared = self.prepare_pair(pair, peer=peer, **changes)
        self.denied_permit(prepared, peer=peer)
        return prepared

    def test_verified_trial_is_required_and_later_sources_can_enter(self):
        old = (self.mid, self.event)
        self.p.dot_journal.cutover.hold('single-trial-boundary')
        reserved = self.inbound('reserved-before-arm')
        self.closed_trial()
        gap = self.inbound('gap-before-steady')
        current = self.status()
        self.assertFalse(current['steady_admission_enabled'])
        self.assertFalse(current['any_new_admission_enabled'])
        enabled = self.enable(max_sources=3)
        self.assertTrue(enabled['steady_admission_enabled'])
        self.assertTrue(enabled['any_new_admission_enabled'])
        self.assertEqual(enabled['steady_generation']['max_sources'], 3)
        self.assertFalse(enabled['remote_legacy_writers_fenced'])
        self.assertTrue(enabled['remote_legacy_direct_write_risk'])
        self.assertFalse(enabled['strict_exactly_once'])
        later = [self.inbound('steady-future-' + str(i)) for i in range(2)]
        for excluded in (old, reserved, gap):
            self.assertFalse(self.p.dot_journal.cutover.classify(excluded[0])['new_write_eligible'])
            self.assert_no_permit(excluded)
        for pair in later:
            self.assertTrue(self.p.dot_journal.cutover.classify(pair[0])['new_write_eligible'])
            prepared = self.prepare_pair(pair)
            first = self.authorize(prepared)
            self.assertTrue(first['send_authorized'])
            self.assertFalse(first['remote_legacy_writers_fenced'])
            self.assertFalse(self.authorize(prepared)['send_authorized'])
        self.assertEqual(self.status()['steady_generation']['source_count'], 2)

    def test_trial_missing_or_unreadback_parts_cannot_open_steady(self):
        self.closed_trial(verified=False)
        before = self.status()
        with self.assertRaises(WeixinError):
            self.enable()
        self.assertEqual(self.status(), before)

        self.fresh_database()
        self.arm()
        pair = self.inbound('trial-with-only-original-readback')
        self.select(pair[0])
        original = self.prepare_pair(pair)
        self.authorize(original)
        self.commit(original)
        self.commit(original, source='history_readback')
        self.p.weixin.queue_reply(pair[0], 'synthetic final')
        final = self.prepare_pair(pair, part='final')
        self.authorize(final)
        self.commit(final, target='Sentinel_' + '2' * 32)
        self.close_trial()
        with self.assertRaises(WeixinError):
            self.enable()
        self.commit(final, source='history_readback', target='Sentinel_' + '2' * 32)
        self.assertTrue(self.enable()['steady_admission_enabled'])

    def test_revision_bump_without_formal_close_cannot_open_steady(self):
        self.arm()
        pair = self.inbound('trial-revision-without-close')
        self.select(pair[0])
        original = self.prepare_pair(pair)
        self.assertTrue(self.authorize(original)['send_authorized'])
        self.commit(original)
        self.commit(original, source='history_readback')
        self.p.weixin.queue_reply(pair[0], 'synthetic final')
        final = self.prepare_pair(pair, part='final')
        self.assertTrue(self.authorize(final)['send_authorized'])
        self.commit(final, target='Sentinel_' + '2' * 32)
        self.commit(final, source='history_readback', target='Sentinel_' + '2' * 32)
        selected_revision = self.status()['admission_revision']
        # Simulate an older or interrupted path that advanced the revision
        # without an actual close decision recorded under a request ID.
        with self.p.db:
            self.p.db.execute('''UPDATE weixin_dot_cutover
                SET admission_revision=admission_revision+1 WHERE singleton=1''')
        self.assertEqual(self.status()['admission_revision'], selected_revision + 1)
        self.assertEqual(self.p.db.execute('''SELECT count(*) FROM weixin_dot_cutover_closures''').fetchone()[0], 0)
        before = self.status()
        with self.assertRaises(WeixinError) as caught:
            self.enable()
        self.assertEqual(caught.exception.kind, 'dot_steady_closed_trial_required')
        self.assertEqual(self.status(), before)

    def test_cap_duplicate_input_and_concurrent_first_ingress(self):
        self.closed_trial()
        self.enable(max_sources=2)
        first = self.inbound('cap-first')
        account = self.p.weixin.account()
        duplicate = {'message_id': 'cap-first', 'message_type': 1,
                     'from_user_id': 'fixture-owner', 'to_user_id': 'fixture-bot',
                     'context_token': 'CONTEXT_CANARY',
                     'item_list': [{'type': 1, 'text_item': {'text': self.text}}]}
        self.assertEqual(self.p.weixin.ingest(account, {'msgs': [duplicate]}), 0)
        peers = [self.open_probe(), self.open_probe()]
        barrier = threading.Barrier(2)
        def insert(peer, source):
            msg = dict(duplicate, message_id=source)
            barrier.wait(timeout=5)
            return peer.weixin.ingest(peer.weixin.account(), {'msgs': [msg]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(insert, peers, ('cap-second-a', 'cap-second-b')))
        self.assertEqual(results, [1, 1])
        current = self.status()
        self.assertEqual(current['steady_generation']['source_count'], 2)
        self.assertEqual(current['steady_generation']['remaining'], 0)
        self.assertTrue(self.p.dot_journal.cutover.classify(first[0])['new_write_eligible'])
        later = [self.p.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()[0]
                 for source in ('cap-second-a', 'cap-second-b')]
        self.assertEqual(sum(self.p.dot_journal.cutover.classify(mid)['new_write_eligible']
                             for mid in later), 1)
        rejected = next(mid for mid in later
                        if not self.p.dot_journal.cutover.classify(mid)['new_write_eligible'])
        # Add only the local event link required by prepare; cap never granted custody.
        with self.p.db:
            self.p.db.execute("UPDATE weixin_inbox SET event_id='evt_fixture_capped' WHERE id=?", (rejected,))
            self.p.db.execute("INSERT INTO pings(id,nonce,sub_id,body,status,name,message_id) VALUES('evt_fixture_capped','','fixture-sub','{}','delivered','weixin.message',?)", (rejected,))
        self.assert_no_permit((rejected, 'evt_fixture_capped'))

    def test_pause_restart_and_unknown_never_reenter_next_generation(self):
        self.closed_trial()
        first_generation = self.enable(max_sources=3)
        pair = self.inbound('steady-unknown-before-restart')
        prepared = self.prepare_pair(pair)
        permit = self.authorize(prepared)
        self.assertTrue(permit['send_authorized'])
        peer = self.open_probe()
        self.assertEqual(peer.dot_journal.cutover.status()['steady_generation']['generation_id'],
                         first_generation['steady_generation']['generation_id'])
        peer.dot_journal.recover_inflight()
        self.assertEqual(self.row(prepared)['state'], 'unknown')
        self.assertEqual(self.row(prepared)['authorization_id'], permit['authorization_id'])
        self.assertFalse(self.authorize(prepared, peer=peer)['send_authorized'])
        paused = self.pause(peer=peer)
        self.assertFalse(paused['steady_admission_enabled'])
        self.assertFalse(paused['any_new_admission_enabled'])
        gap = self.inbound('steady-gap-while-paused')
        resumed = self.enable(request_id='steady-generation-two')
        self.assertNotEqual(resumed['steady_generation']['generation_id'],
                            first_generation['steady_generation']['generation_id'])
        self.assert_no_permit(gap)
        self.assertFalse(self.p.dot_journal.cutover.classify(pair[0])['new_write_eligible'])
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        reingested = self.p.weixin.ingest(self.p.weixin.account(), {'msgs': [{
            'message_id': 'steady-unknown-before-restart', 'message_type': 1,
            'from_user_id': 'fixture-owner', 'to_user_id': 'fixture-bot',
            'context_token': 'CONTEXT_CANARY',
            'item_list': [{'type': 1, 'text_item': {'text': self.text}}]}]})
        self.assertEqual(reingested, 0)
        self.assertFalse(self.p.dot_journal.cutover.classify(pair[0])['new_write_eligible'])
        self.assertEqual(self.status()['steady_generation']['source_count'], 0)
        fresh = self.inbound('steady-after-resume')
        self.assertTrue(self.authorize(self.prepare_pair(fresh))['send_authorized'])

    def test_close_vs_authorize_keeps_issued_permit_but_blocks_later(self):
        for close_first in (True, False):
            with self.subTest(close_first=close_first):
                self.fresh_database()
                self.closed_trial()
                self.enable()
                pair = self.inbound('steady-close-race')
                prepared = self.prepare_pair(pair)
                peer = self.open_probe()
                current = self.status()
                close = lambda: self.p.dot_journal.cutover.close(current['boundary_id'],
                                                                  'steady-close-race-request')
                authorize = lambda: self.authorize(prepared, peer=peer)
                if close_first:
                    closed, attempt = self.ordered_transactions(self.p, close, peer, authorize)
                    self.assertIsInstance(attempt, str)
                    self.assertIsNone(self.row(prepared)['authorization_id'])
                else:
                    attempt, closed = self.ordered_transactions(peer, authorize, self.p, close)
                    self.assertTrue(attempt['send_authorized'])
                    self.assertEqual(self.row(prepared)['authorization_id'], attempt['authorization_id'])
                    self.p.dot_journal.mark_unknown(prepared['journal_id'], prepared['attempt_id'])
                    self.assertEqual(self.row(prepared)['state'], 'unknown')
                self.assertFalse(closed['steady_admission_enabled'])
                self.assertFalse(closed['any_new_admission_enabled'])
                self.assertFalse(closed['previously_granted_permits_revoked'])
                later = self.inbound('steady-after-close')
                self.assert_no_permit(later)

    def test_pause_vs_authorize_both_transaction_orders(self):
        for pause_first in (True, False):
            with self.subTest(pause_first=pause_first):
                self.fresh_database()
                self.closed_trial()
                self.enable()
                pair = self.inbound('steady-pause-race')
                prepared = self.prepare_pair(pair)
                peer = self.open_probe()
                current = self.status()
                pause = lambda: self.p.dot_journal.cutover.pause_steady(
                    current['boundary_id'], 'steady-pause-race-request', current['admission_revision'])
                authorize = lambda: self.authorize(prepared, peer=peer)
                if pause_first:
                    paused, attempt = self.ordered_transactions(self.p, pause, peer, authorize)
                    self.assertIsInstance(attempt, str)
                    self.assertIsNone(self.row(prepared)['authorization_id'])
                else:
                    attempt, paused = self.ordered_transactions(peer, authorize, self.p, pause)
                    self.assertTrue(attempt['send_authorized'])
                    self.assertEqual(self.row(prepared)['authorization_id'], attempt['authorization_id'])
                self.assertFalse(paused['steady_admission_enabled'])
                self.assertFalse(paused['any_new_admission_enabled'])
                self.assertFalse(paused['previously_granted_permits_revoked'])

    def test_old_close_replay_cannot_pause_new_generation(self):
        self.closed_trial()  # records close request_id='single-trial-close'
        started = self.enable()
        pair = self.inbound('steady-after-old-close')
        prepared = self.prepare_pair(pair)
        permit = self.authorize(prepared)
        self.assertTrue(permit['send_authorized'])
        boundary = started['boundary_id']
        replay = self.p.dot_journal.cutover.close(boundary, 'single-trial-close')
        self.assertTrue(replay['close_request_replayed'])
        self.assertFalse(replay['close_effect_is_current'])
        self.assertEqual(replay['admission_revision'], started['admission_revision'])
        self.assertTrue(self.status()['steady_admission_enabled'])
        fresh = self.p.dot_journal.cutover.close(boundary, 'new-steady-close')
        self.assertFalse(fresh['steady_admission_enabled'])
        self.assertEqual(fresh['admission_revision'], started['admission_revision'] + 1)
        self.assertEqual(self.row(prepared)['authorization_id'], permit['authorization_id'])
        self.p.dot_journal.mark_unknown(prepared['journal_id'], prepared['attempt_id'])
        self.assertEqual(self.row(prepared)['state'], 'unknown')
        self.assertFalse(self.authorize(prepared)['send_authorized'])

    def test_old_rollback_worker_close_fails_closed_until_explicit_pause(self):
        self.closed_trial()
        started = self.enable()
        pair = self.inbound('steady-member-before-old-close')
        prepared = self.prepare_pair(pair)
        root = Path(__file__).resolve().parent / 'fixtures' / 'guarded_rollback'
        script = r'''
import json, socket, sqlite3, sys
def forbidden(*args, **kwargs): raise AssertionError('network forbidden')
socket.getaddrinfo = socket.create_connection = forbidden
socket.socket.connect = socket.socket.connect_ex = forbidden
req = json.load(sys.stdin)
from probe.probe import Probe
p = Probe(req['state'], clock=lambda: req['now'])
closed = p.dot_journal.cutover.close(req['boundary_id'], 'old-worker-fresh-close')
assert closed['admission_revision'] == req['revision'] + 1
assert closed['close_request_replayed'] is False
try:
    with p.db:
        p.db.execute("UPDATE weixin_dot_journal SET state='authorized',authorization_id=?,authorization_ever_issued=1,updated_at=? WHERE id=? AND state='prepared'", ('d'*32, req['now'], req['journal_id']))
except sqlite3.IntegrityError:
    pass
else:
    raise AssertionError('old worker bypassed persistent steady guard')
assert p.db.execute('SELECT state FROM weixin_dot_journal WHERE id=?', (req['journal_id'],)).fetchone()[0] == 'prepared'
p.close()
'''
        completed = subprocess.run([sys.executable, '-B', '-c', script], input=json.dumps({
            'state': self.tmp.name, 'now': self.now,
            'boundary_id': started['boundary_id'],
            'revision': started['admission_revision'],
            'journal_id': prepared['journal_id']}), text=True, capture_output=True,
            cwd=root, env=dict(os.environ, PYTHONPATH=str(root)), timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        after_old_close = self.status()
        self.assertEqual(after_old_close['admission_revision'], started['admission_revision'] + 1)
        self.assertEqual(after_old_close['steady_generation']['state'], 'active')
        self.assertFalse(after_old_close['steady_generation']['admission_active'])
        self.assertFalse(after_old_close['steady_admission_enabled'])
        self.assertFalse(self.p.dot_journal.cutover.classify(pair[0])['new_write_eligible'])
        self.denied_permit(prepared)
        gap = self.inbound('steady-gap-after-old-close')
        with self.assertRaises(WeixinError) as caught:
            self.enable(request_id='cannot-implicitly-resume-old-worker-close')
        self.assertEqual(caught.exception.kind, 'dot_steady_prior_generation_requires_pause')
        paused = self.pause(request_id='explicit-pause-after-old-close')
        self.assertEqual(paused['steady_generation']['state'], 'paused')
        resumed = self.enable(request_id='new-generation-after-old-close')
        self.assertNotEqual(resumed['steady_generation']['generation_id'],
                            started['steady_generation']['generation_id'])
        self.assert_no_permit(gap)
        self.assert_no_permit(pair)
        fresh = self.inbound('steady-new-after-old-close')
        self.assertTrue(self.authorize(self.prepare_pair(fresh))['send_authorized'])

    def test_old_pause_replay_cannot_pause_later_generation(self):
        self.closed_trial()
        first = self.enable()
        paused = self.pause(request_id='pause-first-generation')
        self.assertFalse(paused['steady_admission_enabled'])
        second = self.enable(request_id='second-after-pause')
        self.assertNotEqual(first['steady_generation']['generation_id'],
                            second['steady_generation']['generation_id'])
        pair = self.inbound('source-after-old-pause')
        replay = self.p.dot_journal.cutover.pause_steady(
            second['boundary_id'], 'pause-first-generation', first['admission_revision'])
        self.assertTrue(replay['steady_pause_request_replayed'])
        self.assertFalse(replay['steady_pause_effect_is_current'])
        self.assertEqual(replay['steady_pause_applied_revision'], paused['admission_revision'])
        self.assertEqual(self.status()['admission_revision'], second['admission_revision'])
        self.assertTrue(self.status()['steady_admission_enabled'])
        self.assertTrue(self.authorize(self.prepare_pair(pair))['send_authorized'])

    def test_incomplete_capture_blocks_same_prepared_attempt_until_complete(self):
        self.closed_trial()
        self.enable()
        pair = self.inbound('steady-incomplete-capture')
        row = self.p.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?',
                                (pair[0],)).fetchone()
        original_manifest = row['metadata_json']
        incomplete = json.loads(original_manifest)
        incomplete['complete'] = False
        incomplete['issue_codes'] = ['synthetic_capture_interrupted']
        with self.p.db:
            self.p.db.execute('UPDATE weixin_inbound_manifests SET metadata_json=? WHERE message_id=?',
                              (json.dumps(incomplete), pair[0]))
        prepared = self.prepare_pair(pair)
        self.denied_permit(prepared)
        self.assertEqual(self.row(prepared)['state'], 'prepared')
        # A string is truthy in Python but is not a verified boolean capture.
        # Keep the recorded parts intact to isolate this type confusion.
        malformed = json.loads(original_manifest)
        malformed['complete'] = 'false'
        with self.p.db:
            self.p.db.execute('UPDATE weixin_inbound_manifests SET metadata_json=? WHERE message_id=?',
                              (json.dumps(malformed), pair[0]))
        self.denied_permit(prepared)
        self.assertEqual(self.row(prepared)['state'], 'prepared')
        with self.p.db:
            self.p.db.execute('UPDATE weixin_inbound_manifests SET metadata_json=? WHERE message_id=?',
                              (original_manifest, pair[0]))
        recovered = self.prepare_pair(pair)
        self.assertEqual(recovered['attempt_id'], prepared['attempt_id'])
        self.assertTrue(self.authorize(recovered)['send_authorized'])

    def test_attachment_parts_and_normal_reply_are_independent(self):
        self.closed_trial()
        self.enable()
        items = [{'type': 1, 'text_item': {'text': self.text}},
                 {'type': 4, 'file_item': {'file_name': 'first.txt'}},
                 {'type': 4, 'file_item': {'file_name': 'second.txt'}}]
        pair = self.inbound('steady-two-attachments', item_list=items)
        self.assertTrue(self.p.weixin.inbound_completeness(pair[0])['complete'])
        attachment_rows = self.p.media.list_for_message(pair[0])
        self.assertEqual(len(attachment_rows), 2)
        aids = [r['attachment_id'] for r in attachment_rows]
        original = self.prepare_pair(pair)
        self.assertTrue(self.authorize(original)['send_authorized'])
        parts = [self.prepare_pair(pair, attachment_id=aid) for aid in aids]
        for state in ('queued', 'downloading', 'error', 'expired'):
            with self.p.db:
                self.p.db.execute('UPDATE weixin_attachments SET status=?,expires_at=? WHERE id=?',
                                  (state, self.now + 100, aids[0]))
            self.denied_permit(parts[0])
        self.denied_permit(parts[1])
        for size, digest, expiry in ((None, 'a' * 64, self.now + 100),
                                     (3, None, self.now + 100),
                                     (3, 'a' * 64, self.now - 1)):
            with self.p.db:
                self.p.db.execute("UPDATE weixin_attachments SET status='cached',size=?,sha256=?,expires_at=? WHERE id=?",
                                  (size, digest, expiry, aids[0]))
            self.denied_permit(parts[0])
        # Local synthetic media metadata represents two independently cached
        # nonexpired parts. No network or real attachment bytes are read.
        with self.p.db:
            self.p.db.execute("UPDATE weixin_attachments SET status='cached',size=3,sha256=?,expires_at=? WHERE id=?",
                              ('a' * 64, self.now + 100, aids[0]))
        self.assertTrue(self.authorize(parts[0])['send_authorized'])
        self.commit(parts[0], target='Sentinel_' + '3' * 32)
        self.p.dot_journal.mark_unknown(parts[1]['journal_id'], parts[1]['attempt_id'])
        self.assertFalse(self.authorize(parts[1])['send_authorized'])
        states = {r['attachment_id']: r['state'] for r in self.p.dot_journal.for_message(pair[0])['records']}
        self.assertEqual(states[None], 'authorized')
        self.assertEqual(states[aids[0]], 'committed')
        self.assertEqual(states[aids[1]], 'unknown')
        ordinary = self.inbound('steady-normal-reply')
        self.pause()
        reply = self.p.weixin.queue_reply(ordinary[0], 'ordinary final remains independent')
        self.assertEqual(reply['send_id'], ordinary[0])
        self.assertFalse(self.status()['any_new_admission_enabled'])

    def test_rpc_contract_rejects_invalid_or_stale_changes(self):
        self.closed_trial()
        status = self.status()
        args = {'boundary_id': status['boundary_id'], 'target_scope': self.scope,
                'request_id': 'rpc-steady-generation',
                'expected_revision': status['admission_revision'], 'max_sources': 2}
        tools = {tool['name']: tool for tool in Probe.tools()}
        enable_name = 'enable_weixin_dot_steady_future_sources'
        pause_name = 'pause_weixin_dot_steady_future_sources'
        self.assertEqual(set(tools[enable_name]['inputSchema']['required']), set(args))
        self.assertFalse(tools[enable_name]['inputSchema']['additionalProperties'])
        for changes in ({'max_sources': True}, {'max_sources': 0}, {'max_sources': 1001},
                        {'expected_revision': 0.0}, {'request_id': ''}, {'extra': 'forbidden'}):
            with self.subTest(changes=changes):
                self.assertIn('error', self.rpc(enable_name, dict(args, **changes)))
                self.assertEqual(self.status(), status)
        response = self.rpc(enable_name, args)
        self.assertNotIn('error', response)
        enabled = response['result']['structuredContent']
        self.assertTrue(enabled['steady_admission_enabled'])
        self.assertFalse(enabled['steady_enable_request_replayed'])
        replay = self.rpc(enable_name, args)['result']['structuredContent']
        self.assertTrue(replay['steady_enable_request_replayed'])
        self.assertEqual(replay['steady_enable_applied_revision'], enabled['steady_enable_applied_revision'])
        self.assertEqual(replay['steady_generation'], enabled['steady_generation'])
        self.assertEqual(self.status()['admission_revision'], enabled['admission_revision'])
        pause_args = {'boundary_id': status['boundary_id'],
                      'request_id': 'rpc-steady-pause',
                      'expected_revision': enabled['admission_revision']}
        self.assertEqual(set(tools[pause_name]['inputSchema']['required']), set(pause_args))
        self.assertIn('error', self.rpc(pause_name, dict(pause_args, extra='forbidden')))
        paused = self.rpc(pause_name, pause_args)['result']['structuredContent']
        self.assertFalse(paused['any_new_admission_enabled'])
        self.assertFalse(paused['steady_pause_request_replayed'])
        pause_replay = self.rpc(pause_name, pause_args)['result']['structuredContent']
        self.assertTrue(pause_replay['steady_pause_request_replayed'])
        self.assertEqual(pause_replay['steady_pause_applied_revision'], paused['steady_pause_applied_revision'])
        self.assertEqual(pause_replay['steady_generation'], paused['steady_generation'])
        with self.assertRaises(WeixinError):
            self.enable(request_id='stale-revision', expected_revision=status['admission_revision'])

    def test_persistent_sql_guard_blocks_old_worker_and_raw_authorize(self):
        self.closed_trial()
        self.enable()
        pair = self.inbound('steady-member-for-old-worker')
        prepared = self.prepare_pair(pair)
        # Even a current process cannot set authorized without the new versioned
        # generation marker; old code has no column or method to supply it.
        with self.assertRaises(sqlite3.IntegrityError), self.p.db:
            self.p.db.execute("""UPDATE weixin_dot_journal SET state='authorized',authorization_id=?,
                authorization_ever_issued=1,updated_at=? WHERE id=? AND state='prepared'""",
                ('f' * 32, self.now, prepared['journal_id']))
        self.assertEqual(self.row(prepared)['state'], 'prepared')
        root = Path(__file__).resolve().parent / 'fixtures' / 'guarded_rollback'
        self.assertTrue((root / 'probe' / 'probe.py').is_file())
        script = r'''
import json, socket, sqlite3, sys
def forbidden(*args, **kwargs): raise AssertionError('network forbidden')
socket.getaddrinfo = socket.create_connection = forbidden
socket.socket.connect = socket.socket.connect_ex = forbidden
req = json.load(sys.stdin)
from probe.probe import Probe
from probe.weixin import WeixinError
p = Probe(req['state'], clock=lambda: req['now'])
record = req['record']
try:
    result = p.dot_journal.authorize(record['journal_id'], record['attempt_id'], record['event_id'])
except WeixinError:
    pass
else:
    assert result['send_authorized'] is False
try:
    with p.db:
        p.db.execute("UPDATE weixin_dot_journal SET state='authorized',authorization_id=?,authorization_ever_issued=1,updated_at=? WHERE id=? AND state='prepared'", ('e'*32, req['now'], record['journal_id']))
except sqlite3.IntegrityError:
    pass
else:
    raise AssertionError('old raw SQL bypassed steady marker guard')
assert p.db.execute('SELECT state FROM weixin_dot_journal WHERE id=?', (record['journal_id'],)).fetchone()[0] == 'prepared'
p.close()
'''
        env = dict(os.environ, PYTHONPATH=str(root))
        completed = subprocess.run([sys.executable, '-B', '-c', script], input=json.dumps({
            'state': self.tmp.name, 'now': self.now, 'record': {
                'journal_id': prepared['journal_id'], 'attempt_id': prepared['attempt_id'],
                'event_id': prepared['event_id']}}), text=True, capture_output=True,
            cwd=root, env=env, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.row(prepared)['state'], 'prepared')

    def test_authorized_generation_marker_is_immutable(self):
        self.closed_trial()
        enabled = self.enable()
        pair = self.inbound('steady-member-marker-lock')
        prepared = self.prepare_pair(pair)
        permit = self.authorize(prepared)
        self.assertTrue(permit['send_authorized'])
        marker = self.row(prepared)['steady_generation_id']
        self.assertEqual(marker, enabled['steady_generation']['generation_id'])
        for substitute in (None, 'dsg_' + 'e' * 32):
            with self.subTest(substitute=substitute):
                with self.assertRaises(sqlite3.IntegrityError), self.p.db:
                    self.p.db.execute('''UPDATE weixin_dot_journal
                        SET steady_generation_id=? WHERE id=?''',
                        (substitute, prepared['journal_id']))
                self.assertEqual(self.row(prepared)['steady_generation_id'], marker)
                self.assertEqual(self.row(prepared)['authorization_id'], permit['authorization_id'])

    def test_unknown_stronger_authorize_guard_blocks_migration_and_survives(self):
        trigger = 'weixin_dot_trial_authorize_guard'
        stronger_sql = '''CREATE TRIGGER weixin_dot_trial_authorize_guard
            BEFORE UPDATE ON weixin_dot_journal
            WHEN NEW.state='authorized' AND OLD.state!='authorized'
            BEGIN SELECT RAISE(ABORT,'fixture_all_authorize_denied'); END'''
        with self.p.db:
            self.p.db.execute('DROP TRIGGER ' + trigger)
            self.p.db.execute(stronger_sql)
        saved_sql = self.p.db.execute('''SELECT sql FROM sqlite_master
            WHERE type='trigger' AND name=?''', (trigger,)).fetchone()['sql']
        self.assertEqual(saved_sql, stronger_sql)
        with self.assertRaises(WeixinError) as caught:
            self.open_probe()
        self.assertEqual(caught.exception.kind, 'dot_steady_unknown_authorize_guard')
        # A failed constructor must not replace a preexisting stronger rule.
        persisted_sql = self.p.db.execute('''SELECT sql FROM sqlite_master
            WHERE type='trigger' AND name=?''', (trigger,)).fetchone()['sql']
        self.assertEqual(persisted_sql, saved_sql)


if __name__ == '__main__':
    unittest.main()
