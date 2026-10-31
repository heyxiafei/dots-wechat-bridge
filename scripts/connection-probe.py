#!/usr/bin/env python3
"""Plan or run a temporary state-free MCP stdio connection probe."""
import argparse
from pathlib import Path
import sys

# Do not write __pycache__ in the installation or inspect any runtime directory.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probe.connection_probe import canonical, plan, serve


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # This command has no secret arguments; do not echo accidental values.
        self.exit(2, 'Invalid read-only diagnostic arguments; use --help.\n')


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument('--stdio', action='store_true')
    parser.add_argument('--duration-seconds', type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 180:
        parser.error('duration must be between 1 and 180 seconds')
    if not args.stdio:
        print(canonical(plan()))
        return 0
    try:
        reason = serve(args.duration_seconds)
    except (ValueError, OSError):
        print('Read-only MCP diagnostic could not start; no runtime changes were made.', file=sys.stderr)
        return 1
    return 0 if reason in ('eof', 'deadline', 'stdio_closed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
