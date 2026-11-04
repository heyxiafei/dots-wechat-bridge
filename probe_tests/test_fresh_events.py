"""Fresh event gates: synthetic state, injected transport, no network/services."""
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from probe import probe as module
from probe.probe import Probe, RpcError
from probe.weixin import MESSAGE_EVENT, WeixinError


HOST = 'fresh-callback.example.test'
URL = 'https://' + HOST + '/events'
SECRET = 'whsec_' + base64.b64encode(b'f' * 32).decode()


class FixtureAdmission:
    """An injected policy to exercise Probe hooks, never production evidence."""
    def __init__(self, probe, params):
        self.probe = probe
        self.subscription_id = probe.subscription_id(params)
        self.allowed = set()
        self.ready = False
        self.valid = True
        self.before_count = 0
        self.after_count = 0

    def require_ready(self):
        if not self.valid or not self.ready:
            raise WeixinError('fresh_fixture_not_ready')

    def before_subscribe(self, params):
        self.before_count += 1
        if not self.valid or self.probe.subscription_id(params) != self.subscription_id:
            raise WeixinError('fresh_fixture_subscription_mismatch')

    def after_subscribe(self, result):
        if result['id'] != self.subscription_id or not self.valid:
            raise WeixinError('fresh_fixture_subscription_mismatch')
        self.after_count += 1
        self.ready = True

    def allows_source(self, message_id):
        return self.ready and self.valid and message_id in self.allowed


class FreshEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.requests = []
        self.verify_handler = None
        self.event_handler = lambda event: (200, b'{}')
        self.p = Probe(self.tmp.name, clock=lambda: self.now,
                       allowed_hosts=[HOST], post=self.receiver)
        self.addCleanup(self.p.close)
        self.p.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
                                     'ilink_user_id': 'fixture-owner', 'bot_token': 'fixture-token'})
        for target in ('socket.create_connection', 'socket.socket.connect', 'subprocess.Popen'):
            guard = patch(target, side_effect=AssertionError('offline test forbids external IO'))
            guard.start()
            self.addCleanup(guard.stop)

    @staticmethod
    def params(name=MESSAGE_EVENT):
        return {'name': name, 'arguments': {}, 'ttlMs': None,
                'delivery': {'mode': 'webhook', 'url': URL, 'secret': SECRET}}

    def receiver(self, url, body, headers):
        event = json.loads(body)
        self.requests.append(event)
        if event.get('type') == 'verification':
            if self.verify_handler:
                return self.verify_handler(event)
            return 200, json.dumps({'challenge': event['challenge']}).encode()
        return self.event_handler(event)

    def install_policy(self, handshake=True):
        self.policy = FixtureAdmission(self.p, self.params())
        self.p.fresh_admission = self.policy
        if handshake:
            self.p.subscribe(self.params())
        return self.policy

    def source(self, index, admitted=True):
        mid = 'wx_' + hashlib.sha256(str(index).encode()).hexdigest()[:32]
        with self.p.db:
            self.p.db.execute('''INSERT INTO weixin_inbox
                (id,account,peer,source_id,text,created_at,kind,unsupported,event_id)
                VALUES(?,?,?,?,?,?,'text','[]',NULL)''',
                (mid, 'fixture-bot', 'fixture-owner', str(index), 'synthetic source', self.now + index))
        if admitted:
            self.policy.allowed.add(mid)
        return mid

    def linked(self, index=1):
        mid = self.source(index)
        self.assertEqual(self.p.queue_weixin_messages(), 1)
        eid = self.p.db.execute('SELECT event_id FROM weixin_inbox WHERE id=?', (mid,)).fetchone()[0]
        return mid, eid

    def row(self, eid):
        return dict(self.p.db.execute('SELECT * FROM pings WHERE id=?', (eid,)).fetchone())

    def events(self):
        return [event for event in self.requests if event.get('type') != 'verification']

    def test_fresh_renewal_never_uses_cached_challenge(self):
        first = self.p.subscribe(self.params())
        self.install_policy(False)
        for _ in range(2):
            self.assertEqual(self.p.subscribe(self.params())['id'], first['id'])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual((self.policy.before_count, self.policy.after_count), (4, 2))
        self.assertTrue(self.policy.ready)

    def test_failed_challenge_never_marks_runtime_ready(self):
        self.install_policy(False)
        self.verify_handler = lambda event: (403, b'{}')
        with self.assertRaises(RpcError):
            self.p.subscribe(self.params())
        self.assertFalse(self.policy.ready)
        self.assertEqual(self.policy.after_count, 0)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 0)

    def test_changed_identity_during_challenge_cannot_commit(self):
        self.install_policy(False)
        def change(event):
            self.policy.valid = False
            return 200, json.dumps({'challenge': event['challenge']}).encode()
        self.verify_handler = change
        with self.assertRaises(WeixinError):
            self.p.subscribe(self.params())
        self.assertEqual(self.policy.after_count, 0)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 0)

    def test_other_event_subscription_and_unsubscribe_are_refused(self):
        self.install_policy()
        before = len(self.requests)
        with self.assertRaises(WeixinError):
            self.p.subscribe(self.params('test.ping'))
        with self.assertRaises(RpcError):
            self.p.unsubscribe(self.params())
        with self.assertRaises(RpcError):
            self.p.emit()
        self.assertEqual(len(self.requests), before)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 1)

    def test_only_admitted_sources_queue_without_old_rows_starving_them(self):
        self.install_policy()
        old = [self.source(index, False) for index in range(40)]
        mid = self.source(50)
        self.assertEqual(self.p.queue_weixin_messages(), 1)
        self.assertEqual(self.p.db.execute('SELECT message_id FROM pings').fetchone()[0], mid)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_inbox WHERE event_id IS NULL').fetchone()[0], len(old))

    def test_no_events_before_this_runtime_handshake(self):
        self.install_policy()
        mid, eid = self.linked()
        self.policy.ready = False
        self.source(2)
        self.assertEqual(self.p.queue_weixin_messages(), 0)
        self.assertFalse(self.p.deliver_once())
        self.assertEqual(self.row(eid)['attempts'], 0)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.p.event_next_wait(), .5)

    def test_unknown_response_is_terminal_and_never_replayed(self):
        self.install_policy()
        for index, status in enumerate((0, 408, 429, 503), 1):
            with self.subTest(status=status):
                mid, eid = self.linked(index)
                self.event_handler = lambda event, status=status: (status, b'{}')
                self.assertTrue(self.p.deliver_once())
                self.now += 60
                self.assertFalse(self.p.deliver_once())
                self.assertEqual((self.row(eid)['status'], self.row(eid)['attempts']), ('delivery_unknown', 1))
        self.assertEqual(len(self.events()), 4)

    def test_exception_is_unknown_without_retry(self):
        self.install_policy()
        _, eid = self.linked()
        def timeout(event):
            raise TimeoutError()
        self.event_handler = timeout
        self.assertTrue(self.p.deliver_once())
        self.now += 60
        self.assertFalse(self.p.deliver_once())
        self.assertEqual(self.row(eid)['status'], 'delivery_unknown')
        self.assertEqual(len(self.events()), 1)

    def test_denial_does_not_replay_or_change_original_subscription(self):
        self.install_policy()
        sub_before = dict(self.p.db.execute('SELECT * FROM subscriptions').fetchone())
        for index, status in enumerate((403, 410), 1):
            _, eid = self.linked(index)
            self.event_handler = lambda event, status=status: (status, b'{}')
            self.p.deliver_once()
            self.now += 60
            self.assertFalse(self.p.deliver_once())
            self.assertEqual((self.row(eid)['status'], self.row(eid)['attempts']), ('failed', 1))
        self.assertEqual(dict(self.p.db.execute('SELECT * FROM subscriptions').fetchone()), sub_before)

    def test_restart_preserves_attempted_event_as_unknown(self):
        self.install_policy()
        _, eid = self.linked()
        with self.p.db:
            self.p.db.execute("UPDATE pings SET attempts=1,lease_token='fixture-lease',lease_until=? WHERE id=?", (self.now + 30, eid))
        self.p.recover_event_deliveries()
        self.now += 60
        self.assertFalse(self.p.deliver_once())
        row = self.row(eid)
        self.assertEqual((row['id'], row['status'], row['attempts']), (eid, 'delivery_unknown', 1))
        self.assertIsNone(row['lease_token'])
        self.assertEqual(self.events(), [])

    def test_admission_is_rechecked_immediately_before_post(self):
        self.install_policy()
        mid, eid = self.linked()
        original = self.p.delivery_timing.attempt
        def revoke(message_id, now):
            original(message_id, now)
            self.policy.allowed.remove(message_id)
        with patch.object(self.p.delivery_timing, 'attempt', side_effect=revoke):
            self.assertTrue(self.p.deliver_once())
        self.assertEqual(self.events(), [])
        self.assertEqual(self.row(eid)['status'], 'cancelled')
        self.policy.allowed.add(mid)
        self.now += 60
        self.assertFalse(self.p.deliver_once())

    def test_changed_subscription_revision_blocks_post(self):
        self.install_policy()
        _, eid = self.linked()
        def change(message_id, now):
            self.p.db.execute("UPDATE subscriptions SET revision='different-fixture-revision'")
        with patch.object(self.p.delivery_timing, 'attempt', side_effect=change):
            self.assertTrue(self.p.deliver_once())
        self.assertEqual(self.events(), [])
        self.assertEqual(self.row(eid)['last_error_kind'], 'fresh_admission_changed_before_post')

    def test_unrelated_and_forged_association_queues_are_untouched(self):
        self.p.subscribe(self.params('test.ping'))
        unrelated = self.p.emit()['event_id']
        before = self.row(unrelated)
        self.install_policy()
        mid, eid = self.linked()
        with self.p.db:
            self.p.db.execute("INSERT INTO pings(id,nonce,sub_id,body,status,name,message_id) VALUES('forged','','fixture-other-route','{}','queued',?,?)", (MESSAGE_EVENT, mid))
        self.assertTrue(self.p.deliver_once())
        self.assertFalse(self.p.deliver_once())
        self.assertEqual(self.row(unrelated), before)
        self.assertEqual(self.row('forged')['attempts'], 0)
        self.assertEqual([event['eventId'] for event in self.events()], [eid])

    def test_corrupt_body_cannot_redirect_an_admitted_event(self):
        self.install_policy()
        _, eid = self.linked()
        with self.p.db:
            self.p.db.execute("UPDATE pings SET body=? WHERE id=?", (json.dumps(
                {'data': {'message_id': 'unadmitted-historical-source'}}), eid))
        self.assertFalse(self.p.deliver_once())
        self.assertEqual(self.events(), [])
        self.assertEqual(self.row(eid)['attempts'], 0)

    def test_ordinary_cli_cannot_start_a_restored_fresh_generation(self):
        self.install_policy(False)
        with patch.object(module, 'Probe', return_value=self.p), \
                patch.object(module.threading, 'Thread') as workers, \
                patch.object(sys, 'argv', ['probe.py', '--state-dir', self.tmp.name]), \
                patch.object(sys, 'stderr', new_callable=io.StringIO) as error:
            self.assertEqual(module.main(), 1)
        workers.assert_not_called()
        self.assertIn('rollback must stay stopped', error.getvalue())

    def test_fresh_cli_without_approval_starts_no_workers(self):
        directory = Path(self.tmp.name) / 'empty-state'
        with patch.object(module.threading, 'Thread') as workers, \
                patch.object(sys, 'argv', ['probe.py', '--fresh-production', '--state-dir', str(directory)]), \
                patch.object(sys, 'stderr', new_callable=io.StringIO) as error:
            self.assertEqual(module.main(), 1)
        workers.assert_not_called()
        self.assertIn('no workers started', error.getvalue())

    def test_fresh_cli_cannot_mix_diagnostic_or_read_only_modes(self):
        for other in ('--emit', '--offline', '--status'):
            with self.subTest(other=other), patch.object(module, 'Probe') as constructor, \
                    patch.object(sys, 'argv', ['probe.py', '--fresh-production', other]), \
                    patch.object(sys, 'stderr', new_callable=io.StringIO), \
                    self.assertRaises(SystemExit) as stopped:
                module.main()
            self.assertEqual(stopped.exception.code, 2)
            constructor.assert_not_called()


if __name__ == '__main__':
    unittest.main()
