"""Deterministic owner routing on an injected, existing Weixin transport.

The channel is trusted host code: read() must normalize the bridge's actual
owner proof and capture, never accept caller-asserted chat JSON. send() must use
the existing final slot and permissions. This module starts no transport.
"""
import hashlib
import json
import math
import re
import time
from collections.abc import Mapping

from .contracts import AttachmentRef, BackendCapabilities, BackendEvent, BackendRequest, BackendResult
from .routing import management_command, valid_name


_FAILURES = {
    'backend_unavailable': '当前后端未接通，本次没有调用模型。',
    'backend_lane_busy': '该会话的上次任务尚未结束，本次没有再次调用。',
    'unsupported_media': '当前后端不支持这类附件，本次没有发送给模型。',
    'quoted_context_unsupported': '此入口暂不处理引用内容；请用独立正文说明需求。',
    'request_deadline_elapsed': '任务等待已到期，结果未知；不会自动重新执行。',
    'backend_exception': '后端返回状态未知；本次不会自动重试或切换供应商。',
    'backend_result_invalid': '后端结果未通过关联检查，本次结果未知。',
    'router_interrupted': '上次任务被中断，结果未知；不会自动重新执行。',
}


def _final_open(event):
    state = event.get('reply_state')
    if not isinstance(state, dict) or state.get('next_phase') == 'none':
        return False
    final = state.get('final')
    return isinstance(final, dict) and final.get('committed') is False and final.get('status') == 'not_committed'


