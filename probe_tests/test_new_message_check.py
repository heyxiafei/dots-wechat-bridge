import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from probe import new_message_check as check
from probe.probe import Probe, acquire_worker_lock


class Clock:
    def __init__(self):
        self.value = 1000.0
    def __call__(self):
        return self.value
    def sleep(self, amount):
        self.value += amount


class Crash(BaseException):
    pass


class Client:
    def __init__(self, test, batches=None, response=None, failure=None):
        self.test, self.batches = test, list(batches or [])
        self.response = {} if response is None else response
        self.failure = failure
        self.polls, self.posts = 0, 0
    def poll(self, account):
        self.polls += 1
        self.test.assertEqual(account['cursor'], '')
        return self.batches.pop(0) if self.batches else {'msgs': []}
    def send(self, account, peer, text, client_id, context):
        self.posts += 1
        with sqlite3.connect(self.test.path) as db:
            row = db.execute('SELECT status,attempts,client_id FROM weixin_outbox').fetchone()
            self.test.assertEqual(row, ('sending', 1, client_id))
        self.test.assertEqual(peer, 'fixture-owner')
        self.test.assertEqual(context, 'fixture-context')
        if self.failure:
            raise self.failure
        return self.response


class MetadataOnly(dict):
    def get(self, key, default=None):
        if key == 'item_list':
            raise AssertionError('historic or foreign body inspected')
        return super().get(key, default)


class NewMessageCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.path = self.directory/'probe.sqlite'
        self.clock = Clock()
        p = Probe(self.directory)
        p.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
                                'ilink_user_id': 'fixture-owner',
                                'bot_token': 'synthetic-' + 'binding-fixture'})
        p.close()
        self.round = '1' * 32
        self.announcements = []
        self.net = patch('socket.create_connection', side_effect=AssertionError('real network forbidden'))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.addCleanup(self.temp.cleanup)

    def message(self, source='new-source', **changes):
        result = {'message_id': source, 'from_user_id': 'fixture-owner',
                  'to_user_id': 'fixture-bot', 'message_type': 1, 'message_state': 2,
                  'create_time_ms': self.clock()*1000, 'context_token': 'fixture-context',
                  'item_list': [{'type': 1, 'text_item': {'text': check.phrase(self.round)}}]}
        result.update(changes)
        return result

    def run_check(self, client, **kw):
        return check.execute(self.directory, self.round, 'fixture-user-request',
                             'fixture-old-writer-stopped', duration=3, client=client,
                             clock=self.clock, monotonic=self.clock,
                             pause=self.clock.sleep, announce=self.announcements.append, **kw)

    def counts(self):
        with sqlite3.connect(self.path) as db:
            return tuple(db.execute('SELECT count(*) FROM '+name).fetchone()[0]
                         for name in ('weixin_inbox', 'weixin_outbox', 'subscriptions', 'pings'))

    def test_accepted_once_and_restart_only_reads_same_slot(self):
        client = Client(self, [{'msgs': [self.message()], 'get_updates_buf': 'never-retained'}])
        result = self.run_check(client)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['send_id'], result['source_message_id'])
        self.assertIsNone(result['phone_received'])
        self.assertFalse(result['cloud_dot_connection_verified'])
        self.assertEqual(self.counts(), (1, 1, 0, 0))
        self.assertEqual(self.run_check(client)['status'], 'accepted')
        self.assertEqual((client.polls, client.posts), (1, 1))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT cursor FROM weixin_accounts').fetchone()[0], '')
            self.assertEqual(db.execute('SELECT status,attempts,kind FROM weixin_outbox').fetchone(), ('accepted', 1, 'reply'))

    def test_timeout_is_unknown_and_never_reposted(self):
        client = Client(self, [{'msgs': [self.message()]}], failure=TimeoutError('private-fake-value'))
        result = self.run_check(client)
        self.assertEqual(result['status'], 'delivery_unknown')
        self.assertNotIn('private-fake-value', json.dumps(result))
        self.run_check(client)
        self.assertEqual(client.posts, 1)

    def test_all_explicit_failures_terminal_including_rate_context_auth(self):
        for response in ({'ret': -2, 'errmsg': 'rate limit'}, {'ret': -2, 'errmsg': 'prepare failed'}, {'ret': -14}, {'errcode': 17}):
            outcome, _, _ = check.classify_send(response)
            self.assertEqual(outcome, 'rejected')
        client = Client(self, [{'msgs': [self.message()]}], response={'ret': -2})
        self.assertEqual(self.run_check(client)['status'], 'rejected')
        self.run_check(client)
        self.assertEqual(client.posts, 1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT status FROM weixin_outbox').fetchone()[0], 'rejected')

    def test_response_shapes_preserve_existing_acceptance_semantics(self):
        for response in ({}, {'ret': 0}, {'errcode': None}, {'ret': 0, 'errcode': 0}):
            self.assertEqual(check.classify_send(response)[0], 'accepted')
        for response in (None, [], {'ret': True}, {'ret': '0'}, {'base_resp': {}}):
            self.assertEqual(check.classify_send(response)[0], 'delivery_unknown')

    def test_multiple_matching_sources_stop_without_sending(self):
        client = Client(self, [{'msgs': [self.message('one'), self.message('two')]}])
        result = self.run_check(client)
        self.assertEqual(result['error_kind'], 'ambiguous_matching_messages')
        self.assertEqual(client.posts, 0)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_duplicate_same_source_still_one_send(self):
        message = self.message()
        client = Client(self, [{'msgs': [message, dict(message)]}])
        self.assertEqual(self.run_check(client)['status'], 'accepted')
        self.assertEqual(client.posts, 1)

    def test_old_and_foreign_body_never_inspected_no_cursor_advance(self):
        old = MetadataOnly(self.message(create_time_ms=1))
        foreign = MetadataOnly(self.message(from_user_id='other-owner'))
        client = Client(self, [{'msgs': [old, foreign], 'get_updates_buf': 'must-not-use'}])
        result = self.run_check(client)
        self.assertEqual(result['error_kind'], 'unmatched_batch_stopped_no_cursor_advance')
        self.assertEqual((client.polls, client.posts), (1, 0))
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_old_batch_can_contain_one_new_match_without_importing_old(self):
        client = Client(self, [{'msgs': [MetadataOnly(self.message('old', create_time_ms=1)), self.message()]}])
        self.assertEqual(self.run_check(client)['status'], 'accepted')
        self.assertEqual(self.counts(), (1, 1, 0, 0))

    def test_bad_time_recipient_context_and_nontext_are_rejected(self):
        account = {'id': 'fixture-bot', 'owner': 'fixture-owner'}
        variants = [self.message(create_time_ms=value) for value in (None, True, float('nan'), float('inf'), 1001000)]
        variants += [self.message(to_user_id='other-bot'), self.message(context_token=''),
                     self.message(message_id=None), self.message(message_id=True),
                     self.message(message_id=-1), self.message(quote={}),
                     self.message(message_type=True), self.message(ref_msg={'text': 'anything'}),
                     self.message(item_list=[{'type': 2, 'image_item': {}}]),
                     self.message(item_list=[{'type': 1, 'text_item': {'text': 'different'}}])]
        for message in variants:
            with self.subTest(message=list(message)):
                self.assertIsNone(check.candidate(account, message, check.phrase(self.round), 1000, 1003, 1000))

    def test_optional_recipient_and_official_text_metadata_supported(self):
        account = {'id': 'fixture-bot', 'owner': 'fixture-owner'}
        for recipient in (None, '', 'fixture-bot'):
            message = self.message(to_user_id=recipient)
            message['item_list'][0].update(create_time_ms=1000000, update_time_ms=1000000,
                                           is_completed=True, msg_id='fixture-item-id')
            self.assertIsNotNone(check.candidate(account, message, check.phrase(self.round), 1000, 1003, 1000))
        for changes in ({'is_completed': False}, {'create_time_ms': True},
                        {'update_time_ms': float('inf')}, {'msg_id': 1}, {'ref_msg': {}},
                        {'unexpected_payload': {}}):
            message = self.message()
            message['item_list'][0].update(changes)
            self.assertIsNone(check.candidate(account, message, check.phrase(self.round), 1000, 1003, 1000))
        for state in (None, 0, 2):
            self.assertIsNotNone(check.candidate(account, self.message(message_state=state), check.phrase(self.round), 1000, 1003, 1000))
        for state in (True, 1, '2', 3):
            self.assertIsNone(check.candidate(account, self.message(message_state=state), check.phrase(self.round), 1000, 1003, 1000))

    def test_config_read_strict_success_does_not_write_or_poll(self):
        before = self.path.read_bytes()
        captured = []
        class ConfigClient:
            def request(inner, base, endpoint, payload, token, timeout):
                captured.append((endpoint, payload, timeout))
                return {'ret': 0, 'typing_ticket': 'private-fake-value', 'errmsg': 'never-show'}
        result = check.check_configuration(self.directory, ConfigClient())
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(captured, [('/ilink/bot/getconfig', {'ilink_user_id': 'fixture-owner'}, 10)])
        self.assertNotIn('private-fake-value', json.dumps(result))
        self.assertNotIn('fixture-owner', json.dumps(result))
        self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as db:
            self.assertFalse(check.round_exists(db))
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_config_missing_bool_nonzero_or_conflicting_ret_is_not_success(self):
        for response, expected in (({}, 'unknown'), ({'ret': True}, 'unknown'),
                                   ({'ret': None}, 'unknown'), ({'ret': -14}, 'rejected'),
                                   ({'ret': 0, 'errcode': -1}, 'unknown')):
            class ConfigClient:
                def request(inner, *args, **kwargs):
                    return response
            result = check.check_configuration(self.directory, ConfigClient())
            self.assertEqual(result['status'], expected)
            self.assertIsNot(result['configuration_readable'], True)

    def test_config_exception_is_redacted_and_never_retried(self):
        class ConfigClient:
            calls = 0
            def request(inner, *args, **kwargs):
                inner.calls += 1
                raise TimeoutError('private-fake-value')
        client = ConfigClient()
        result = check.check_configuration(self.directory, client)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(client.calls, 1)
        self.assertNotIn('private-fake-value', json.dumps(result))

    def test_config_cli_uses_one_bounded_read_and_no_round_or_refs(self):
        script = Path(__file__).resolve().parents[1]/'scripts/check-new-message.py'
        spec = importlib.util.spec_from_file_location('new_message_cli_config', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch('sys.argv', [str(script), '--check-config', '--state-dir', str(self.directory)]), patch.object(module, 'check_configuration', return_value={'status': 'verified'}) as called, patch.object(module, 'execute', side_effect=AssertionError('live forbidden')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(), 0)
        called.assert_called_once_with(self.directory)

    def test_builtin_triggers_preserved_and_custom_trigger_refused(self):
        with sqlite3.connect(self.path) as db:
            before = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
        client = Client(self, [{'msgs': [self.message()]}])
        self.assertEqual(self.run_check(client)['status'], 'accepted')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall(), before)
        # Independently validate a new synthetic database with one extra trigger.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw).resolve()
            p = Probe(directory)
            p.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
                                    'ilink_user_id': 'fixture-owner', 'bot_token': 'synthetic-' + 'token'})
            p.db.execute('CREATE TRIGGER custom AFTER INSERT ON weixin_inbox BEGIN SELECT 1; END')
            p.db.commit()
            p.close()
            with self.assertRaisesRegex(check.CheckError, 'custom_schema_refused'):
                check.execute(directory, self.round, 'approval', 'isolation', client=client)

    def test_unconsumed_default_quote_control_row_is_allowed(self):
        p = Probe(self.directory)
        self.assertEqual(p.db.execute('SELECT count(*) FROM weixin_quote_shape_once').fetchone()[0], 1)
        p.close()
        client = Client(self, [{'msgs': [self.message()]}])
        self.assertEqual(self.run_check(client)['status'], 'accepted')

    def test_expiry_after_durable_attempt_but_before_post_is_zero_post(self):
        client = Client(self, [{'msgs': [self.message()]}])
        original = check.binding
        count = 0
        def changed(*args, **kwargs):
            nonlocal count
            count += 1
            result = original(*args, **kwargs)
            if count == 3:
                self.clock.value += 4
            return result
        with patch.object(check, 'binding', side_effect=changed):
            result = self.run_check(client)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['error_kind'], 'test_window_expired')
        self.assertEqual(client.posts, 0)
        self.assertEqual(result['attempts'], 1)
        self.assertTrue(result['attempts_are_durable_submission_markers'])
        self.run_check(client)
        self.assertEqual(client.posts, 0)

    def test_receipt_outbox_disagreement_never_reports_accepted(self):
        client = Client(self, [{'msgs': [self.message()]}])
        self.run_check(client)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE weixin_outbox SET status='delivery_unknown'")
        result = check.read_receipt(self.directory, self.round)
        self.assertFalse(result['api_accepted'])
        self.assertEqual(result['status'], 'delivery_unknown')
        self.assertEqual(result['error_kind'], 'receipt_state_mismatch')
        self.run_check(client)
        self.assertEqual(client.posts, 1)

    def test_existing_business_state_refused_without_poll_or_modification(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO weixin_outbox(message_id,account,peer,text,client_id,status,created_at) VALUES('unrelated','fixture-bot','fixture-owner','unused','existing','queued',1)")
        client = Client(self)
        with self.assertRaisesRegex(check.CheckError, 'non_diagnostic_state_present'):
            self.run_check(client)
        self.assertEqual((client.polls, client.posts), (0, 0))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT status,attempts FROM weixin_outbox').fetchone(), ('queued', 0))

    def test_existing_subscription_refused_without_reading_secret(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO subscriptions(id,url,secret,rotate_until,expires,verified_at) VALUES('old-sub','unused','synthetic-subscription',0,2000,1)")
        client = Client(self)
        with self.assertRaisesRegex(check.CheckError, 'non_diagnostic_state_present'):
            self.run_check(client)
        self.assertEqual(client.polls, 0)

    def test_binding_epoch_change_before_post_blocks(self):
        outer = self
        class RebindClient(Client):
            def poll(self, account):
                with sqlite3.connect(outer.path) as db:
                    db.execute("UPDATE weixin_binding_epoch SET epoch='changed'")
                return {'msgs': [outer.message()]}
        client = RebindClient(self)
        self.assertEqual(self.run_check(client)['error_kind'], 'binding_changed')
        self.assertEqual(client.posts, 0)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_crash_after_post_before_receipt_never_retries(self):
        client = Client(self, [{'msgs': [self.message()]}])
        with patch.object(check, 'classify_send', side_effect=Crash()):
            with self.assertRaises(Crash):
                self.run_check(client)
        self.assertEqual(check.read_receipt(self.directory, self.round)['status'], 'delivery_unknown')
        self.assertEqual(self.run_check(client)['status'], 'delivery_unknown')
        self.assertEqual(client.posts, 1)

    def test_crash_while_waiting_cannot_rearm_or_replace_round(self):
        client = Client(self)
        client.poll = lambda account: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.run_check(client)
        self.assertEqual(self.run_check(Client(self))['status'], 'stopped')
        self.round = '2' * 32
        with self.assertRaisesRegex(check.CheckError, 'different_round_already_recorded'):
            self.run_check(Client(self))

    def test_expired_round_never_posts_and_does_not_advance_cursor(self):
        client = Client(self)
        result = self.run_check(client)
        self.assertEqual(result['error_kind'], 'test_window_expired')
        self.assertEqual(client.posts, 0)
        self.assertLessEqual(client.polls, 6)

    def test_local_worker_lock_refuses(self):
        lock = acquire_worker_lock(self.directory)
        try:
            with self.assertRaises(BlockingIOError):
                self.run_check(Client(self))
        finally:
            lock.close()

    def test_bad_refs_duration_symlink_fail_before_client(self):
        client = Client(self)
        for reference in (None, '', 'bad\nref'):
            with self.assertRaises(check.CheckError):
                check.execute(self.directory, self.round, reference, 'isolation', client=client)
        link = self.directory/'link'
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(check.CheckError, 'state_symlink_refused'):
            check.execute(link, self.round, 'approval', 'isolation', client=client)
        self.assertEqual((client.polls, client.posts), (0, 0))

    def test_plan_is_default_and_never_opens_state(self):
        script = Path(__file__).resolve().parents[1]/'scripts/check-new-message.py'
        spec = importlib.util.spec_from_file_location('new_message_cli', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch('sys.argv', [str(script)]), patch.object(module, 'execute', side_effect=AssertionError('live forbidden')), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['mode'], 'plan')
        self.assertFalse(result['authorization_granted'])
        self.assertEqual(self.counts(), (0, 0, 0, 0))


if __name__ == '__main__':
    unittest.main()
