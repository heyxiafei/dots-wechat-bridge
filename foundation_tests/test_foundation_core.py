"""Behavior tests use synthetic normalized channels and temporary ledgers only."""
import copy
from pathlib import Path
import tempfile
import unittest

from foundation.contracts import AttachmentRef, BackendCapabilities, BackendEvent, BackendResult
from foundation.core import BridgeCore
from foundation.store import FoundationStore


class Channel:
    def __init__(self):
        self.messages, self.sends = {}, []
        self.send_failure = False

    def add(self, message_id, text='hello', **changes):
        event = {'message_id': message_id, 'conversation_id': 'owner-chat', 'owner_id': 'synthetic-owner',
                 'owner_verified': True, 'source': 'owner_weixin_dm', 'complete': True, 'text': text,
                 'quotes': [], 'attachments': [],
                 'reply_state': {'final': {'committed': False, 'status': 'not_committed'}, 'next_phase': 'ack'}}
        event.update(changes)
        self.messages[message_id] = event

    def read(self, message_id):
        return copy.deepcopy(self.messages[message_id])

    def send(self, message_id, text):
        self.sends.append((message_id, text))
        if self.send_failure:
            raise TimeoutError('never leak this')
        self.messages[message_id]['reply_state'] = {'final': {'committed': True, 'status': 'accepted'}, 'next_phase': 'none'}
        return {'status': 'accepted'}


