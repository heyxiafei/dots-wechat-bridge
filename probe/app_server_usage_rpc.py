"""Bounded official App Server stdio reader, instantiated by a trusted host.

Construction does no I/O. The host selects an already trusted official executable
and supplies an independently verified owner/account binding and verifier. No
login, credential-file discovery, thread/turn, tool execution or fallback exists.
The official process can manage/refresh its own existing authentication; passing
refreshToken=False is not a promise that its auth manager never writes tokens.
"""
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from .usage_display import UsageBinding
from .official_usage_provider import OfficialReadDenied


class AppServerUsageError(RuntimeError):
    """Only a fixed local classification; never upstream account/error text."""


def _fail(code):
    raise AppServerUsageError(code) from None


class StdioAppServerSession:
    """One owned, bounded stdio session. No public listener or persistent daemon.

    The caller, not a message or configuration supplied by an untrusted source,
    must attest executable provenance before using this class. The child inherits
    the already-authorized host environment; this adapter never enumerates it.
    """
    def __init__(self, executable, *, start_permit, timeout=15, max_line_bytes=131072):
        path = Path(executable)
        if not path.is_absolute():
            _fail('app_server_executable_must_be_absolute')
        if not callable(start_permit):
            _fail('app_server_start_permit_required')
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 60):
            _fail('invalid_app_server_timeout')
        if type(max_line_bytes) is not int or not 1024 <= max_line_bytes <= 1048576:
            _fail('invalid_app_server_response_limit')
        self.executable = str(path)
        self.start_permit = start_permit
        self.timeout = timeout
        self.max_line_bytes = max_line_bytes
        self.process = None
        self.selector = None
        self.buffer = bytearray()
        self.sequence = 0
        self.total_bytes = 0
        self.account_verified = False
        self.started = False

    def __enter__(self):
        if self.started:
            _fail('app_server_session_already_started')
        # Starting the official program may load/update its own authentication.
        # The trusted host must authorize that process lifecycle, independently
        # of a quota request or a valid owner/account binding.
        if self.start_permit(self.executable) is not True:
            _fail('app_server_start_not_authorized')
        self.started = True
        self.deadline = time.monotonic() + self.timeout
        try:
            self.process = subprocess.Popen(
                [self.executable, 'app-server'], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, start_new_session=True,
            )
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.request('initialize', {'clientInfo': {
                'name': 'dot_wechat_usage_reader', 'version': '1.0.0'},
                'capabilities': {'experimentalApi': False}})
            self._send({'method': 'initialized'})
            return self
        except BaseException:
            self.close()
            raise

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            _fail('app_server_read_timeout')
        return remaining

    def _send(self, envelope):
        self._remaining()
        try:
            # Small fixed protocol messages only; no user text or credentials.
            wire = json.dumps(envelope, separators=(',', ':')).encode() + b'\n'
            self.process.stdin.write(wire)
            self.process.stdin.flush()
        except (OSError, ValueError, AttributeError):
            _fail('app_server_transport_closed')

    def _line(self):
        while True:
            self._remaining()
            end = self.buffer.find(b'\n')
            if end >= 0:
                if end > self.max_line_bytes:
                    _fail('app_server_response_too_large')
                line = bytes(self.buffer[:end])
                del self.buffer[:end + 1]
                return line
            if len(self.buffer) > self.max_line_bytes:
                _fail('app_server_response_too_large')
            if not self.selector.select(self._remaining()):
                _fail('app_server_read_timeout')
            try:
                chunk = os.read(self.process.stdout.fileno(),
                                min(8192, self.max_line_bytes + 1 - len(self.buffer)))
            except OSError:
                _fail('app_server_transport_closed')
            if not chunk:
                _fail('app_server_transport_closed')
            self.total_bytes += len(chunk)
            if self.total_bytes > self.max_line_bytes * 8:
                _fail('app_server_output_limit')
            self.buffer.extend(chunk)

    def request(self, method, params):
        allowed = {'initialize', 'account/read', 'account/rateLimits/read'}
        if method not in allowed or not isinstance(params, dict):
            _fail('app_server_method_not_allowed')
        if method == 'initialize' and (params != {
                'clientInfo': {'name': 'dot_wechat_usage_reader', 'version': '1.0.0'},
                'capabilities': {'experimentalApi': False}}
                or params['capabilities']['experimentalApi'] is not False):
            _fail('app_server_params_not_allowed')
        if (method == 'account/read' and
                (set(params) != {'refreshToken'} or params['refreshToken'] is not False)):
            _fail('app_server_params_not_allowed')
        if method == 'account/rateLimits/read' and params != {}:
            _fail('app_server_params_not_allowed')
        # One initialize, two account snapshots and one quota result. In
        # addition to read/output limits this bounds all fixed stdin messages.
        if self.sequence >= 4:
            _fail('app_server_request_limit')
        if method == 'account/rateLimits/read':
            # The host verifies the initial account/read before invoking this.
            self.account_verified = True
        self.sequence += 1
        request_id = self.sequence
        self._send({'id': request_id, 'method': method, 'params': params})
        # Ignore bounded notifications, never satisfy a server request/approval.
        for _ in range(128):
            try:
                message = json.loads(self._line(), parse_constant=lambda _: None)
            except (ValueError, UnicodeError, RecursionError):
                _fail('app_server_invalid_response')
            if not isinstance(message, dict):
                _fail('app_server_invalid_response')
            if 'method' in message:
                if 'id' in message:
                    _fail('app_server_unexpected_server_request')
                if message['method'] == 'account/updated' and self.account_verified:
                    _fail('app_server_account_update_observed')
                continue
            if type(message.get('id')) is not int or message['id'] != request_id:
                _fail('app_server_response_id_mismatch')
            if 'error' in message:
                # No inferred permission category, retry, raw message or data.
                _fail('app_server_rpc_error')
            if not isinstance(message.get('result'), dict):
                _fail('app_server_invalid_result')
            return message['result']
        _fail('app_server_notification_limit')

    def close(self):
        if self.selector is not None:
            self.selector.close()
            self.selector = None
        process, self.process = self.process, None
        if process is None:
            return
        # Own process group only. Even a startup failure must not leave a daemon.
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        # The leader exiting does not establish that its children exited.
        # Clean the owned group even after a successful leader wait.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)
        self.buffer.clear()

    def __exit__(self, *_):
        self.close()


