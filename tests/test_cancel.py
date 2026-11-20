"""Cancellation through the CLI and tick."""
import json

try:
    from test_t2 import FAKES, JOB_ID, descendants, pid_alive
    from test_t5 import T5Base
except ImportError:
    from tests.test_t2 import FAKES, JOB_ID, descendants, pid_alive
    from tests.test_t5 import T5Base


class TestCancel(T5Base):
    def assert_cancelled(self, jid):
        state = self.state(jid)
        self.assertEqual(state["state"], "cancelled")
        self.assertIsNone(state["run"])
        result = json.loads((self.jobs / jid / "result.json").read_text())
        self.assertEqual(result["state"], "cancelled")

    def test_cancel_running_planner_kills_group_and_keeps_other_job(self):
        self.set_role("plan.fable", FAKES / "exec_hang.sh")
        self.set_role("exec.sol", FAKES / "exec_hang.sh")
        self.submit(tier="L3")
        other = self.submit(job_id=f"{JOB_ID}-other")["id"]
        _, pid = self.wait_for_pid()
        _, other_pid = self.wait_for_pid(other)
        children = descendants(pid)
        self.assertTrue(children)
        self.amb("cancel", JOB_ID)
        self.amb("cancel", JOB_ID)
        self.tick()
        self.assert_cancelled(JOB_ID)
        self.assertFalse(any(pid_alive(p) for p in [pid, *children]))
        self.assertTrue(pid_alive(other_pid))
        self.assertEqual(self.state(other)["state"], "exec")
        self.assertEqual(len(self.history_to(self.state(), "cancelled")), 1)

    def test_cancel_parent_stops_running_child_and_cancels_queued_child(self):
        self.set_role("exec.sol", FAKES / "exec_hang.sh")
        self.submit(tier="L3")
        parent = self.tick_until(lambda s: s["state"] == "subs")
        first, second = parent["subs"]
        _, pid = self.wait_for_pid(first)
        self.assertEqual(self.state(second)["state"], "queued")
        self.amb("cancel", JOB_ID)
        self.tick()
        for jid in (JOB_ID, first, second):
            self.assert_cancelled(jid)
        self.assertFalse(pid_alive(pid))

    def test_cancel_queued_stops_dependents_from_waiting_forever(self):
        self.submit()
        other = self.submit(job_id=f"{JOB_ID}-other", depends_on=[JOB_ID])["id"]
        self.amb("cancel", "unknown", expected=3)
        self.amb("cancel", JOB_ID)
        self.tick()
        self.assert_cancelled(JOB_ID)
        self.assertEqual(self.state(other)["state"], "blocked")
        self.assertFalse(self.pid_files())

    def test_cancel_unreachable_runner_returns_error_without_claiming_success(self):
        self.set_role("exec.sol", FAKES / "exec_hang.sh")
        self.submit()
        _, pid = self.wait_for_pid()
        ssh = self.bin / "ssh"
        ssh.write_text("#!/bin/sh\nexit 255\n")
        ssh.chmod(0o755)
        self.config["hosts"]["local"]["ssh"] = "unreachable"
        self.save_config()
        self.amb("cancel", JOB_ID, expected=2)
        self.assertEqual(self.state()["state"], "exec")
        self.assertTrue(pid_alive(pid))
