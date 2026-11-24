import fcntl
import json
import os
from pathlib import Path
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import amb
import amb_metrics
from tests.test_t2 import FAKES, JOB_ID, TERMINAL, T2Base, pid_alive


class TestRunnerWake(unittest.TestCase):
    def test_exit_wakes_tick_once_unlocked_on_local_and_ssh(self):
        start_minutes = []
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
                jid = subprocess.check_output([*cli, "new", "--once", "--kind", "doc", "--tier", "L1", "--host", host],
                                              env=env, text=True).strip()
                draft = root / "jobs" / "_draft" / jid / "job.json"
                job = json.loads(draft.read_text())
                job["user_words"] = "Wake after exit"
                job["accept"] = []
                draft.write_text(json.dumps(job))
                subprocess.run([*cli, "submit", jid], env=env, check=True)
                state = root / "jobs" / jid / "state.json"
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and json.loads(state.read_text())["state"] != "done":
                    time.sleep(0.05)
                self.assertEqual(json.loads(state.read_text())["state"], "done")
                self.assertIn("wake confirmed", (root / "jobs" / jid / "attempts/1/exec/stdout").read_text())
                with (root / "stats.jsonl").open() as stats:
                    [started] = [row for row in amb_metrics.records(stats)
                                 if row["event"] == "timing_started" and row["id"] == jid]
                start_minutes.append(amb_metrics.minutes(amb_metrics.timestamp(started["dispatched_at"]),
                                                         amb_metrics.timestamp(started["started_at"])))
                with (root / ".tick.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)  # Wait for the last heartbeat write before cleanup.
        median, maximum = statistics.median(start_minutes), max(start_minutes)
        self.assertLessEqual(median, 0.34)
        self.assertLessEqual(maximum, 2)


class TestStepTimeout(T2Base):
    def test_idle_retries_only_exec_and_keeps_progress(self):
        self.config["defaults"]["step_minutes"] = None
        self.config["defaults"]["model_idle_timeout_s"] = 0.6
        quiet = self.root / "allow_idle"
        self.set_role("exec.sol", self.script(
            "retry.sh", 'case "$2" in\n'
            '  *-retry-1/prompt.md) test -f "$1/DONE.txt"; '
            "printf '%s\\n' '{\"summary\": \"resumed\"}' > \"$3\";;\n"
            f'  *) touch "$1/DONE.txt"; while ! test -f {shlex.quote(str(quiet))}; do echo ready >&2; sleep .1; done; '
            'sleep 60;;\nesac\n'))
        self.submit(limits={"max_attempts": 3})
        _, pid = self.wait_for_pid()
        quiet.touch()
        state = self.tick_until(lambda st: st["state"] in TERMINAL, timeout=30)
        self.assertEqual((state["state"], state["attempt"], state["summary"]), ("done", 1, "resumed"))
        self.assertFalse(pid_alive(pid))
        self.assertEqual([(step["step"], step["retry"], step["outcome"]) for step in state["step_timings"]],
                         [("exec", 0, "idle_timeout"), ("exec", 1, "0"), ("accept/01-A1", 0, "0")])
        self.assertGreaterEqual(state["step_timings"][0]["process_s"], 0.6)

    def test_output_growth_prevents_model_timeout(self):
        self.config["defaults"]["model_idle_timeout_s"] = 0.8
        self.set_role("exec.sol", self.script(
            "growing.sh", "for i in 1 2 3 4 5 6 7 8; do echo progress >&2; sleep .2; done\n"
            f'exec {shlex.quote(str(FAKES / "exec_ok.sh"))} "$@"\n'))
        self.submit()
        state = self.tick_until(lambda st: st["state"] in TERMINAL, timeout=30)
        self.assertEqual(state["state"], "done")
        self.assertGreater(state["step_timings"][0]["process_s"], 0.8)
        self.assertEqual(len([step for step in state["step_timings"] if step["step"] == "exec"]), 1)

    def test_second_idle_timeout_falls_back_to_failure_policy(self):
        self.config["defaults"]["model_idle_timeout_s"] = 0.5
        self.config["ladder"]["sol"] = ["sol"]
        self.set_role("exec.sol", FAKES / "exec_hang.sh")
        self.submit()
        state = self.tick_until(lambda st: st["state"] in TERMINAL, timeout=30)
        self.assertEqual((state["state"], state["last_sig"], state["attempt"]), ("blocked", "exec:timeout", 1))
        self.assertEqual([(step["retry"], step["outcome"]) for step in state["step_timings"]],
                         [(0, "idle_timeout"), (1, "idle_timeout")])

    def test_accept_timeout_retries_current_check_once(self):
        marker = shlex.quote(str(self.root / "accept-ran"))
        self.submit(accept=[{"id": "A1", "cmd": "true"},
                            {"id": "A2", "cmd": f"test -f {marker} || (touch {marker}; sleep 60)", "timeout_s": 0.5}])
        state = self.tick_until(lambda st: st["state"] in TERMINAL, timeout=30)
        self.assertEqual((state["state"], state["attempt"]), ("done", 1))
        self.assertEqual([check["id"] for check in state["accept_results"]], ["A1", "A2"])
        self.assertEqual([(step["step"], step["outcome"]) for step in state["step_timings"][-3:]],
                         [("accept/01-A1", "0"), ("accept/02-A2", "timeout"), ("accept/02-A2", "0")])

    def test_long_execution_reports_active_and_completed_steps(self):
        self.set_role("exec.sol", FAKES / "exec_hang.sh")
        self.submit()
        self.wait_for_pid()
        path = self.jobs / JOB_ID / "state.json"
        state = self.state()
        began = datetime.now(timezone.utc) - timedelta(seconds=301)
        state["started_at"] = state["history"][-1]["ts"] = began.isoformat()
        state["run"]["began"] = began.timestamp()
        path.write_text(json.dumps(state), encoding="utf-8")
        self.tick()
        self.tick()
        self.amb("cancel", JOB_ID)
        with (self.root / "stats.jsonl").open() as stream:
            events = [entry for entry in amb_metrics.records(stream) if "execution_breakdown" in entry]
        self.assertEqual([row["event"] for row in events], ["timing_long_execution", "cancelled"])
        self.assertEqual(events[0]["execution_breakdown"]["steps"][0]["outcome"], "running")
        self.assertEqual(events[1]["execution_breakdown"]["steps"][0]["outcome"], "cancelled")
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
        report = json.loads(self.amb("speed", "--json", "--since", cutoff).stdout)
        self.assertGreater(report["stage_max_minutes"]["start_to_finish"], 5)
        self.assertEqual(report["stage_samples"]["start_to_finish"], 1)
        self.assertEqual(report["execution_long_tail"][0]["phases"][0]["phase"], "exec")