class Backend:
    capabilities = BackendCapabilities(sessions=True, callbacks=True)

    def __init__(self, backend_id='dot', behavior=None):
        self.backend_id, self.behavior, self.calls = backend_id, behavior, []

    def submit(self, request):
        self.calls.append(request)
        if self.behavior:
            return self.behavior(request)
        return BackendResult(request.request_id, 'succeeded', text='reply:' + request.text,
                             backend_session=self.backend_id + '-private-session')


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'tasks.sqlite3'
        self.now = 1000.0
        self.store = FoundationStore(self.path, clock=lambda: self.now)
        self.channel = Channel()
        self.dot, self.other = Backend(), Backend('other')
        self.core = BridgeCore(self.store, {'dot': self.dot, 'other': self.other}, self.channel,
                               'synthetic-owner', clock=lambda: self.now, timeout_seconds=30)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_duplicate_event_is_one_backend_and_one_original_final(self):
        self.channel.add('one')
        self.assertEqual(self.core.handle('one')['send_status'], 'accepted')
        self.assertTrue(self.core.handle('one')['duplicate'])
        self.assertEqual((len(self.dot.calls), len(self.channel.sends)), (1, 1))

    def test_owner_capture_and_source_rejected_without_call_or_send(self):
        for n, change in enumerate(({'owner_verified': False}, {'owner_id': 'someone-else'},
                                    {'complete': False}, {'source': 'backend_callback'})):
            key = 'invalid-' + str(n)
            self.channel.add(key, **change)
            with self.assertRaisesRegex(ValueError, 'source_unverified'):
                self.core.handle(key)
        self.assertEqual((len(self.dot.calls), len(self.channel.sends)), (0, 0))

    def test_quoted_management_does_not_change_default(self):
        self.channel.add('quote', '/backend other', quotes=[{'text': '/backend other'}])
        result = self.core.handle('quote')
        self.assertEqual(result['backend_status'], 'unsupported')
        self.assertEqual(self.store.route()['backend_id'], 'dot')
        self.assertEqual(len(self.dot.calls), 0)

    def test_attachment_capability_failure_is_explicit_without_model(self):
        self.channel.add('image', attachments=[AttachmentRef('picture', 'image/png', 5, '0' * 64)])
        result = self.core.handle('image')
        self.assertEqual(result['error_code'], 'unsupported_media')
        self.assertEqual(result['send_status'], 'accepted')
        self.assertEqual(len(self.dot.calls), 0)

    def test_unknown_send_never_retries_after_reopen(self):
        self.channel.add('uncertain')
        self.channel.send_failure = True
        self.assertEqual(self.core.handle('uncertain')['send_status'], 'unknown')
        self.store.close()
        self.store = FoundationStore(self.path)
        core = BridgeCore(self.store, {'dot': self.dot}, self.channel, 'synthetic-owner')
        self.assertEqual(core.handle('uncertain')['send_status'], 'unknown')
        self.assertEqual((len(self.dot.calls), len(self.channel.sends)), (1, 1))

    def test_interrupted_running_and_sending_are_unknown_not_replayed(self):
        self.channel.add('interrupted')
        event = self.channel.read('interrupted')
        self.store.claim('interrupted', 'owner-chat', self.core._digest(event), 1030)
        self.store.claim_send('interrupted')
        self.store.close()
        self.store = FoundationStore(self.path)
        core = BridgeCore(self.store, {'dot': self.dot}, self.channel, 'synthetic-owner')
        result = core.handle('interrupted')
        self.assertEqual((result['backend_status'], result['send_status']), ('unknown', 'unknown'))
        self.assertEqual((len(self.dot.calls), len(self.channel.sends)), (0, 0))

    def test_default_change_does_not_move_pending_task(self):
        self.dot.behavior = lambda r: BackendResult(r.request_id, 'pending', task_id='admitted-one')
        self.channel.add('long')
        self.assertEqual(self.core.handle('long')['backend_status'], 'pending')
        self.channel.add('status', '/status')
        self.assertEqual(self.core.handle('status')['backend_status'], 'succeeded')
        self.channel.add('switch', '/backend other')
        self.assertEqual(self.core.handle('switch')['backend_status'], 'succeeded')
        self.channel.add('next')
        self.core.handle('next')
        self.assertEqual(self.store.task('long')['backend_id'], 'dot')
        self.assertEqual(self.store.task('next')['backend_id'], 'other')
        self.assertEqual((len(self.dot.calls), len(self.other.calls)), (1, 1))

    def test_same_backend_session_and_model_are_isolated(self):
        self.channel.add('first'); self.core.handle('first')
        self.channel.add('second'); self.core.handle('second')
        self.assertEqual(self.dot.calls[1].backend_session, 'dot-private-session')
        self.core.set_default('other')
        self.channel.add('third'); self.core.handle('third')
        self.assertIsNone(self.other.calls[0].backend_session)
        self.assertEqual(self.other.calls[0].history, ())
        self.core.set_default('dot', 'different-model')
        self.channel.add('fourth'); self.core.handle('fourth')
        self.assertIsNone(self.dot.calls[-1].backend_session)
        self.assertEqual(self.dot.calls[0].model, None)
        self.assertEqual(self.dot.calls[-1].model, 'different-model')
        self.assertLess(self.dot.calls[0].config_revision, self.dot.calls[-1].config_revision)

    def test_new_session_does_not_delete_old_session(self):
        self.channel.add('old'); self.core.handle('old')
        old_key = self.store.task('old')['session_key']
        self.core.new_session('owner-chat')
        self.channel.add('new'); self.core.handle('new')
        self.assertNotEqual(old_key, self.store.task('new')['session_key'])
        self.assertEqual(self.store.backend_session(old_key), 'dot-private-session')
        self.assertIsNone(self.dot.calls[-1].backend_session)

    def test_new_session_command_is_unsupported_without_backend_capability(self):
        self.dot.capabilities = BackendCapabilities(sessions=False)
        self.channel.add('old'); self.core.handle('old')
        old_key = self.store.task('old')['session_key']
        self.channel.add('new-command', '/new')
        result = self.core.handle('new-command')
        self.assertEqual(result['backend_status'], 'unsupported')
        self.assertEqual(result['error_code'], 'sessions_unsupported')
        self.channel.add('next'); self.core.handle('next')
        self.assertEqual(old_key, self.store.task('next')['session_key'])
        self.assertNotIn('已重置', self.channel.sends[1][1])

    def test_backend_failure_does_not_fallback(self):
        self.dot.behavior = lambda r: BackendResult(r.request_id, 'failed', error_code='permission_denied')
        self.channel.add('denied')
        self.assertEqual(self.core.handle('denied')['error_code'], 'permission_denied')
        self.assertEqual(len(self.other.calls), 0)

    def test_unavailable_default_can_run_without_any_ai(self):
        core = BridgeCore(self.store, {}, self.channel, 'synthetic-owner', clock=lambda: self.now)
        self.channel.add('offline')
        self.assertEqual(core.handle('offline')['error_code'], 'backend_unavailable')
        self.assertEqual(core.status()['task_states'], {'failed': 1})

    def test_duplicate_callback_and_reopen_do_not_send_twice(self):
        self.dot.behavior = lambda r: BackendResult(r.request_id, 'pending', task_id='admitted-one')
        self.channel.add('async')
        self.core.handle('async')
        event = BackendEvent('async', 'admitted-one', 'event-one', 'result', BackendResult('async', 'succeeded', text='done'))
        self.core.complete(event, backend_id='dot')
        self.core.complete(event, backend_id='dot')
        self.assertEqual(len(self.channel.sends), 1)
        self.store.close()
        self.store = FoundationStore(self.path)
        core = BridgeCore(self.store, {'dot': self.dot}, self.channel, 'synthetic-owner', clock=lambda: self.now)
        self.assertTrue(core.complete(event, backend_id='dot')['duplicate'])
        self.assertEqual(len(self.channel.sends), 1)

    def test_callback_wrong_backend_task_or_request_rejected(self):
        self.dot.behavior = lambda r: BackendResult(r.request_id, 'pending', task_id='right-task')
        self.channel.add('async'); self.core.handle('async')
        wrong = BackendEvent('async', 'wrong-task', 'one', 'result', BackendResult('async', 'succeeded', text='wrong'))
        with self.assertRaisesRegex(ValueError, 'callback_binding_mismatch'):
            self.core.complete(wrong, backend_id='dot')
        right = BackendEvent('async', 'right-task', 'two', 'result', BackendResult('async', 'succeeded', text='wrong'))
        with self.assertRaisesRegex(ValueError, 'callback_binding_mismatch'):
            self.core.complete(right, backend_id='other')
        self.assertEqual(len(self.channel.sends), 0)

    def test_timeout_then_late_callback_is_unique_unknown(self):
        self.dot.behavior = lambda r: BackendResult(r.request_id, 'pending', task_id='task')
        self.channel.add('late'); self.core.handle('late')
        self.now = 1031
        self.core.expire()
        event = BackendEvent('late', 'task', 'late-result', 'result', BackendResult('late', 'succeeded', text='too late'))
        self.core.complete(event, backend_id='dot')
        self.assertEqual(len(self.channel.sends), 1)
        self.assertNotIn('too late', self.channel.sends[0][1])
        self.assertEqual(self.store.task('late')['backend_status'], 'unknown')

    def test_source_change_before_final_stops_send(self):
        def change(request):
            self.channel.messages['changed']['owner_verified'] = False
            return BackendResult(request.request_id, 'succeeded', text='do not send')
        self.dot.behavior = change
        self.channel.add('changed')
        self.assertEqual(self.core.handle('changed')['send_status'], 'source_unverified')
        self.assertEqual(len(self.channel.sends), 0)

    def test_existing_ack_allows_one_final_but_final_unknown_stops(self):
        self.channel.add('ack')
        self.channel.messages['ack']['reply_state']['ack'] = {'committed': True, 'status': 'accepted'}
        self.assertEqual(self.core.handle('ack')['send_status'], 'accepted')
        self.channel.add('final')
        self.channel.messages['final']['reply_state']['final'] = {'committed': False, 'status': 'unknown'}
        self.assertEqual(self.core.handle('final')['backend_status'], 'not_called')
        self.assertEqual(len(self.dot.calls), 1)

    def test_malformed_pending_result_or_error_not_leaked(self):
        self.dot.behavior = lambda r: BackendResult(r.request_id, 'pending')
        self.channel.add('bad')
        self.assertEqual(self.core.handle('bad')['error_code'], 'backend_result_invalid')
        self.assertEqual(len(self.channel.sends), 1)
        self.assertNotIn('never leak', self.channel.sends[0][1])

    def test_same_ledger_second_router_excluded_and_readonly_status_safe(self):
        self.channel.add('active')
        event = self.channel.read('active')
        self.store.claim('active', 'owner-chat', self.core._digest(event), 1030)
        with self.assertRaisesRegex(RuntimeError, 'router_already_active'):
            FoundationStore(self.path)
        self.assertEqual(FoundationStore.read_summary(self.path)['task_states'], {'running': 1})
        self.assertEqual(self.store.task('active')['backend_status'], 'running')

    def test_body_and_private_session_not_in_status(self):
        self.channel.add('private', 'private message body'); self.core.handle('private')
        status = str(self.core.status())
        self.assertNotIn('private message body', status)
        self.assertNotIn('dot-private-session', status)

    def test_registration_does_not_silently_accept_a_different_backend(self):
        with self.assertRaisesRegex(ValueError, 'invalid_backend_registration'):
            BridgeCore(self.store, {'dot': self.other}, self.channel, 'synthetic-owner')


if __name__ == '__main__':
    unittest.main()
