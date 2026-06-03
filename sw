#!/usr/bin/env bash
# StackWeft CLI entrypoint.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${PYTHONPATH:-}:$HERE"
exec python3 -m stackweft.cli "$@"
