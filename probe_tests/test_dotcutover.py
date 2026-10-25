"""Bounded local ownership fixtures; no real remote fence or message sends."""
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from concurrent.futures import ThreadPoolExecutor

import test_dotjournal as fixtures
from probe.dotcutover import DotCutoverStore, cutover_tools, CUTOVER_VERSION
from probe.weixin import WeixinError, MESSAGE_EVENT, encoded


class LocalCutoverTests(unittest.TestCase):
    # Reuse helpers, not the 32 inherited tests or an imported TestCase alias.
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

    def hold(self):
        return self.p.dot_journal.cutover.hold('synthetic-local-cutover')

    def enable(self, boundary=None, **changes):
        boundary = boundary or self.hold()
        args = {'boundary_id': boundary['boundary_id'], 'target_scope': self.scope,
                'expected_revision': boundary['admission_revision']}
        args.update(changes)
        return self.p.dot_journal.cutover.enable(**args)

    def close_admission(self, request_id='synthetic-close', peer=None):
        store = (peer or self.p).dot_journal.cutover
        return store.close(store.status()['boundary_id'], request_id)

    def new_source(self, source='future-one'):
        self.mid, self.event = self.inbound(source)
        return self.prepare()

    def ingest_on(self, p, source):
        p.weixin.ingest(p.weixin.account(), {'msgs': [{
            'message_id': source, 'message_type': 1, 'from_user_id': 'fixture-owner',
            'to_user_id': 'fixture-bot', 'context_token': 'CONTEXT_CANARY',
            'item_list': [{'type': 1, 'text_item': {'text': 'BODY_CANARY'}}]}]})
        return p.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()[0]

    def test_hold_snapshots_all_old_ids_including_unlinked_pending_media(self):
        pending, _ = self.inbound('old-without-event', linked=False)
        self.attachment('att_' + 'a' * 32, message_id=pending)
        with self.p.db:
            self.p.db.execute("UPDATE weixin_attachments SET status='queued' WHERE message_id=?", (pending,))
        boundary = self.hold()
        self.assertEqual(boundary['state'], 'closed')
        self.assertIsNone(boundary['target_scope'])
        self.assertFalse(boundary['new_admission_enabled'])
        self.assertFalse(boundary['remote_legacy_writers_fenced'])
        self.assertEqual((boundary['snapshot_count'], boundary['legacy_count']), (2, 2))
        ids = sorted([self.mid, pending])
        self.assertEqual(boundary['snapshot_hash'], hashlib.sha256(encoded(ids).encode()).hexdigest())
        for mid in ids:
            self.assertEqual(self.p.dot_journal.cutover.classify(mid)['classification'], 'legacy')

    def test_hold_idempotent_and_does_not_recapture_reserved_new_ids(self):
        first = self.hold()
        new = self.new_source()
        second = self.hold()
        self.assertEqual(first['boundary_id'], second['boundary_id'])
        self.assertEqual(first['snapshot_hash'], second['snapshot_hash'])
        self.assertEqual(second['snapshot_count'], 1)
        self.assertEqual(second['reserved_new_count'], 1)
        self.assertEqual(self.p.dot_journal.cutover.classify(new['message_id'])['classification'], 'reserved_new')
        with self.assertRaisesRegex(WeixinError, 'already_exists'):
            self.p.dot_journal.cutover.hold('different-request')

    def test_closed_boundary_reserves_new_key_but_never_authorizes_it(self):
        self.hold()
        prepared = self.new_source()
        scope = self.p.dot_journal.cutover.classify(prepared['message_id'])
        self.assertEqual(scope['classification'], 'reserved_new')
        self.assertFalse(scope['new_write_eligible'])
        with self.assertRaisesRegex(WeixinError, 'admission_pending'):
            self.authorize(prepared)
        self.assertEqual(self.row(prepared)['state'], 'prepared')
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_enabled_exact_new_key_gets_once_only_bounded_permit_and_receipt(self):
        boundary = self.hold()
        prepared = self.new_source()
        enabled = self.enable(boundary)
        self.assertTrue(enabled['new_admission_enabled'])
        self.assertFalse(enabled['remote_legacy_writers_fenced'])
        permit = self.authorize(prepared)
        self.assertTrue(permit['send_authorized'])
        self.assertEqual(permit['admission_mode'], 'bounded_local_ownership')
        self.assertTrue(permit['remote_legacy_direct_write_risk'])
        self.assertFalse(permit['remote_api_fenced_by_bridge'])
        self.assertFalse(permit['remote_legacy_writers_fenced'])
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        receipt = self.commit(prepared)
        self.assertEqual(receipt['state'], 'committed')
        self.assertEqual(self.commit(prepared)['target_dot_message_id'], self.target)
        self.assertFalse(self.prepare()['external_write_allowed'])

    def test_old_and_duplicate_source_never_becomes_a_new_key(self):
        old_mid, old_event = self.mid, self.event
        boundary = self.hold()
        self.enable(boundary)
        prepared = self.prepare()
        with self.assertRaisesRegex(WeixinError, 'admission_pending'):
            self.authorize(prepared)
        self.ingest_on(self.p, 'old-one')
        self.assertEqual(self.p.dot_journal.cutover.classify(old_mid)['classification'], 'legacy')
        self.assertEqual(self.p.dot_journal.cutover.status()['reserved_new_count'], 0)
        self.assertEqual(self.row(prepared)['event_id'], old_event)

    def test_old_and_new_unknown_records_are_never_reset_by_enable(self):
        old = self.prepare()
        self.p.dot_journal.mark_unknown(old['journal_id'], old['attempt_id'])
        boundary = self.hold()
        new = self.new_source()
        self.p.dot_journal.mark_unknown(new['journal_id'], new['attempt_id'])
        self.enable(boundary)
        for record in (old, new):
            self.assertFalse(self.authorize(record)['send_authorized'])
            self.assertEqual(self.row(record)['state'], 'unknown')
        self.assertFalse(self.prepare()['first_prepare'])
        self.assertEqual(self.prepare()['attempt_id'], new['attempt_id'])

    def test_boundary_and_enabled_scope_are_immutable(self):
        boundary = self.hold()
        with self.assertRaisesRegex(WeixinError, 'boundary_mismatch'):
            self.enable(boundary, boundary_id='djc_' + 'f' * 32)
        first = self.enable(boundary)
        self.assertEqual(self.enable(boundary), first)
        with self.assertRaisesRegex(WeixinError, 'scope_conflict'):
            self.enable(boundary, target_scope='another-room')
        self.mid, self.event = self.inbound('scope-conflict')
        wrong = self.prepare(target_scope='another-room')
        with self.assertRaisesRegex(WeixinError, 'admission_pending'):
            self.authorize(wrong)
        self.assertEqual(self.p.dot_journal.cutover.status()['target_scope'], self.scope)

    def test_owner_change_cannot_reuse_boundary_or_read_its_state(self):
        boundary = self.hold()
        with self.p.db:
            self.p.db.execute("UPDATE weixin_accounts SET owner='fixture-other'")
        for call in (self.p.dot_journal.cutover.status,
                     lambda: self.p.dot_journal.cutover.enable(boundary['boundary_id'], self.scope, 0),
                     lambda: self.p.dot_journal.cutover.classify(self.mid)):
            with self.assertRaisesRegex(WeixinError, 'owner_mismatch'):
                call()

    def test_new_owner_source_still_requires_exact_event_and_attachment(self):
        boundary = self.hold()
        self.enable(boundary)
        self.mid, self.event = self.inbound('new-identity')
        with self.assertRaisesRegex(WeixinError, 'event_mismatch'):
            self.prepare(event_id='wrong-event')
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_attachment'):
            self.prepare(attachment_id='att_' + 'b' * 32)
        other, _ = self.inbound('new-other')
        self.attachment('att_' + 'c' * 32, message_id=other)
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_attachment'):
            self.prepare(attachment_id='att_' + 'c' * 32)
        self.assertEqual(self.inbound('outsider', from_user_id='fixture-other'), (None, None))

    def test_old_ledger_new_prepares_fenced_but_old_receipt_can_reconcile(self):
        legacy = self.p.dot_mirrors.prepare(self.mid, self.event, 'original')
        self.assertTrue(legacy['claim_granted'])
        boundary = self.hold()
        repeated = self.p.dot_mirrors.prepare(self.mid, self.event, 'original')
        self.assertFalse(repeated['claim_granted'])
        committed = self.p.dot_mirrors.commit(legacy['mirror_id'], legacy['attempt_id'], self.event,
                                            self.target, 'history_readback')
        self.assertEqual(committed['state'], 'committed')
        self.mid, self.event = self.inbound('new-for-old-tool')
        with self.assertRaisesRegex(WeixinError, 'legacy_prepare_fenced'):
            self.p.dot_mirrors.prepare(self.mid, self.event, 'original')
        self.enable(boundary)
        with self.assertRaisesRegex(WeixinError, 'legacy_prepare_fenced'):
            self.p.dot_mirrors.prepare(self.mid, self.event, 'original')

    def test_racing_old_prepare_and_hold_never_grants_a_post_hold_new_claim(self):
        second = self.open_probe(verifier=False)
        barrier = threading.Barrier(2)
        def legacy():
            barrier.wait()
            try:
                return second.dot_mirrors.prepare(self.mid, self.event, 'original')
            except WeixinError as error:
                return error.kind
        def hold():
            barrier.wait()
            return self.hold()
        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(legacy)
            held = pool.submit(hold)
            result, boundary = old.result(), held.result()
        self.assertEqual(boundary['legacy_count'], 1)
        self.assertEqual(self.p.dot_journal.cutover.classify(self.mid)['classification'], 'legacy')
        if isinstance(result, dict):
            self.assertTrue(result['claim_granted'])  # Prior local grant remains a disclosed risk.
            self.assertFalse(self.p.dot_mirrors.prepare(self.mid, self.event, 'original')['claim_granted'])
        else:
            self.assertEqual(result, 'dot_journal_legacy_prepare_fenced')
            with self.assertRaisesRegex(WeixinError, 'legacy_prepare_fenced'):
                self.p.dot_mirrors.prepare(self.mid, self.event, 'original')

    def test_concurrent_hold_and_ingress_partition_every_id_exactly_once(self):
        second = self.open_probe(verifier=False)
        barrier = threading.Barrier(2)
        def ingress():
            barrier.wait()
            return [self.ingest_on(second, 'concurrent-' + str(n)) for n in range(20)]
        def hold():
            barrier.wait()
            return self.hold()
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(ingress)
            held = pool.submit(hold)
            ids, boundary = future.result(), held.result()
        members = self.p.db.execute('SELECT message_id,classification FROM weixin_dot_cutover_sources').fetchall()
        self.assertEqual({r[0] for r in members}, {self.mid, *ids})
        self.assertEqual(len(members), 21)
        legacy_ids = sorted(r[0] for r in members if r[1] == 'legacy')
        self.assertEqual(len(legacy_ids), boundary['snapshot_count'])
        self.assertEqual(hashlib.sha256(encoded(legacy_ids).encode()).hexdigest(), boundary['snapshot_hash'])
        self.assertTrue(all(r[1] in ('legacy', 'reserved_new') for r in members))

    def test_restart_keeps_boundary_but_preserves_unknown_send_safety(self):
        boundary = self.hold()
        prepared = self.new_source()
        self.enable(boundary)
        self.authorize(prepared)
        restarted = self.open_probe(verifier=False)
        self.assertEqual(restarted.dot_journal.cutover.status()['state'], 'new_only')
        restarted.dot_journal.recover_inflight()
        self.assertEqual(restarted.dot_journal.cutover.status()['boundary_id'], boundary['boundary_id'])
        self.assertEqual(restarted.dot_journal.cutover.classify(prepared['message_id'])['classification'], 'reserved_new')
        self.assertEqual(self.row(prepared)['state'], 'unknown')
        self.assertFalse(self.authorize(prepared, peer=restarted)['send_authorized'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_status_and_classification_are_readonly_and_do_not_disclose_bodies(self):
        self.hold()
        self.new_source()
        before = self.p.db.total_changes
        self.p.db.execute('PRAGMA query_only=ON')
        try:
            status = self.p.dot_journal.cutover.status()
            classification = self.p.dot_journal.cutover.classify(self.mid)
            journal = self.p.dot_journal.for_message(self.mid)
        finally:
            self.p.db.execute('PRAGMA query_only=OFF')
        self.assertEqual(self.p.db.total_changes, before)
        safe = json.dumps([status, classification, journal])
        for canary in ('BODY_CANARY', 'TOKEN_CANARY', 'CONTEXT_CANARY', 'fixture-owner'):
            self.assertNotIn(canary, safe)
        self.assertFalse(journal['external_write_allowed'])
        self.assertFalse(classification['remote_legacy_writers_fenced'])

    def test_tool_contract_arguments_and_read_only_boundary_discovery(self):
        definitions = {t['name']: t for t in cutover_tools()}
        self.assertEqual(set(definitions), {'get_weixin_dot_cutover_status',
            'enable_weixin_dot_cutover_new_sources', 'close_weixin_dot_cutover_new_sources',
            'arm_weixin_dot_single_source_trial', 'select_weixin_dot_single_source_trial',
            'enable_weixin_dot_steady_future_sources', 'pause_weixin_dot_steady_future_sources'})
        self.assertTrue(definitions['get_weixin_dot_cutover_status']['annotations']['readOnlyHint'])
        self.assertFalse(definitions['enable_weixin_dot_cutover_new_sources']['annotations']['readOnlyHint'])
        self.assertFalse(definitions['close_weixin_dot_cutover_new_sources']['annotations']['readOnlyHint'])
        self.assertEqual(self.p.dot_journal.cutover.call('get_weixin_dot_cutover_status', {})['state'], 'not_staged')
        for name, args in [('get_weixin_dot_cutover_status', {'extra': True}),
                           ('hold_weixin_dot_cutover', {}),
                           ('enable_weixin_dot_cutover_new_sources', {}),
                           ('enable_weixin_dot_cutover_new_sources', {'boundary_id': 'wrong', 'target_scope': self.scope}),
                           ('close_weixin_dot_cutover_new_sources', {}),
                           ('close_weixin_dot_cutover_new_sources', {'boundary_id': 'wrong'}),
                           ('get_weixin_dot_cutover_status', None)]:
            with self.assertRaises(WeixinError):
                self.p.dot_journal.cutover.call(name, args)
        boundary = self.hold()
        result = self.p.dot_journal.cutover.call('enable_weixin_dot_cutover_new_sources',
                                                {'boundary_id': boundary['boundary_id'], 'target_scope': self.scope,
                                                 'expected_revision': boundary['admission_revision']})
        self.assertEqual(result['cutover_version'], CUTOVER_VERSION)
        self.assertEqual(result['state'], 'new_only')
        self.assertFalse(result['remote_legacy_writers_fenced'])

    def test_close_preserves_receipts_sources_scope_and_old_prepare_guard(self):
        boundary = self.hold()
        one = self.new_source('committed-before-close')
        self.enable(boundary)
        self.authorize(one)
        self.commit(one)
        pending = self.new_source('prepared-before-close')
        ids = [tuple(r) for r in self.p.db.execute('SELECT * FROM weixin_dot_cutover_sources ORDER BY message_id')]
        receipt = dict(self.row(one))
        closed = self.close_admission()
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(closed['target_scope'], self.scope)
        self.assertEqual(closed['boundary_id'], boundary['boundary_id'])
        self.assertEqual(closed['snapshot_hash'], boundary['snapshot_hash'])
        self.assertFalse(closed['previously_granted_permits_revoked'])
        self.assertEqual(ids, [tuple(r) for r in self.p.db.execute('SELECT * FROM weixin_dot_cutover_sources ORDER BY message_id')])
        self.assertEqual(receipt, dict(self.row(one)))
        with self.assertRaisesRegex(WeixinError, 'admission_pending'):
            self.authorize(pending)
        with self.assertRaisesRegex(WeixinError, 'legacy_prepare_fenced'):
            self.p.dot_mirrors.prepare(self.mid, self.event, 'original')

    def test_close_does_not_revoke_granted_permit_or_prevent_same_target_commit(self):
        boundary = self.hold()
        prepared = self.new_source()
        self.enable(boundary)
        permit = self.authorize(prepared)
        closed = self.close_admission()
        self.assertFalse(closed['previously_granted_permits_revoked'])
        self.assertEqual(self.row(prepared)['authorization_id'], permit['authorization_id'])
        self.assertEqual(self.row(prepared)['state'], 'authorized')
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        self.assertEqual(self.commit(prepared)['target_dot_message_id'], self.target)

    def test_close_of_initially_closed_boundary_invalidates_old_enable_revision(self):
        boundary = self.hold()
        closed = self.close_admission()
        self.assertEqual(closed['admission_revision'], boundary['admission_revision'] + 1)
        with self.assertRaisesRegex(WeixinError, 'revision_conflict'):
            self.enable(boundary)
        enabled = self.enable(closed)
        self.assertEqual(enabled['state'], 'new_only')

    def test_lost_close_response_retry_after_reenable_never_undoes_new_decision(self):
        boundary = self.hold()
        self.enable(boundary)
        closed = self.close_admission('close-1')
        duplicate = self.close_admission('close-1')
        self.assertTrue(duplicate['close_request_replayed'])
        self.assertEqual(duplicate['admission_revision'], closed['admission_revision'])
        self.assertEqual(duplicate['close_applied_revision'], closed['admission_revision'])
        enabled = self.enable(closed)
        late = self.close_admission('close-1')
        self.assertEqual(late['state'], 'new_only')
        self.assertEqual(late['admission_revision'], enabled['admission_revision'])
        self.assertEqual(late['close_applied_revision'], closed['admission_revision'])
        self.assertFalse(late['close_effect_is_current'])
        fresh = self.close_admission('close-2')
        self.assertEqual(fresh['state'], 'closed')
        self.assertEqual(fresh['admission_revision'], enabled['admission_revision'] + 1)

    def test_close_is_durable_across_restart_and_duplicate_operation(self):
        self.enable()
        closed = self.close_admission()
        restarted = self.open_probe(verifier=False)
        restarted.dot_journal.recover_inflight()
        self.assertEqual(restarted.dot_journal.cutover.status()['state'], 'closed')
        replayed = self.close_admission(peer=restarted)
        self.assertTrue(replayed['close_request_replayed'])
        self.assertEqual(replayed['admission_revision'], closed['admission_revision'])
        self.assertEqual(replayed['close_applied_revision'], closed['admission_revision'])

    def test_closed_admission_keeps_inbox_and_normal_reply_commit_independent(self):
        self.enable()
        self.close_admission()
        self.mid, self.event = self.inbound('normal-after-close')
        read = self.p.weixin.read_message(self.mid)
        self.assertEqual(read['text'], 'BODY_CANARY')
        reply = self.p.weixin.queue_reply(self.mid, 'synthetic final without sending')
        self.assertEqual(reply['send_id'], self.mid)
        self.assertEqual(self.p.db.execute('SELECT status FROM weixin_outbox WHERE message_id=?', (self.mid,)).fetchone()[0], 'queued')
        self.assertEqual(self.p.dot_journal.cutover.status()['state'], 'closed')

    def test_old_enable_rpc_and_old_sql_cannot_reopen_closed_database(self):
        boundary = self.hold()
        self.enable(boundary)
        closed = self.close_admission()
        with self.assertRaisesRegex(WeixinError, 'invalid_dot_cutover_arguments'):
            self.p.dot_journal.cutover.call('enable_weixin_dot_cutover_new_sources',
                {'boundary_id': boundary['boundary_id'], 'target_scope': self.scope})
        # Exact pre-revision enable statement, exercised through a second connection.
        other = self.open_probe(verifier=False)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'enable_revision_required'), other.db:
            other.db.execute("UPDATE weixin_dot_cutover SET state='new_only',target_scope=?,enabled_at=? WHERE singleton=1 AND state='closed'", (self.scope, self.now))
        self.assertEqual(other.dot_journal.cutover.status()['admission_revision'], closed['admission_revision'])
        self.assertEqual(other.dot_journal.cutover.status()['state'], 'closed')

    def test_missing_or_malformed_close_identity_never_changes_state(self):
        boundary = self.hold()
        self.enable(boundary)
        before = self.p.dot_journal.cutover.status()
        for request in (None, '', True, ' ', 'x' * 257):
            with self.assertRaisesRegex(WeixinError, 'invalid_dot_cutover_arguments'):
                self.p.dot_journal.cutover.close(boundary['boundary_id'], request)
        with self.assertRaisesRegex(WeixinError, 'boundary_mismatch'):
            self.p.dot_journal.cutover.close('djc_' + 'f' * 32, 'close-other')
        self.assertEqual(self.p.dot_journal.cutover.status(), before)

    def test_scope_cannot_change_after_close_via_new_or_old_code(self):
        self.enable()
        closed = self.close_admission()
        with self.assertRaisesRegex(WeixinError, 'scope_conflict'):
            self.enable(closed, target_scope='different-scope')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'scope_conflict'), self.p.db:
            self.p.db.execute("UPDATE weixin_dot_cutover SET target_scope='different-scope'")
        self.assertEqual(self.p.dot_journal.cutover.status()['state'], 'closed')

    def test_close_and_authorize_barrier_both_transaction_orders(self):
        boundary = self.hold()
        for recovered, close_first in ((False, True), (False, False), (True, True), (True, False)):
            with self.subTest(recovered=recovered, close_first=close_first):
                self.enable(self.p.dot_journal.cutover.status())
                suffix = str(recovered) + '-' + str(close_first)
                prepared = self.new_source('race-' + suffix)
                retired = None
                if recovered:
                    retired = prepared
                    self.now = retired['lease_until'] + 1
                    prepared = self.prepare()
                    self.assertTrue(prepared['preparation_recovered'])
                    self.assertEqual(prepared['journal_id'], retired['journal_id'])
                    self.assertNotEqual(prepared['attempt_id'], retired['attempt_id'])
                    self.assertFalse(prepared['authorization_ever_issued'])
                    self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_journal WHERE id=?',
                                                      (prepared['journal_id'],)).fetchone()[0], 1)
                other = self.open_probe(verifier=False)
                if retired:
                    with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
                        self.authorize(retired, peer=other)
                    with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
                        self.commit(retired, peer=other)
                barrier = threading.Barrier(2)
                first_has_write_lock = threading.Event()
                release_first = threading.Event()
                second_attempting = threading.Event()
                first_db = self.p.db if close_first else other.db
                second_db = other.db if close_first else self.p.db
                def first_trace(sql):
                    if sql.startswith('SELECT * FROM weixin_dot_cutover WHERE singleton=1'):
                        first_has_write_lock.set()
                        if not release_first.wait(5):
                            raise AssertionError('timed out waiting for second contender')
                def second_trace(sql):
                    if sql == 'BEGIN IMMEDIATE':
                        second_attempting.set()
                first_db.set_trace_callback(first_trace)
                second_db.set_trace_callback(second_trace)
                def close():
                    barrier.wait()
                    if not close_first:
                        self.assertTrue(first_has_write_lock.wait(5))
                    return self.p.dot_journal.cutover.close(boundary['boundary_id'], 'race-close-' + suffix)
                def authorize():
                    barrier.wait()
                    if close_first:
                        self.assertTrue(first_has_write_lock.wait(5))
                    try:
                        return self.authorize(prepared, peer=other)
                    except WeixinError as error:
                        return error.kind
                try:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        c = pool.submit(close)
                        a = pool.submit(authorize)
                        self.assertTrue(second_attempting.wait(5))
                        release_first.set()
                        closed, granted = c.result(timeout=10), a.result(timeout=10)
                finally:
                    release_first.set()
                    first_db.set_trace_callback(None)
                    second_db.set_trace_callback(None)
                self.assertEqual(closed['state'], 'closed')
                if close_first:
                    self.assertEqual(granted, 'dot_journal_fence_or_source_admission_pending')
                    self.assertIsNone(self.row(prepared)['authorization_id'])
                else:
                    self.assertTrue(granted['send_authorized'])
                    self.assertEqual(self.row(prepared)['authorization_id'], granted['authorization_id'])
                    # Release pending-authorized enable guard without replaying.
                    other.dot_journal.mark_unknown(prepared['journal_id'], prepared['attempt_id'])
                if retired:
                    with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
                        self.authorize(retired, peer=other)
                    with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
                        self.commit(retired, peer=other)

    def test_crash_during_close_rolls_back_and_after_commit_is_durable(self):
        self.enable()
        before = self.p.dot_journal.cutover.status()
        script = '''import os,sys
from probe.probe import Probe
p=Probe(sys.argv[1],clock=lambda:1800000000.)
s=p.dot_journal.cutover
boundary=s.status()['boundary_id']
if sys.argv[2]=='during':
    s._metadata=lambda row: os._exit(71)
s.close(boundary,'crash-close')
os._exit(72)
'''
        during = subprocess.run([sys.executable, '-B', '-c', script, self.tmp.name, 'during'], capture_output=True, text=True)
        self.assertEqual(during.returncode, 71, during.stderr)
        self.assertEqual(self.p.dot_journal.cutover.status(), before)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_cutover_closures').fetchone()[0], 0)
        after = subprocess.run([sys.executable, '-B', '-c', script, self.tmp.name, 'after'], capture_output=True, text=True)
        self.assertEqual(after.returncode, 72, after.stderr)
        restarted = self.open_probe(verifier=False)
        self.assertEqual(restarted.dot_journal.cutover.status()['state'], 'closed')
        repeated = self.close_admission('crash-close', peer=restarted)
        self.assertTrue(repeated['close_request_replayed'])
        self.assertEqual(repeated['admission_revision'], before['admission_revision'] + 1)

    def _replace_with_v1_cutover_schema(self):
        # Rebuild only a temporary fixture's cutover schema, preserving its
        # existing source classifications and journal rows. This is not a
        # production migration or a proposed rollback procedure.
        with self.p.db:
            self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_single_trial').fetchone()[0], 0)
            # A genuine v1 cutover database has neither trial nor steady
            # tables and cross-table triggers. Remove these empty synthetic
            # additions before replacing the cutover table; otherwise SQLite
            # cannot rename it while the newer ingress trigger refers to it.
            self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_steady_generations').fetchone()[0], 0)
            for trigger in ('weixin_dot_steady_future_ingress',
                            'weixin_dot_steady_source_no_update',
                            'weixin_dot_steady_source_no_delete',
                            'weixin_dot_steady_generation_no_delete',
                            'weixin_dot_steady_generation_guard'):
                self.p.db.execute('DROP TRIGGER ' + trigger)
            self.p.db.execute('DROP TABLE weixin_dot_steady_pauses')
            self.p.db.execute('DROP TABLE weixin_dot_steady_sources')
            self.p.db.execute('DROP TABLE weixin_dot_steady_generations')
            for trigger in ('weixin_dot_trial_future_ingress', 'weixin_dot_trial_no_delete',
                            'weixin_dot_trial_immutable', 'weixin_dot_trial_bulk_enable_guard',
                            'weixin_dot_trial_authorize_guard', 'weixin_dot_trial_authorized_insert_guard'):
                self.p.db.execute('DROP TRIGGER ' + trigger)
            self.p.db.execute('DROP TABLE weixin_dot_trial_future_sources')
            self.p.db.execute('DROP TABLE weixin_dot_single_trial')
            self.p.db.execute('DROP TRIGGER weixin_dot_cutover_ingress')
            self.p.db.execute('''CREATE TABLE weixin_dot_cutover_v1 (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL UNIQUE,
                account TEXT NOT NULL, owner TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('closed','new_only')),
                target_scope TEXT, snapshot_count INTEGER NOT NULL, snapshot_hash TEXT NOT NULL,
                created_at REAL NOT NULL, enabled_at REAL,
                CHECK(state='closed' OR target_scope IS NOT NULL))''')
            self.p.db.execute('''INSERT INTO weixin_dot_cutover_v1
                SELECT singleton,id,account,owner,request_id,state,target_scope,snapshot_count,
                       snapshot_hash,created_at,enabled_at FROM weixin_dot_cutover''')
            self.p.db.execute('DROP TABLE weixin_dot_cutover')
            self.p.db.execute('ALTER TABLE weixin_dot_cutover_v1 RENAME TO weixin_dot_cutover')
            self.p.db.execute('DROP TABLE weixin_dot_cutover_closures')

    def test_v1_migration_and_repeated_upgrade_preserve_receipts_and_classifications(self):
        boundary = self.hold()
        prepared = self.new_source('prior-committed')
        self.enable(boundary)
        self.authorize(prepared)
        self.commit(prepared)
        unknown = self.new_source('prior-unknown')
        self.p.dot_journal.mark_unknown(unknown['journal_id'], unknown['attempt_id'])
        rows = [tuple(r) for r in self.p.db.execute('SELECT * FROM weixin_dot_journal ORDER BY id')]
        classifications = [tuple(r) for r in self.p.db.execute('SELECT * FROM weixin_dot_cutover_sources ORDER BY message_id')]
        self._replace_with_v1_cutover_schema()
        migrated = DotCutoverStore(self.p)
        status = migrated.status()
        self.assertEqual(status['boundary_id'], boundary['boundary_id'])
        self.assertEqual(status['state'], 'new_only')
        self.assertEqual(status['admission_revision'], 0)
        self.assertEqual(rows, [tuple(r) for r in self.p.db.execute('SELECT * FROM weixin_dot_journal ORDER BY id')])
        self.assertEqual(classifications, [tuple(r) for r in self.p.db.execute('SELECT * FROM weixin_dot_cutover_sources ORDER BY message_id')])
        closed = migrated.close(boundary['boundary_id'], 'migrated-close')
        twice = DotCutoverStore(self.p)
        self.assertEqual(twice.status()['admission_revision'], closed['admission_revision'])
        self.assertEqual(twice.status()['state'], 'closed')
        self.assertTrue(twice.close(boundary['boundary_id'], 'migrated-close')['close_request_replayed'])
        late_id = self.ingest_on(self.p, 'post-upgrade')
        self.assertEqual(twice.classify(late_id)['classification'], 'reserved_new')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'enable_revision_required'), self.p.db:
            self.p.db.execute("UPDATE weixin_dot_cutover SET state='new_only',target_scope=?,enabled_at=? WHERE singleton=1 AND state='closed'", (self.scope, self.now))

    def test_interrupted_column_migration_rolls_back_and_retry_finishes(self):
        boundary = self.hold()
        self._replace_with_v1_cutover_schema()
        connection = self.p.db
        class InterruptedConnection:
            def __getattr__(self, name):
                return getattr(connection, name)
            def __enter__(self):
                connection.__enter__()
                return self
            def __exit__(self, *args):
                return connection.__exit__(*args)
            def execute(self, statement, *args):
                if statement.startswith('ALTER TABLE weixin_dot_cutover ADD COLUMN closed_at'):
                    raise RuntimeError('synthetic migration interruption')
                return connection.execute(statement, *args)
        fake = SimpleNamespace(db=InterruptedConnection(), lock=self.p.lock, clock=lambda: self.now)
        with self.assertRaisesRegex(RuntimeError, 'synthetic migration interruption'):
            DotCutoverStore(fake)
        names = {r['name'] for r in connection.execute('PRAGMA table_info(weixin_dot_cutover)')}
        self.assertNotIn('admission_revision', names)
        self.assertNotIn('closed_at', names)
        repaired = DotCutoverStore(self.p)
        self.assertEqual(repaired.status()['boundary_id'], boundary['boundary_id'])
        self.assertEqual(repaired.status()['state'], 'closed')
        self.assertEqual(repaired.close(boundary['boundary_id'], 'after-interruption')['admission_revision'], 1)

    def test_enable_revision_rejects_boolean_float_negative_and_string(self):
        boundary = self.hold()
        for value in (True, False, 0.0, -1, '0', None):
            with self.subTest(value=value), self.assertRaisesRegex(WeixinError, 'invalid_dot_cutover_arguments'):
                self.enable(boundary, expected_revision=value)
        self.assertEqual(self.p.dot_journal.cutover.status()['admission_revision'], 0)
        self.assertEqual(self.p.dot_journal.cutover.status()['state'], 'closed')

    def test_attachments_independent_and_unknown_does_not_become_eligible_again(self):
        boundary = self.hold()
        text = self.new_source()
        aid = 'att_' + 'd' * 32
        self.attachment(aid)
        attachment = self.prepare(attachment_id=aid)
        self.enable(boundary)
        self.authorize(text)
        self.commit(text)
        self.p.dot_journal.mark_unknown(attachment['journal_id'], attachment['attempt_id'])
        self.assertFalse(self.authorize(attachment)['send_authorized'])
        self.assertEqual(self.row(attachment)['state'], 'unknown')
        self.assertEqual(self.row(text)['state'], 'committed')
        self.assertEqual(len(self.p.dot_journal.for_message(self.mid)['records']), 2)

    def test_verified_epoch_cannot_be_silently_relabelled_as_local_cutover(self):
        staged = self.p.dot_journal.epochs.stage(self.scope, 'verified-route-pending')
        self.assertEqual(staged['state'], 'staged')
        with self.assertRaisesRegex(WeixinError, 'epoch_conflict'):
            self.hold()
        self.assertEqual(self.p.dot_journal.cutover.status()['state'], 'not_staged')
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_cutover_sources').fetchone()[0], 0)

    def test_hold_waits_for_existing_authorized_intents_then_only_admits_new(self):
        staged = self.p.dot_journal.epochs.stage(self.scope, 'verified-before-local')
        self.p.dot_journal.epochs.activate(staged['epoch_id'], 'synthetic-proof')
        prepared = self.new_source('before-local-boundary')
        self.authorize(prepared)
        self.p.dot_journal.epochs.suspend_for_restart()
        with self.assertRaisesRegex(WeixinError, 'authorized_intent_pending'):
            self.hold()
        self.assertEqual(self.p.dot_journal.cutover.status()['state'], 'not_staged')
        self.p.dot_journal.recover_inflight()
        boundary = self.hold()
        self.enable(boundary)
        self.assertEqual(self.row(prepared)['state'], 'unknown')
        self.assertFalse(self.authorize(prepared)['send_authorized'])
        self.assertEqual(self.p.dot_journal.cutover.classify(prepared['message_id'])['classification'], 'legacy')


if __name__ == '__main__':
    unittest.main()
