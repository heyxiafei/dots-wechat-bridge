"""Bounded, state-free MCP transport diagnosis; no bridge or credential access.

Only stdlib is imported. In particular, importing or constructing Probe here
would create database state and defeat this entry point's purpose.
"""
import json
import io
import hashlib
import ipaddress
import os
import re
import secrets
import select
import signal
import sys
import time
from urllib.parse import urlsplit


# Static protocol identity shared with probe.py, without importing its runtime.
VERSION = '2026-07-28'
SERVER_NAME = 'dots-wechat-bridge'
SERVER_VERSION = '0.5.0+r7-connection-check'
MAX_FRAME_BYTES = 65536
MAX_JSON_DEPTH = 64
MAX_REQUESTS = 256
MAX_DURATION_SECONDS = 180


def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def parse_frame(raw):
    """Bound container depth before JSON allocation, independently of Python.

    Bytes retain json.loads' encoding detection. Only brackets outside strings
    count; the standard decoder still validates syntax and matching delimiters.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode(json.detect_encoding(raw), 'surrogatepass')
    depth, quoted, escaped = 0, False, False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError('json_depth_limit')
        elif char in ']}':
            depth = max(0, depth - 1)
    return json.loads(raw)


def plan(observe_subscriptions=False):
    return {'mode': 'plan', 'stdio_started': False, 'authorization_granted': False,
            'requires_subscription_digest': False, 'requires_state_directory': False,
            'requires_transport_authorization': True, 'max_duration_seconds': MAX_DURATION_SECONDS,
            'network_enabled': False, 'weixin_polling_enabled': False,
            'message_sending_enabled': False, 'subscriptions_enabled': False,
            'subscription_observation_enabled': observe_subscriptions is True,
            'note': 'Only a later authorized private transport can expose this diagnostic.'}


def lifetime(params):
    """Preserve exact requested TTL semantics; none of this grants a lifetime."""
    if 'ttlMs' not in params:
        return {'kind': 'omitted'}
    value = params['ttlMs']
    if value is None:
        return {'kind': 'null'}
    if type(value) is not int or not 0 < value < 2**53:
        raise ValueError('invalid_lifetime')
    return {'kind': 'finite', 'value': value}


def observe_subscription(params):
    """Project a candidate route without echoing host, URL, secret or cursor.

    This deliberately does not validate a signing secret, resolve DNS, verify
    the caller or register anything. A valid projection is not authorization.
    Route encoding must exactly match Probe.subscription_id's canonical list.
    """
    if (not isinstance(params, dict)
            or not set(params) <= {'name', 'arguments', 'delivery', 'ttlMs', 'cursor', '_meta'}
            or params.get('name') != 'weixin.message' or params.get('arguments', {}) != {}
            or ('_meta' in params and not isinstance(params['_meta'], dict))):
        raise ValueError('invalid_subscription_shape')
    delivery = params.get('delivery')
    if (not isinstance(delivery, dict) or not set(delivery) <= {'mode', 'url', 'secret'}
            or delivery.get('mode') != 'webhook'):
        raise ValueError('invalid_delivery_shape')
    url = delivery.get('url')
    if (not isinstance(url, str) or not 1 <= len(url) <= 8192
            or any(ord(char) < 33 or ord(char) == 127 for char in url)):
        raise ValueError('invalid_callback_shape')
    parts = urlsplit(url)
    host = parts.hostname
    if (parts.scheme != 'https' or not host or len(host) > 253
            or parts.username or parts.password or parts.fragment
            or parts.port not in (None, 443) or '.' not in host
            or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                       for label in host.split('.'))):
        raise ValueError('invalid_callback_shape')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('literal_address_refused')
    route = json.dumps(['owner-only-tunnel', url, 'weixin.message', {}],
                       sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    digest = hashlib.sha256(route.encode()).hexdigest()
    return {'observation_type': 'subscription_candidate', 'observed_not_authorized': True,
            'candidate_callback_host_digest': hashlib.sha256(host.encode('ascii')).hexdigest(),
            'candidate_route_digest': digest, 'candidate_subscription_id': 'sub_' + digest[:32],
            'requested_lifetime': lifetime(params), 'subscription_registered': False,
            'callback_challenge_sent': False, 'original_subscription_verified': False}


def observed_event():
    return {'name': 'weixin.message',
            'description': 'DIAGNOSTIC ONLY: subscription requests are observed and refused; no messages or events are delivered.',
            'delivery': ['webhook'],
            'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
            'payloadSchema': {'type': 'object', 'properties': {'message_id': {'type': 'string'}},
                              'required': ['message_id'], 'additionalProperties': False}}


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


def rpc(request, instance_id, observe_subscriptions=False):
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
    capabilities = {'tools': {}}
    if observe_subscriptions is True:
        capabilities['events'] = {}
    if method == 'initialize':
        result = {'protocolVersion': VERSION, 'serverInfo': server_info,
                  'capabilities': capabilities}
    elif method == 'server/discover':
        result = {'resultType': 'complete', 'supportedVersions': [VERSION],
                  'serverInfo': server_info, 'capabilities': capabilities}
    elif method == 'ping' and params == {}:
        result = {}
    elif method == 'tools/list' and params == {}:
        result = {'tools': tools()}
    elif observe_subscriptions is True and method == 'events/list' and params == {}:
        result = {'events': [observed_event()]}
    elif observe_subscriptions is True and method == 'events/subscribe':
        try:
            observation = observe_subscription(params)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return error(request_id, -32602)
        response = error(request_id)
        response['error']['data'] = observation
        return response
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


def serve(duration_seconds=60, stdin_fd=0, stdout=None, stderr=None, observe_subscriptions=False):
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
                    request = parse_frame(line)
                except (ValueError, UnicodeError, RecursionError):
                    response = error(None, -32700)
                else:
                    response = rpc(request, instance_id, observe_subscriptions)
                if response is not None:
                    output.emit(response, deadline)
                    observation = response.get('error', {}).get('data', {})
                    if observation.get('observation_type') == 'subscription_candidate':
                        diagnostics.emit({'event': 'subscription_candidate_observed',
                                          'instance_id': instance_id, **observation}, deadline)
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
