#!/bin/sh
# Local dependency setup only. No credentials, network policy or services are changed.
set -eu
cd "$(dirname "$0")/.."
PYTHON_BIN=${PYTHON_BIN:-python3}
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
if [ -e .venv ] || [ -L .venv ]; then
  echo 'Existing .venv detected; bootstrap only initializes a clean source directory.' >&2
  exit 1
fi
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m unittest discover -s probe_tests
.venv/bin/python scripts/generate-schema.py --check
