#!/usr/bin/env bash
set -eu

mkdir -p -- "$1/tests"
printf '%s\n' 'touched' > "$1/tests/touched.txt"
"$(dirname -- "${BASH_SOURCE[0]}")/exec_ok.sh" "$@"
