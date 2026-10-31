"""Bounded, state-free MCP transport diagnosis; no bridge or credential access.

Only stdlib is imported. In particular, importing or constructing Probe here
would create database state and defeat this entry point's purpose.
"""
import json
import io
import os
import secrets
import select
import signal
import sys
import time


# Static protocol identity shared with probe.py, without importing its runtime.
VERSION = '2026-07-28'
SERVER_NAME = 'dots-wechat-bridge'
SERVER_VERSION = '0.5.0+r7-connection-check'
MAX_FRAME_BYTES = 65536
MAX_REQUESTS = 256
MAX_DURATION_SECONDS = 180


def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def plan():
    return {'mode': 'plan', 'stdio_started': False, 'authorization_granted': False,
            'requires_subscription_digest': False, 'requires_state_directory': False,
            'requires_transport_authorization': True, 'max_duration_seconds': MAX_DURATION_SECONDS,
            'network_enabled': False, 'weixin_polling_enabled': False,
            'message_sending_enabled': False, 'subscriptions_enabled': False,
            'note': 'Only a later authorized private transport can expose this diagnostic.'}


def diagnostic(instance_id):
    return {'mode': 'read_only_mcp_connection_check', 'mcp_request_received': True,
            'instance_id': instance_id, 'instance_id_is_secret': False,
            'identity_scope': 'this_process_only_not_host_or_cross_client_fencing',
            'binding': 'not_read', 'weixin_authentication': 'not_checked',
            'weixin_connectivity': 'not_checked', 'phone_delivery': 'not_checked',
            'runtime_database_opened': False, 'credentials_read': False,
            'weixin_polling_enabled': False, 'message_sending_enabled': False,
            'subscriptions_enabled': False, 'subscription_state': 'not_read_or_changed'}


def tools():
    return [{'name': 'get_weixin_status',
             'description': 'DIAGNOSTIC MODE ONLY: prove this MCP request reached the temporary read-only process. Does not read the real Weixin binding, authentication, messages or subscription state; never means Weixin is connected.',
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
             'annotations': {'readOnlyHint': True, 'openWorldHint': False}}]


def valid_id(value):
    return (type(value) is int and -(2**53) < value < 2**53
            or isinstance(value, str) and 0 < len(value) <= 128)


def error(request_id, code=-32000):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {
        'code': code, 'message': 'Read-only MCP diagnostic: operation unavailable'}}


def rpc(request, instance_id):
    """No request, including a notification, can authorize or perform a write."""
    request_id = request.get('id') if isinstance(request, dict) else None
    safe_id = request_id if valid_id(request_id) else None
    if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
            or not isinstance(request.get('method'), str)
            or not isinstance(request.get('params', {}), dict)):
        return error(safe_id, -32600)
    if 'id' not in request:
        # Notifications never yield replies or cause side effects. This includes
        # attempted subscribe/send notifications as well as initialized.
        return None
    if not valid_id(request_id):
        return error(None, -32600)
    method, params = request['method'], dict(request.get('params', {}))
    if '_meta' in params:
        if not isinstance(params['_meta'], dict):
            return error(request_id, -32602)
        # Standard transport metadata is inert here, never authorization and
        # never included in diagnostic output or tool arguments.
        params.pop('_meta')
    server_info = {'name': SERVER_NAME, 'version': SERVER_VERSION}
    if method == 'initialize':
        result = {'protocolVersion': VERSION, 'serverInfo': server_info,
                  'capabilities': {'tools': {}}}
    elif method == 'server/discover':
        result = {'resultType': 'complete', 'supportedVersions': [VERSION],
                  'serverInfo': server_info, 'capabilities': {'tools': {}}}
    elif method == 'ping' and params == {}:
        result = {}
    elif method == 'tools/list' and params == {}:
        result = {'tools': tools()}
    elif (method == 'tools/call' and set(params) <= {'name', 'arguments'}
          and params.get('name') == 'get_weixin_status'
          and params.get('arguments', {}) == {}):
        value = diagnostic(instance_id)
        result = {'content': [{'type': 'text', 'text': canonical(value)}],
                  'structuredContent': value, 'isError': False}
    else:
        return error(request_id)
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


