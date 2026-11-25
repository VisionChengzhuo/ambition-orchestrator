"""关卡前核对实际目标，终态同步失败时保持可重试。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import amb


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls, self.children = [], {}

        def fake(cfg, *a):
            report = a[2] if a[0] == "progress" else a[3]
            self.calls.append((*a, Path(report).read_text()))
            return "ok"
        for p in (mock.patch.object(amb, "jobs_dir", lambda *p: os.path.join(self.tmp.name, *p)),
                  mock.patch.object(amb, "linear_app", fake),
                  mock.patch.object(amb, "lead_children", lambda jid: list(self.children))):
            p.start()
            self.addCleanup(p.stop)

    def sync(self, job, st, result="结果全文：到达率 92%。\n"):
        job = {"id": "J-T", "linear": "<REDACTED_ID>", "linear_auto": True, **job}
        os.makedirs(os.path.join(self.tmp.name, "J-T"), exist_ok=True)
        Path(self.tmp.name, "J-T", "result.md").write_text(result)
        for child, cst in self.children.items():
            os.makedirs(os.path.join(self.tmp.name, child), exist_ok=True)
            Path(self.tmp.name, child, "state.json").write_text(json.dumps(cst))
        st = {"state": "done", "summary": "这一轮做完了。", "history": [], **st}
        amb.linear_sync_state(job, st, {})
        return [c[0] for c in self.calls], st

    # 第 1 处：没有复核的单
    def test_no_review_missing_goal_met_requires_follow_up(self):
        cmds, st = self.sync({}, {"open_items": []})
        self.assertEqual(cmds, ["progress"])
        self.assertIn("执行者没有报告目标已达成", st["card_open_reasons"])

    def test_no_review_goal_met_false_writes_gap(self):
        cmds, st = self.sync({}, {"open_items": [], "self_goal_met": False, "self_goal_gap": "还没跑评测"})
        self.assertEqual(cmds, ["progress"])
        self.assertIn("还没跑评测", self.calls[0][-1])

    def test_no_review_goal_met_true_closes(self):
        cmds, _ = self.sync({}, {"open_items": [], "self_goal_met": True})
        self.assertEqual(cmds, ["state"])
        self.assertEqual(self.calls[0][2], "Done")

    def test_finish_step_stores_self_report_strictly(self):
        src = Path(amb.__file__).read_text(encoding="utf-8")
        self.assertIn('"self_goal_met": res.get("goal_met") if isinstance(res.get("goal_met"), bool) else None', src)
        self.assertIn('"goal_met": true, "goal_gap": ""', src)  # 执行者最终 JSON 模板带这个字段

    def test_long_done_requires_live_children_to_finish(self):
        self.children = {"J-T.c1": {"state": "exec"}}
        st = {"open_items": [], "review": {"goal_met": True}}
        cmds, _ = self.sync({"long": True}, st)
        self.assertEqual(cmds, ["progress"])
        self.assertNotIn("--paused", self.calls[-1])
        self.children = {"J-T.c1": {"state": "done", "open_items": []}}
        self.calls.clear()
        cmds, _ = self.sync({"long": True}, st)
        self.assertEqual(cmds, ["state"])
        self.assertEqual(self.calls[-1][2], "Done")

    def test_notify_refreshes_a_terminal_card_when_last_delegation_finishes(self):
        delegated = {"what": "收评测", "owner": "Ambition", "eta": None}
        self.children = {"J-T.c1": {"state": "exec"}}
        job = {"id": "J-T", "linear": "<REDACTED_ID>", "linear_auto": True}
        _, st = self.sync(job, {"open_items": [delegated], "delegated_open_items": [delegated],
                                "review": {"goal_met": True}})
        self.assertTrue(st["linear_waiting_delegations"])
        st["notified"] = {"linear": "done"}
        Path(self.tmp.name, "J-T.c1", "state.json").write_text('{"state":"failed","open_items":[]}')
        cfg = {"defaults": {}, "linear": {"dry_run": False}}
        with mock.patch.object(amb, "load", return_value=(job, st)), mock.patch.object(amb, "save"):
            amb.notify(job["id"], cfg)
        self.assertIn("--paused", self.calls[-1])
        self.assertNotIn("收评测", self.calls[-1][-1])
        self.assertNotIn("linear_waiting_delegations", st)

    def test_resolving_last_background_item_pauses_ended_job(self):
        item = {"what": "收评测", "owner": "Ambition", "eta": None}
        job = {"id": "J-T", "linear": "<REDACTED_ID>", "linear_auto": True}
        _, st = self.sync(job, {"state": "done", "open_items": [item], "self_goal_met": False})
        _, activity = amb.card_activity(job["linear"])
        key, = activity["pending"]
        path = Path(self.tmp.name, "out.md")
        path.write_text("## 现在到哪了\n已收到评测。\n\n## 下一步\n由 Ambition 核实完成标准。\n")
        args = SimpleNamespace(id=job["id"], file=str(path), resolve=[key], ask=None)
        with mock.patch.object(amb, "root", return_value=self.tmp.name), \
                mock.patch.object(amb, "load", return_value=(job, st)):
            amb.cmd_progress(args, {"linear": {"dry_run": False}})
        self.assertIn("--paused", self.calls[-1])
        _, updated = amb.card_activity(job["linear"])
        self.assertIn(key, updated["resolved"])

    def test_failed_and_blocked_pause_a_managed_card(self):
        for phase in ("failed", "blocked"):
            with self.subTest(phase=phase):
                self.calls.clear()
                cmds, _ = self.sync({"long": True}, {"state": phase, "needs_human": "前置评测未通过"})
                self.assertEqual(cmds, ["progress"])
                self.assertIn("--paused", self.calls[-1])
                self.assertIn("前置评测未通过", self.calls[-1][-1])

    def test_attached_long_card_keeps_state_after_failure(self):
        cmds, _ = self.sync({"long": True, "linear_auto": False, "linear_attach": True}, {"state": "failed"})
        self.assertEqual(cmds, ["progress"])
        self.assertNotIn("--paused", self.calls[-1])

    def test_failed_writeback_is_not_acknowledged(self):
        job = {"id": "J-T", "linear": "<REDACTED_ID>", "linear_auto": True}
        cfg = {"linear": {"dry_run": False}}
        with mock.patch.object(amb, "linear_sync_state", side_effect=[None, "ok"]) as sync:
            self.assertFalse(amb.linear_update("J-T", job, {"state": "failed"}, cfg, 0))
            self.assertTrue(amb.linear_update("J-T", job, {"state": "failed"}, cfg, 0))
        self.assertEqual(sync.call_count, 2)
