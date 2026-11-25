#!/usr/bin/env bash
set -eu

printf '%s\n' 'done' >> "$1/DONE.txt"
printf '%s\n' '{"summary":"ok","unverified":[]}' > "$3"
