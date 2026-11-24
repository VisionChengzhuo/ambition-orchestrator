#!/usr/bin/env bash
set -eu

repo_line=$(grep -m 1 '^REPO: ' -- "$2")
python3 - "${repo_line#REPO: }" "$3" <<'PY'
import json
import sys

repo, out_file = sys.argv[1:]
subtasks = [
    {
        "title": title,
        "host": "local",
        "repo": repo,
        "exec": "sol",
        "accept": [{"id": "A1", "cmd": "test -f DONE.txt"}],
        "depends_on": dependencies,
    }
    for title, dependencies in [("s1", []), ("s2", [0])]
]
with open(out_file, "w", encoding="utf-8") as output:
    json.dump({"subs": subtasks}, output)
    output.write("\n")
PY
