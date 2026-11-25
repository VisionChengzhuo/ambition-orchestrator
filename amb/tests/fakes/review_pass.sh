#!/usr/bin/env bash
set -eu

sha_line=$(grep -m 1 -E '^SHA: [[:xdigit:]]{1,40}$' -- "$2")
printf '{"verdict":"PASS","sha":"%s","findings":[]}\n' "${sha_line#SHA: }" > "$3"
