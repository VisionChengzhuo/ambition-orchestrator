"""Completed executors resume finalization after a host disconnect."""
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import amb


class TestExecRecovery(unittest.TestCase):
    def test_collected_exec_survives_host_down(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), TemporaryDirectory() as directory:
                local = Path(directory)
                (local / "exit").write_text("0")
                (local / "stdout").write_text('{"summary":"original result"}')
                run = {"step": "exec", "local": directory, "dir": directory, "host": "local",
                       "began": time.time(), "started": time.time()}
                st = {"state": "exec", "run": None if legacy else run, "attempt": 1,
                      "wt": str(local), "exec_output_dir": directory}
                job = {"host": "local"}
                host = Mock()
                host.poll.return_value = {"exit": "0", "alive": False}
                host.run.return_value.returncode = 0
                host.git.side_effect = [amb.HostDown("offline"), Mock(stdout="sha"), Mock(stdout="")]
                with patch.object(amb, "Host", return_value=host), patch.object(amb, "save"), \
                        patch.object(amb, "record_started"), patch.object(amb, "att_dir", return_value=directory), \
                        patch.object(amb, "collect"), patch.object(amb, "after_exec") as advance:
                    if legacy:
                        st["step_timings"] = [amb.step_timing(st, run, "0", time.time())]
                        self.assertTrue(amb.recover_completed_exec("job", job, st))
                    with self.assertRaises(amb.HostDown):
                        amb.poll_step("job", {"parent": "parent", **job}, st, {})
                    self.assertTrue(st["run"]["collected"])
                    amb.poll_step("job", job, st, {})
                    self.assertIsNone(st["run"])
                    self.assertEqual(st["summary"], "original result")
                    self.assertEqual(len(st["step_timings"]), 1)
                    self.assertEqual(host.poll.call_count, 0 if legacy else 1)
                    host.launch.assert_not_called()
                    advance.assert_called_once()

    def test_ready_child_can_run_after_unrelated_sibling_cancelled(self):
        job = {"parent": "parent", "plan_attempt": 1, "depends_on": ["required"]}
        st = {"state": "queued", "history": []}
        parent = {"state": "blocked", "attempt": 1, "plan_base_sha": "base",
                  "needs_human": "子任务 sibling cancelled", "subs": ["required", "child", "sibling"]}
        states = {"parent": parent, "required": {"state": "done"}, "sibling": {"state": "cancelled"}}
        with patch.object(amb, "jobs_dir", side_effect=lambda jid, *_: jid), \
                patch.object(amb, "read_json", side_effect=lambda jid, _: states[jid]), \
                patch.object(amb, "dispatch_start_due", return_value=None), \
                patch.object(amb, "start_attempt") as start:
            amb.start_ready_attempt("child", job, st, {})
            start.assert_called_once_with("child", job, st, {})
            job["depends_on"].append("sibling")
            amb.start_ready_attempt("child", job, st, {})
            self.assertEqual(st["state"], "blocked")
            self.assertEqual(start.call_count, 1)
