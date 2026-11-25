"""Verify that per-task selection reaches the execution step and survives retries."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import amb


class TestDispatchModels(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory(dir=os.getcwd()))
        self.enterContext(patch.dict(os.environ, {"AMB_ROOT": self.directory}))
        self.cfg = amb.load_config()
        self.cfg["hosts"]["local"]["work_root"] = self.directory
        self.cfg["roles"].update({
            "exec.sol": {
                "cmd": "codex exec -m default-sol -c model_reasoning_effort=medium -c 'other=true' -",
                "model": "default-sol",
            },
            "exec.fable": {
                "cmd": "claude -p --model=fable --effort low",
                "model": "fable",
                "host_commands": {"remote": "bash <REDACTED_PATH> -p --model fable --effort=low"},
            },
        })

    def test_submit_launch_and_retry_preserve_choices(self):
        output = io.StringIO()
        with patch.object(amb, "load_config", return_value=self.cfg), redirect_stdout(output):
            self.assertEqual(amb.main(["new", "--once", "--kind", "doc", "--tier", "L1",
                                       "--model", "provider/custom", "--effort", "minimal"]), 0)
        jid = output.getvalue().strip()
        draft = amb.jobs_dir("_draft", jid, "job.json")
        job = amb.read_json(draft)
        job.update(title="Dispatch options", user_words="Choose this model and effort")
        amb.write_json(draft, job)
        with patch.object(amb, "load_config", return_value=self.cfg), \
                patch.object(amb.subprocess, "Popen"), redirect_stdout(io.StringIO()):
            self.assertEqual(amb.main(["submit", jid]), 0)
        job, state = amb.load(jid)
        self.assertEqual((job["model"], job["effort"]), ("provider/custom", "minimal"))
        with patch.object(amb, "init_doc_repo"), \
                patch.object(amb.Host, "git", return_value=SimpleNamespace(stdout="base\n")), \
                patch.object(amb, "start_step") as launch:
            amb.start_attempt(jid, job, state, self.cfg)
            self.assertEqual(launch.call_args.args[6], [
                "codex", "exec", "--model", "provider/custom",
                "-c", 'model_reasoning_effort="minimal"', "-c", "other=true", "-",
            ])
        self.assertEqual(state["attempts"]["1"]["exec_model"], "provider/custom")
        self.assertEqual(state["attempts"]["1"]["exec_effort"], "minimal")
        amb.fail(jid, job, state, self.cfg, "exec:exit")
        self.assertEqual((state["attempt"], state["exec"], state["state"]), (2, "sol", "queued"))

    def test_role_default_and_partial_override_for_host_wrapper(self):
        expected = ["bash", "<REDACTED_PATH>", "-p", "--model", "fable", "--effort=low"]
        self.assertEqual(amb.exec_argv(self.cfg, "exec.fable", {}, "remote", {}), expected)
        self.assertEqual(
            amb.exec_argv(self.cfg, "exec.fable", {}, "remote", {"model": "another"}),
            ["bash", "<REDACTED_PATH>", "--model", "another", "-p", "--effort=low"],
        )
        self.assertEqual(
            amb.exec_argv(self.cfg, "exec.fable", {}, "local", {"effort": "high"}),
            ["claude", "--effort", "high", "-p", "--model=fable"],
        )

    def test_plan_child_saves_its_own_selection(self):
        parent = amb.skeleton("J-test", "doc", "L3", "local", "", "Choose independently")
        parent.update(model="parent-only", effort="high")
        state = amb.initial_state(parent)
        plan = {"subs": [
            {"title": "Select", "brief": "Do the work", "exec": "fable",
             "model": "custom", "effort": "low", "checklist": ["Done"]},
            {"title": "Default", "brief": "Do the work", "checklist": ["Done"]},
        ]}
        self.assertIsNone(amb.check_plan(plan, parent, self.cfg))
        amb.make_subs(parent["id"], parent, state, plan)
        chosen, default = [amb.load(jid)[0] for jid in state["subs"]]
        self.assertEqual((chosen["model"], chosen["effort"]), ("custom", "low"))
        self.assertFalse(amb.exec_overrides(default))