class BridgeCore:
    def __init__(self, store, backends, channel, owner_id, *, clock=time.time, timeout_seconds=60):
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError('owner_binding_required')
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
            raise ValueError('invalid_timeout')
        self.store, self.backends, self.channel = store, dict(backends), channel
        for key, backend in self.backends.items():
            if (not valid_name(key) or getattr(backend, 'backend_id', None) != key
                    or not isinstance(getattr(backend, 'capabilities', None), BackendCapabilities)
                    or not all(type(value) is bool for value in vars(backend.capabilities).values())
                    or not callable(getattr(backend, 'submit', None))):
                raise ValueError('invalid_backend_registration')
        self.owner_id, self.clock, self.timeout_seconds = owner_id, clock, timeout_seconds

    def set_default(self, backend_id, model=None):
        if backend_id not in self.backends:
            raise ValueError('backend_not_registered')
        return self.store.set_default(backend_id, model)

    def new_session(self, conversation_id):
        return self.store.new_session(conversation_id)

    def status(self):
        result = self.store.summary()
        result['backends'] = {key: {'text': backend.capabilities.text,
                                  'cancellation': backend.capabilities.cancellation,
                                  'task_query': backend.capabilities.task_query}
                              for key, backend in self.backends.items()}
        return result

    def _read(self, message_id):
        event = self.channel.read(message_id)
        if (not isinstance(event, dict) or event.get('message_id') != message_id
                or event.get('source') != 'owner_weixin_dm'
                or event.get('owner_verified') is not True or event.get('owner_id') != self.owner_id
                or event.get('complete') is not True or not isinstance(event.get('text'), str)
                or not isinstance(event.get('conversation_id'), str) or not event['conversation_id']
                or len(event['conversation_id']) > 256
                or not isinstance(event.get('quotes', []), (list, tuple))
                or not isinstance(event.get('attachments', []), (list, tuple))):
            raise ValueError('source_unverified')
        if len(event['text'].encode('utf-8')) > 65536:
            raise ValueError('source_unverified')
        return event

    @staticmethod
    def _digest(event):
        attachments = event.get('attachments', [])
        # Only normalized immutable references enter this boundary.
        if any(not isinstance(a, AttachmentRef) or not isinstance(a.attachment_id, str)
               or not a.attachment_id or len(a.attachment_id) > 256
               or not isinstance(a.media_type, str) or '/' not in a.media_type or len(a.media_type) > 128
               or type(a.size_bytes) is not int or not 0 <= a.size_bytes <= 20 * 1024 * 1024
               or not isinstance(a.sha256, str) or re.fullmatch('[a-f0-9]{64}', a.sha256) is None for a in attachments):
            raise ValueError('invalid_attachment_reference')
        data = [event['message_id'], event['owner_id'], event['source'], event['conversation_id'], event['text'],
                len(event.get('quotes', [])), [(a.attachment_id, a.media_type, a.size_bytes, a.sha256) for a in attachments]]
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()

    @staticmethod
    def _view(job, duplicate=False):
        return {'request_id': job['request_id'], 'backend_id': job['backend_id'],
                'backend_status': job['backend_status'], 'send_status': job['send_status'],
                'error_code': job['error_code'], 'duplicate': duplicate}

    def handle(self, message_id):
        """One exact owner event; never poll, log in or synthesize a new event ID."""
        if not isinstance(message_id, str) or not message_id or len(message_id) > 256:
            raise ValueError('invalid_message_id')
        event = self._read(message_id)
        digest = self._digest(event)
        old = self.store.task(message_id)
        if old:
            return self._view(old, True)
        if not _final_open(event):
            return {'request_id': message_id, 'backend_status': 'not_called', 'send_status': 'external_committed',
                    'error_code': 'original_final_not_open', 'duplicate': False}
        command = management_command(event['text']) if not event.get('quotes') and not event.get('attachments') else None
        job, fresh = self.store.claim(message_id, event['conversation_id'], digest, self.clock() + self.timeout_seconds,
                                      management=command is not None)
        if not fresh:
            return self._view(job, True)
        if job['backend_status'] == 'failed':
            return self._send(job)
        if event.get('quotes'):
            result = BackendResult(message_id, 'unsupported', error_code='quoted_context_unsupported')
        else:
            if command is not None:
                result = self._management(job, command)
            else:
                result = self._submit(job, event)
        job, _ = self.store.finish(message_id, result)
        return self._send(job) if result.status != 'pending' else self._view(job)

    def _management(self, job, command):
        action, argument = command
        if action == 'status':
            route = self.store.route()
            text = '当前后端：' + route['backend_id'] + '\n模型：' + (route['model'] or '后端默认') + '\n配置版本：' + str(route['revision'])
        elif action == 'backend':
            if argument not in self.backends:
                return BackendResult(job['request_id'], 'failed', error_code='backend_not_registered')
            route = self.set_default(argument)
            text = '后续新任务后端：' + route['backend_id'] + '\n配置版本：' + str(route['revision'])
        elif action == 'model':
            route = self.store.route()
            route = self.store.set_default(route['backend_id'], argument)
            text = '后续新任务模型：' + route['model'] + '\n配置版本：' + str(route['revision'])
        elif action == 'new_session':
            backend = self.backends.get(job['backend_id'])
            if backend is None or not backend.capabilities.sessions:
                return BackendResult(job['request_id'], 'unsupported', error_code='sessions_unsupported')
            generation = self.new_session(job['conversation_id'])
            text = '已重置后续任务的会话映射；后端新会话将在下次请求建立。会话版本：' + str(generation)
        else:
            target = self.store.task(argument)
            if target is None or target['conversation_id'] != job['conversation_id']:
                return BackendResult(job['request_id'], 'failed', error_code='task_not_found')
            backend = self.backends.get(target['backend_id'])
            if backend is None or not backend.capabilities.cancellation:
                return BackendResult(job['request_id'], 'unsupported', error_code='cancellation_unsupported')
            # An injected backend may cancel only its own already-admitted task.
            if target['backend_status'] != 'pending' or not target['backend_task_id']:
                return BackendResult(job['request_id'], 'unsupported', error_code='task_not_cancellable')
            if not self.store.claim_cancel(target['request_id']):
                return BackendResult(job['request_id'], 'unknown', error_code='cancellation_unknown')
            try:
                cancellation = backend.cancel(target['backend_task_id'])
            except Exception:
                self.store.mark_cancel(target['request_id'], 'unknown')
                return BackendResult(job['request_id'], 'unknown', error_code='cancellation_unknown')
            if (not isinstance(cancellation, BackendResult) or cancellation.status != 'cancelled'
                    or cancellation.request_id not in (target['request_id'], target['backend_task_id'])
                    or cancellation.task_id not in (None, target['backend_task_id'])):
                self.store.mark_cancel(target['request_id'], 'unknown')
                return BackendResult(job['request_id'], 'unknown', error_code='cancellation_unconfirmed')
            self.store.finish(target['request_id'], BackendResult(target['request_id'], 'cancelled'))
            self.store.mark_cancel(target['request_id'], 'confirmed')
            text = '后端已确认取消该任务。'
        return BackendResult(job['request_id'], 'succeeded', text=text)

    def _submit(self, job, event):
        backend = self.backends.get(job['backend_id'])
        if backend is None:
            return BackendResult(job['request_id'], 'failed', error_code='backend_unavailable')
        if self.clock() >= job['deadline_at']:
            return BackendResult(job['request_id'], 'unknown', error_code='request_deadline_elapsed')
        for attachment in event.get('attachments', []):
            kind = attachment.media_type.split('/', 1)[0]
            flag = {'image': 'images', 'audio': 'audio', 'video': 'video'}.get(kind, 'files')
            if not getattr(backend.capabilities, flag, False):
                return BackendResult(job['request_id'], 'unsupported', error_code='unsupported_media')
        if not backend.capabilities.text and event['text']:
            return BackendResult(job['request_id'], 'unsupported', error_code='text_unsupported')
        request = BackendRequest(job['request_id'], job['message_id'], job['conversation_id'], event['text'],
            backend_session=self.store.backend_session(job['session_key']), model=job['model'],
            config_revision=job['config_revision'], deadline_at=job['deadline_at'],
            attachments=tuple(event.get('attachments', ())))
        try:
            result = backend.submit(request)
        except Exception:
            return BackendResult(job['request_id'], 'unknown', error_code='backend_exception')
        if not self._result_valid(result, job['request_id']):
            return BackendResult(job['request_id'], 'unknown', error_code='backend_result_invalid')
        if self.clock() >= job['deadline_at']:
            return BackendResult(job['request_id'], 'unknown', task_id=result.task_id, error_code='request_deadline_elapsed')
        return result

    @staticmethod
    def _result_valid(result, request_id):
        return (isinstance(result, BackendResult) and result.request_id == request_id
                and (result.error_code is None or isinstance(result.error_code, str)
                     and re.fullmatch('[a-z][a-z0-9_]{0,63}', result.error_code) is not None)
                and (result.backend_session is None or isinstance(result.backend_session, str)
                     and 0 < len(result.backend_session) <= 256)
                and (result.task_id is None and result.status != 'pending' or isinstance(result.task_id, str)
                     and 0 < len(result.task_id) <= 256)
                and (result.text is None or len(result.text.encode('utf-8')) <= 65536)
                and isinstance(result.usage, Mapping)
                and all(isinstance(k, str) and type(v) is int and v >= 0 for k, v in result.usage.items()))

    def _send(self, job):
        if job['send_status'] != 'not_started':
            return self._view(job)
        try:
            event = self._read(job['message_id'])
            if self._digest(event) != job['source_digest']:
                raise ValueError('source_changed')
        except Exception:
            self.store.mark_send(job['request_id'], 'source_unverified')
            return self._view(self.store.task(job['request_id']))
        if event.get('reply_requested', True) is False:
            self.store.mark_send(job['request_id'], 'not_requested')
        elif not _final_open(event):
            self.store.mark_send(job['request_id'], 'external_committed')
        elif self.store.claim_send(job['request_id']):
            text = job['result_text'] if job['backend_status'] == 'succeeded' else _FAILURES.get(job['error_code'], '本次任务未完成，原因：' + (job['error_code'] or job['backend_status']))
            try:
                receipt = self.channel.send(job['message_id'], text)
                status = receipt.get('status') if isinstance(receipt, dict) else None
                # Unknown/unrecognized outcome cannot become a fresh send.
                self.store.mark_send(job['request_id'], status if status in ('accepted', 'queued', 'rejected') else 'unknown')
            except Exception:
                self.store.mark_send(job['request_id'], 'unknown')
        return self._view(self.store.task(job['request_id']))

    def complete(self, event, *, backend_id):
        """Trusted host callback only, not a public or Weixin-text endpoint."""
        if (not isinstance(event, BackendEvent) or event.version != 1 or event.kind != 'result'
                or not self._result_valid(event.result, event.request_id)
                or event.result.task_id not in (None, event.task_id)
                or not isinstance(event.event_id, str) or not event.event_id or len(event.event_id) > 256
                or not isinstance(event.task_id, str) or not event.task_id or len(event.task_id) > 256):
            raise ValueError('invalid_callback')
        job = self.store.task(event.request_id)
        if job is None:
            raise ValueError('unknown_request')
        if self.clock() >= job['deadline_at']:
            # The result may be accounted for, but cannot become a new answer.
            result = BackendResult(event.request_id, 'unknown', error_code='request_deadline_elapsed')
        else:
            result = event.result
        job, changed = self.store.finish(event.request_id, result, callback=event, backend_id=backend_id)
        return self._send(job) if changed and result.status != 'pending' else self._view(job, not changed)

    def expire(self):
        """An existing host calls this; this module installs no scheduler."""
        results = []
        for task in self.store.overdue(self.clock()):
            job, _ = self.store.finish(task['request_id'], BackendResult(task['request_id'], 'unknown', error_code='request_deadline_elapsed'))
            results.append(self._send(job))
        return results