class DeadlineExpired(BaseException):
    pass


class BoundedOutput:
    """Write raw nonblocking bytes so shutdown cannot flush TextIO leftovers."""
    def __init__(self, stream):
        self.stream = stream
        self.fd = None
        self.was_blocking = None
        if isinstance(stream, io.StringIO):
            return  # In-memory sink used only by offline tests.
        self.fd = stream.fileno()
        self.was_blocking = os.get_blocking(self.fd)
        os.set_blocking(self.fd, False)

    def emit(self, value, deadline):
        text = canonical(value) + '\n'
        if self.fd is None:
            if time.monotonic() >= deadline:
                raise DeadlineExpired()
            self.stream.write(text)
            return
        remaining_bytes = memoryview(text.encode('utf-8'))
        while remaining_bytes:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise DeadlineExpired()
            try:
                count = os.write(self.fd, remaining_bytes)
            except BlockingIOError:
                select.select([], [self.fd], [], min(.1, remaining_time))
                continue
            if count <= 0:
                raise BrokenPipeError()
            remaining_bytes = remaining_bytes[count:]

    def close(self):
        if self.fd is not None:
            os.set_blocking(self.fd, self.was_blocking)


def serve(duration_seconds=60, stdin_fd=0, stdout=None, stderr=None):
    """Read bounded JSON-lines from stdin only; exit on EOF, deadline or limits.

    Raw nonblocking output and a timer bound backpressure without leaving a
    TextIO buffer to flush at interpreter shutdown. No process is spawned and
    no worker, file, DB, socket, subscription or approval is created here.
    """
    if type(duration_seconds) is not int or not 1 <= duration_seconds <= MAX_DURATION_SECONDS:
        raise ValueError('invalid_duration')
    if os.name != 'posix' or not hasattr(signal, 'setitimer'):
        raise ValueError('bounded_posix_stdio_required')
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError('existing_timer_refused')
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    instance_id = 'mcp-check-' + secrets.token_hex(16)
    deadline = time.monotonic() + duration_seconds
    buffer, request_count = b'', 0
    reason = 'eof'

    def expired(*_unused):
        raise DeadlineExpired()

    previous = None
    outputs = []
    try:
        output = BoundedOutput(stdout)
        outputs.append(output)
        diagnostics = BoundedOutput(stderr)
        outputs.append(diagnostics)
        # Arm only after both changed descriptor flags are tracked for cleanup.
        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(.001, deadline - time.monotonic()))
        # A safe per-process comparison value, never a host ID or credential.
        diagnostics.emit({'event': 'readonly_mcp_probe_started',
                          'instance_id': instance_id,
                          'duration_seconds': duration_seconds}, deadline)
        while time.monotonic() < deadline:
            ready, _, _ = select.select([stdin_fd], [], [], min(.1, max(0, deadline - time.monotonic())))
            if not ready:
                continue
            chunk = os.read(stdin_fd, 4096)
            if not chunk:
                reason = 'incomplete_frame' if buffer.strip() else 'eof'
                break
            buffer += chunk
            if len(buffer) > MAX_FRAME_BYTES:
                reason = 'frame_limit'
                break
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                request_count += 1
                if request_count > MAX_REQUESTS:
                    reason = 'request_limit'
                    return reason
                try:
                    request = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    response = error(None, -32700)
                else:
                    response = rpc(request, instance_id)
                if response is not None:
                    output.emit(response, deadline)
        else:
            reason = 'deadline'
    except DeadlineExpired:
        reason = 'deadline'
    except (BrokenPipeError, OSError):
        reason = 'stdio_closed'
    finally:
        if previous is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        for output in reversed(outputs):
            try:
                output.close()
            except OSError:
                pass
    return reason
