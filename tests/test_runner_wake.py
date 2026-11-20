import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

import amb


class TestRunnerWake(unittest.TestCase):
    def test_exit_wakes_tick_once_unlocked_on_local_and_ssh(self):
        for remote in (False, True):
            with self.subTest(remote=remote), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ssh = root / "ssh"
                ssh.write_text("#!/bin/sh\nfor last; do :; done\nexec sh -c \"$last\"\n", encoding="utf-8")
                ssh.chmod(0o755)
                host = "fixture-ssh" if remote else "local"
                config = {
                    "hosts": {host: {"ssh": host if remote else None, "work_root": str(root / "work")}},
                    "roles": {"exec.sol": {"cmd": shlex.join([sys.executable, "-c", '''print('{"summary": "wake confirmed"}')'''])}},
                    "runner_wake": True,
                    "heartbeat": str(root / "heartbeat"),
                }
                (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
                env = dict(os.environ, AMB_ROOT=str(root), PATH=f"{root}:{os.environ['PATH']}")
                cli = [sys.executable, amb.__file__]
                jid = subprocess.check_output([*cli, "new", "--kind", "doc", "--tier", "L1", "--host", host],
                                              env=env, text=True).strip()
                draft = root / "jobs" / "_draft" / jid / "job.json"
                job = json.loads(draft.read_text())
                job["user_words"] = "Wake after exit"
                job["accept"] = []
                draft.write_text(json.dumps(job))
                subprocess.run([*cli, "submit", jid], env=env, check=True)
                subprocess.run([*cli, "tick"], env=env, check=True)
                state = root / "jobs" / jid / "state.json"
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and json.loads(state.read_text())["state"] != "done":
                    time.sleep(0.05)
                self.assertEqual(json.loads(state.read_text())["state"], "done")
                self.assertIn("wake confirmed", (root / "jobs" / jid / "attempts/1/exec/stdout").read_text())
