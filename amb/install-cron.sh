#!/usr/bin/env bash
# 只追加一行 amb tick 到当前用户 crontab；可重复执行；不改动其他行。
# 读取失败（除明确的 "no crontab"）立即退出非 0 且不写；写入前后校验非 amb 行不变。
set -eu
CRONTAB_BIN=${CRONTAB_BIN:-crontab}
LINE='* * * * * flock -n ~/amb/.lock python3 ~/amb/amb.py tick'
err=$(mktemp); trap 'rm -f "$err"' EXIT
others() { grep -v -e 'amb.py tick' -e '^$' || true; }
if current=$("$CRONTAB_BIN" -l 2>"$err"); then :
elif grep -qi 'no crontab' "$err"; then current=""
else echo "crontab -l 失败，未做任何修改：$(cat "$err")" >&2; exit 1
fi
if printf '%s\n' "$current" | grep -qF 'amb.py tick'; then echo "already installed"; exit 0; fi
before=$(printf '%s\n' "$current" | others)
again=$("$CRONTAB_BIN" -l 2>/dev/null || true)
[ "$(printf '%s\n' "$again" | others)" = "$before" ] || { echo "crontab 在读取期间被改动，放弃" >&2; exit 1; }
{ [ -n "$current" ] && printf '%s\n' "$current"; printf '%s\n' "$LINE"; } | "$CRONTAB_BIN" -
after=$("$CRONTAB_BIN" -l | others)
[ "$after" = "$before" ] || { echo "警告：安装后非 amb 行与原表不一致" >&2; exit 1; }
echo "installed: $LINE"
