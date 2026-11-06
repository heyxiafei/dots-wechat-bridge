"""Worker loops and bounded stdio execution after CLI admission checks.

No worker starts at import. The entrypoint supplies the bridge factory and loop
targets explicitly, preserving its injection points and startup ordering.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading

try:
    from .rpc_protocol import canonical
    from .outbound import MAX_RPC_BYTES
    from .rpc_diagnostics import record_rpc_diagnostic, subscription_diagnostic
except ImportError:
    from rpc_protocol import canonical
    from outbound import MAX_RPC_BYTES
    from rpc_diagnostics import record_rpc_diagnostic, subscription_diagnostic

def acquire_worker_lock(directory):
    """One daemon per private state directory; read-only/admin CLIs stay usable."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "r+")



def run_weixin_sender(probe, stop, fallback=.5, burst=8):
    """One sender: wake after commits, arbitration before each bounded send."""
    while not stop.is_set():
        probe.sender_wakeup.clear()
        if probe.aihot_arm_wakeup.is_set():
            probe.aihot_arm_wakeup.clear()
            probe.aihot_operator_result = probe.consume_aihot_arm_request()['result']
        # Consumer tick is outside the owner RLock and all SQLite transactions.
        # It may queue one durable notification; the original sender remains
        # the only process that performs Weixin network delivery.
        if probe.aihot_notices is not None:
            probe.aihot_notices.tick()
            held, pending = probe.aihot_notices.sender_guard()
            probe.weixin.aihot_held_notification_ids = held
            probe.weixin.aihot_hold_all_notifications = pending or probe.aihot_notices.status_snapshot().get('auto_guard_blocked') is True
        else:
            # A configured policy with a failed optional hook must not leak old notifications.
            with probe.lock:
                try:
                    configured = getattr(probe, 'aihot_guard_required', False) or probe.db.execute('SELECT 1 FROM aihot_auto_resume WHERE id=1').fetchone() is not None
                except Exception:
                    configured = True
            probe.weixin.aihot_hold_all_notifications = configured
        for _ in range(burst):
            if stop.is_set():
                return
            try:
                decided = probe.receipts.materialize_due()
                if not probe.weixin.send_once() and not decided:
                    break
            except Exception:
                print('weixin send failed; diagnostic content redacted', file=sys.stderr)
                break
        if not stop.is_set():
            probe.sender_wakeup.wait(probe.receipts.next_wait(fallback))



def run_event_pump(probe, stop, fallback=.5, burst=8):
    """After-commit wakeup plus bounded queue draining; no model wake polling."""
    while not stop.is_set():
        probe.event_wakeup.clear()
        failed = False
        for _ in range(burst):
            if stop.is_set():
                return
            try:
                queued = probe.queue_weixin_messages()
                delivered = probe.deliver_once()
                if not queued and not delivered:
                    break
            except Exception:
                print('probe delivery failed; diagnostic content redacted', file=sys.stderr)
                failed = True
                break
        # Run the original queue/delivery burst before the optional publisher.
        # The shared transport remains FIFO across event names. The email
        # publisher is independently throttled, outside all owner/SQLite locks,
        # and only appends its bounded AIHOT events to the existing transport.
        email = getattr(probe, 'aihot_email', None)
        if not stop.is_set() and email is not None:
            try:
                email.tick()
            except Exception:
                print('aihot email publisher failed; diagnostic content redacted', file=sys.stderr)
        if stop.is_set():
            return
        try:
            probe.outbound.cleanup()
        except Exception:
            print('outgoing cleanup failed; diagnostic content redacted', file=sys.stderr)
        wait = fallback if failed else probe.event_next_wait(fallback)
        probe.event_wakeup.wait(wait)



