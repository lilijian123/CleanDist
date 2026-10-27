#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
project_python="${CLEANDIST_PYTHON:-python3}"
if [[ ! -x .venv-linux/bin/python ]] || ! .venv-linux/bin/python -m pip --version >/dev/null 2>&1; then
    "$project_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10 or later is required.")'
    if ! "$project_python" -m venv .venv-linux; then
        printf '%s\n' 'Ubuntu: install python3-venv first (sudo apt install python3-venv).' >&2
        exit 1
    fi
fi
# Reconcile pins as well as missing imports, including existing 6.12 environments.
.venv-linux/bin/python -m pip install --quiet --disable-pip-version-check --only-binary=:all: -r requirements.txt
exec .venv-linux/bin/python -X utf8 main.py "$@"
