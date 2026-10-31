#!/usr/bin/env python3
"""Preview or run one explicitly authorized, isolated Weixin transport check."""
import argparse
import json
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probe.new_message_check import CheckError, check_configuration, execute, plan, read_receipt


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true')
    mode.add_argument('--receipt', action='store_true')
    mode.add_argument('--check-config', action='store_true', help='One authenticated getconfig read; no messages or sender')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--round-id')
    parser.add_argument('--approval-reference')
    parser.add_argument('--isolation-reference')
    parser.add_argument('--duration', type=int, default=120)
    args = parser.parse_args()
    if not args.live and not args.receipt and not args.check_config:
        emit(plan())
        return 0
    if args.state_dir is None or (not args.check_config and args.round_id is None):
        emit({'status': 'refused', 'error_kind': 'state_and_round_required'})
        return 1
    try:
        if args.check_config:
            if not hasattr(signal, 'setitimer'):
                raise CheckError('bounded_posix_runtime_required')
            def config_expired(*unused):
                raise TimeoutError('bounded_config_check_expired')
            previous = signal.signal(signal.SIGALRM, config_expired)
            signal.setitimer(signal.ITIMER_REAL, 12)
            try:
                emit(check_configuration(args.state_dir))
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)
        elif args.receipt:
            emit(read_receipt(args.state_dir, args.round_id))
        else:
            if not hasattr(signal, 'setitimer') or not 1 <= args.duration <= 180:
                raise CheckError('bounded_posix_runtime_required')
            def expired(*unused):
                raise TimeoutError('bounded_check_expired')
            previous = signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, args.duration)
            try:
                emit(execute(args.state_dir, args.round_id, args.approval_reference,
                             args.isolation_reference, args.duration, announce=emit))
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)
        return 0
    except CheckError as error:
        # CheckError strings are fixed categories produced by reviewed code.
        emit({'status': 'refused', 'error_kind': str(error)})
    except BlockingIOError:
        emit({'status': 'refused', 'error_kind': 'local_worker_already_running'})
    except Exception:
        # A prior durable sending record must be queried by its original round.
        emit({'status': 'stopped', 'error_kind': ('config_check_stopped_no_retry' if args.check_config else
                                                'diagnostic_stopped_read_original_round_receipt'),
              'retry_allowed': False})
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