def offline_rpc(probe, request):
    """Installation smoke checks cannot subscribe, bind, queue, or send."""
    method = request.get('method') if isinstance(request, dict) else None
    permitted = method in ('server/discover', 'initialize', 'ping', 'tools/list',
                           'notifications/initialized', 'notifications/cancelled')
    if method == 'tools/call':
        params = request.get('params')
        permitted = isinstance(params, dict) and params.get('name') in (
            'get_probe_status', 'get_weixin_status')
    if not permitted:
        return {'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                'error': {'code': -32000, 'message': 'offline_mode_operation_disabled'}}
    return probe.rpc(request)



def offline_main(directory, *, probe_factory, lock_factory, rpc_handler=offline_rpc):
    """Run only the stdio discovery/status surface, with no network or workers."""
    import socket
    directory = Path(directory)
    marker = directory / '.offline-test-state'
    if directory.is_symlink():
        raise ValueError('offline_state_symlink')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(directory.iterdir()):
        if marker.is_symlink() or not marker.is_file() or marker.read_text() != 'dot-wechat-offline-v1\n':
            raise ValueError('offline_requires_separate_test_state')
    else:
        with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
            output.write('dot-wechat-offline-v1\n')
    def no_network(*args, **kwargs):
        raise RuntimeError('offline_network_disabled')
    socket.create_connection = socket.getaddrinfo = no_network
    socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = no_network
    lock = lock_factory(directory)
    try:
        probe = probe_factory(directory, post=no_network, aihot_enabled=False, email_enabled=False)
        try:
            while line := sys.stdin.buffer.readline(MAX_RPC_BYTES + 1):
                if len(line) > MAX_RPC_BYTES:
                    while line and not line.endswith(b'\n'):
                        line = sys.stdin.buffer.readline(MAX_RPC_BYTES + 1)
                    response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Request too large'}}
                else:
                    try:
                        response = rpc_handler(probe, json.loads(line))
                    except (ValueError, TypeError):
                        response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON'}}
                if response is not None:
                    print(canonical(response), flush=True)
        finally:
            probe.close()
    finally:
        lock.close()
    return 0



def serve_stdio(probe, *, event_pump, sender_loop,
                record_diagnostic=record_rpc_diagnostic,
                project_diagnostic=subscription_diagnostic):
    stop = threading.Event()
    # The handler only wakes the existing sender. A one-time private request
    # must already exist; a signal alone cannot authorize or arm anything.
    signal.signal(signal.SIGUSR2, lambda *_: probe.aihot_arm_wakeup.set())
    probe.weixin.recover_inflight()
    probe.media.recover_inflight()
    probe.outbound.recover_inflight()
    probe.dot_mirrors.recover_inflight()
    probe.dot_journal.recover_inflight()
    probe.receipts.start_policy()
    probe.recover_event_deliveries()
    worker = threading.Thread(target=event_pump, args=(probe, stop), daemon=True)
    worker.start()
    sender = threading.Thread(target=sender_loop, args=(probe, stop), daemon=True)
    sender.start()
    def poll_weixin():
        while not stop.wait(0.5):
            try:
                probe.weixin.poll_once()
            except Exception:
                print("weixin poll failed; diagnostic content redacted", file=sys.stderr)
    poller = threading.Thread(target=poll_weixin, daemon=True)
    poller.start()
    def process_media():
        while not stop.wait(.5):
            try:
                probe.media.process_once()
            except Exception:
                print("media processing failed; diagnostic content redacted", file=sys.stderr)
    media_worker = threading.Thread(target=process_media, daemon=True)
    media_worker.start()
    try:
        while line := sys.stdin.buffer.readline(MAX_RPC_BYTES + 1):
            if len(line) > MAX_RPC_BYTES:
                while not line.endswith(b'\n'):
                    line = sys.stdin.buffer.readline(MAX_RPC_BYTES + 1)
                    if not line:
                        break
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request too large"}}
            else:
                try:
                    request = json.loads(line)
                    record_diagnostic(probe.directory, request)
                    diagnostic = project_diagnostic(request)
                    if diagnostic:
                        print(diagnostic, file=sys.stderr, flush=True)
                    response = probe.rpc(request)
                    record_diagnostic(probe.directory, request, response, completed=True)
                    diagnostic = project_diagnostic(request, response)
                    if diagnostic:
                        print(diagnostic, file=sys.stderr, flush=True)
                except (ValueError, UnicodeError):
                    response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            if response is not None:
                print(canonical(response), flush=True)
    finally:
        stop.set()
        probe.notify_sender()
        probe.notify_events()
        worker.join(timeout=12)
        poller.join(timeout=1)
        media_worker.join(timeout=1)
        sender.join(timeout=1)
        if not worker.is_alive() and not poller.is_alive() and not media_worker.is_alive() and not sender.is_alive():
            probe.close()
    return 0

