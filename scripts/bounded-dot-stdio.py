#!/usr/bin/env python3
"""Plan, or run one explicitly authorized original-route DOT stdio check."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probe.bounded_dot_stdio import plan, run, read_receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true')
    mode.add_argument('--receipt', action='store_true')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--approval-file', type=Path)
    parser.add_argument('--round-id')
    args = parser.parse_args()
    if not args.live and not args.receipt:
        print(json.dumps(plan()))
        return 0
    if args.state_dir is None or (args.live and args.approval_file is None):
        print('bounded DOT check refused: exact state and private authorization record required', file=sys.stderr)
        return 1
    try:
        if args.receipt:
            print(json.dumps(read_receipt(args.state_dir, args.round_id)))
        else:
            run(args.state_dir, args.approval_file)
        return 0
    except Exception:
        print('bounded DOT check stopped; inspect original local receipt; no automatic retry', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