class AppServerUsageRpc:
    """Provider-compatible callable, with an account check before each read.

    account_matches(binding, account) is supplied by the trusted host. It must
    check its existing account identity evidence, not merely type/plan or a
    user-supplied claim. account/read has no promised immutable account ID.
    No account fields are included in the returned usage payload or errors.
    """
    def __init__(self, session_factory, *, binding, account_matches, official_account_id=None):
        if (not callable(session_factory) or not callable(account_matches)
                or not isinstance(binding, UsageBinding) or binding.verified is not True
                or any(not isinstance(v, str) or not 0 < len(v) <= 256
                       for v in (binding.owner_ref, binding.account_ref))):
            _fail('app_server_host_binding_required')
        if (official_account_id is not None and
                (not isinstance(official_account_id, str) or not 0 < len(official_account_id) <= 256)):
            _fail('app_server_account_id_invalid')
        self.session_factory = session_factory
        self.binding = binding
        self.account_matches = account_matches
        self.official_account_id = official_account_id

    def __call__(self, method, params):
        if method != 'account/rateLimits/read' or not isinstance(params, dict) or params != {}:
            _fail('app_server_method_not_allowed')
        denied = None
        try:
            with self.session_factory() as session:
                try:
                    result = self._read_checked(session)
                except OfficialReadDenied as error:
                    denied = error
                    raise
        except OfficialReadDenied:
            # A real refusal classified by the trusted host keeps its stop
            # semantics; never downgrade it into a retryable read failure.
            raise
        except Exception as error:
            # A failing __exit__/process cleanup cannot erase a denial which
            # already occurred. This preserves refusal, not proof of shutdown.
            if denied is not None:
                raise denied from None
            if isinstance(error, AppServerUsageError):
                raise
            _fail('app_server_read_failed')
        if denied is not None:
            # Even an injected context manager that suppresses the exception
            # cannot turn an actual refusal into a successful/unknown result.
            raise denied from None
        return result

    def _read_checked(self, session):
        payload = session.request('account/read', {'refreshToken': False})
        account = payload.get('account') if isinstance(payload, dict) else None
        if not isinstance(account, dict) or account.get('type') != 'chatgpt':
            _fail('app_server_chatgpt_account_required')
        if self.account_matches(self.binding, dict(account)) is not True:
            _fail('app_server_account_mismatch')
        result = session.request('account/rateLimits/read', {})
        if (not isinstance(result, dict) or 'error' in result
                or result.get('isError') is True):
            _fail('app_server_invalid_result')
        if (self.official_account_id is not None
                and result.get('accountId') != self.official_account_id):
            _fail('app_server_usage_account_mismatch')
        # A current-account read is not a transaction. Detect a visible account
        # change; the host still owns independent identity evidence.
        after = session.request('account/read', {'refreshToken': False})
        current = after.get('account') if isinstance(after, dict) else None
        if (not isinstance(current, dict) or current != account
                or self.account_matches(self.binding, dict(current)) is not True):
            _fail('app_server_account_changed')
        return {key: result[key] for key in ('rateLimits', 'rateLimitsByLimitId')
                if key in result}
