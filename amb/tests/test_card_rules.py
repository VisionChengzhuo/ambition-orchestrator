"""Linear 的关闭、挂靠和开子卡规则。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import amb


class AmbCardRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        patches = [mock.patch.object(amb, "jobs_dir", lambda *p: os.path.join(self.tmp.name, *p)),
                   mock.patch.object(amb, "linear_app", lambda cfg, *a: self.calls.append(a) or "ok")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def sync(self, job, st):
        job = {"id": "J-T", "linear": "<REDACTED_ID>", "linear_auto": True, **job}
        st = {"state": "done", "summary": "做完了这一轮。", "history": [], **st}
        amb.linear_sync_state(job, st, {})
        return [c[0] for c in self.calls], st

    def test_long_job_done_calls_progress_not_done(self):
        cmds, st = self.sync({"long": True}, {"open_items": [], "review": {"goal_met": False, "goal_gap": "到达率 0"}})
        self.assertEqual(cmds, ["progress"])
        self.assertFalse(st["card_closable"])
        text = Path(self.tmp.name, "J-T", "linear_result.md").read_text(encoding="utf-8")
        self.assertIn("## 下一步", text)
        self.assertIn("到达率 0", text)

    def test_long_job_goal_met_closes_without_asking(self):
        cmds, _ = self.sync({"long": True}, {"open_items": [], "review": {"goal_met": True}})
        self.assertEqual(cmds, ["state"])
        self.assertEqual(self.calls[0][2], "Done")

    def test_goal_not_met_keeps_card_open(self):
        cmds, _ = self.sync({}, {"open_items": [], "review": {"goal_met": False}})
        self.assertEqual(cmds, ["progress"])

    def test_open_items_keep_card_open(self):
        cmds, _ = self.sync({}, {"open_items": ["H200 tmux eval 还在跑"], "review": {"goal_met": True}})
        self.assertEqual(cmds, ["progress"])
        cmds, _ = self.sync({}, {"review": {"goal_met": True}})  # 没报告 = 按有未收尾
        self.assertEqual(cmds[-1], "progress")

    def test_closable_card_goes_done(self):
        cmds, _ = self.sync({}, {"open_items": [], "review": {"goal_met": True}})
        self.assertEqual(cmds, ["state"])
        self.assertEqual(self.calls[0][2], "Done")

    def test_cancelled_goes_canceled_with_reason(self):
        cmds, _ = self.sync({}, {"state": "cancelled", "history": [{"to": "cancelled", "why": "用户取消"}]})
        self.assertEqual(self.calls[0][:3], ("state", "<REDACTED_ID>", "Canceled"))
        self.assertEqual(Path(self.calls[0][3]).read_text(encoding="utf-8"), "用户取消\n")

    def test_attached_card_never_changes_state(self):
        cmds, _ = self.sync({"linear_auto": False, "linear_attach": True}, {"open_items": [], "review": {"goal_met": True}})
        self.assertEqual(cmds, ["progress"])

    def test_new_flags(self):
        a = SimpleNamespace(kind="doc", tier=None, host="local", repo=None, exec="fable", asked_at=None,
                            under=None, long=True, project=None, linear=None, campaign=None)
        with mock.patch.object(amb, "new_id", return_value="J-T"), mock.patch.object(amb, "append_jsonl"):
            with self.assertRaises(amb.AmbError):
                amb.create_draft(a)
            a.project, a.campaign, a.linear = "机器狗校园导航", "pku-nav", "<REDACTED_ID>"
            amb.create_draft(a)
        job = json.loads(Path(self.tmp.name, "_draft", "J-T", "job.json").read_text(encoding="utf-8"))
        self.assertEqual((job["exec"], job["long"], job["linear_project"], job["campaign"]),
                         ("fable", True, "机器狗校园导航", "pku-nav"))
        self.assertEqual((job["linear"], job["linear_attach"], job["linear_auto"]), ("<REDACTED_ID>", True, False))

    LIVE = {"linear": {"auto_create": True, "dry_run": False}}

    def auto_card(self, job, brief):
        job = {"id": "J-T", "title": "接着推机器狗导航", "card_type": "once", **job}
        with mock.patch.object(amb, "lead_card", return_value=None):
            amb.linear_auto_card(job, brief, self.LIVE)
        return job

    def test_lead_steps_require_explicit_under_to_create_a_child_card(self):
        self.auto_card({"lead_parent": "J-P"}, "# t\n")
        self.assertEqual(self.calls, [])
        self.auto_card({"lead_parent": "J-P", "linear_parent": "<REDACTED_ID>", "goal": "核对产物帧率。"}, "# t\n")
        self.assertEqual(self.calls[0][:2], ("new", "接着推机器狗导航"))
        self.assertEqual(self.calls[0][self.calls[0].index("--parent") + 1], "<REDACTED_ID>")

    def test_nested_task_without_a_parent_card_still_skips_card_creation(self):
        with mock.patch.object(amb, "lead_card", return_value=""):
            amb.linear_auto_card({"id": "J-T"}, "", self.LIVE)
        self.assertEqual(self.calls, [])

    def test_new_card_requires_goal(self):
        """linear_plan 6.2：目标是派单必填项，不填拒绝提交，也不建卡。"""
        with self.assertRaises(amb.AmbError) as err:
            self.auto_card({}, "# 接着推机器狗导航\n\n只有背景。\n")
        self.assertIn("--goal", str(err.exception))
        self.assertEqual(self.calls, [])

    def test_goal_flag_or_brief_section_goes_to_card(self):
        self.auto_card({"goal": "机器狗在北大校园任意点导航。完成标准：三档到达率≥80%"}, "# t\n")
        call = self.calls[-1]
        self.assertEqual(call[0], "new")
        self.assertEqual(call[call.index("--goal") + 1], "机器狗在北大校园任意点导航。完成标准：三档到达率≥80%")
        brief = "# 标题\n\n## 目标\n让卡面写清目标。\n完成标准：全量测试通过。\n\n## 背景与假设\n略\n"
        self.auto_card({}, brief)
        call = self.calls[-1]
        self.assertEqual(call[call.index("--goal") + 1], "让卡面写清目标。\n完成标准：全量测试通过。")

    def test_attached_or_dry_run_needs_no_goal(self):
        self.auto_card({"linear": "<REDACTED_ID>", "linear_attach": True}, "# t\n")
        amb.linear_auto_card({"id": "J-T"}, "# t\n", {"linear": {"auto_create": True, "dry_run": True}})
        self.assertEqual(self.calls, [])

    def test_new_stores_goal(self):
        a = SimpleNamespace(kind="doc", tier=None, host="local", repo=None, exec="opus", asked_at=None,
                            under=None, long=False, project=None, linear=None, campaign=None,
                            once=True, goal="  修好看板。完成标准：卡面不截断  ")
        with mock.patch.object(amb, "new_id", return_value="J-T"), mock.patch.object(amb, "append_jsonl"):
            amb.create_draft(a)
        job = json.loads(Path(self.tmp.name, "_draft", "J-T", "job.json").read_text(encoding="utf-8"))
        self.assertEqual(job["goal"], "修好看板。完成标准：卡面不截断")

    def test_align_queues_owner_review_without_waiting(self):
        """与人类对齐：AutoResearch 的章程/评审决定经 amb align 同时发飞书并写卡评论。"""
        body = Path(self.tmp.name, "charter.md")
        body.write_text("三队方向：A、B、C。\n", encoding="utf-8")
        cfg = {"feishu": {"cmd": "true"}, "linear": {"dry_run": False}}
        a = SimpleNamespace(file=str(body), title="自动研究 nav · 章程", source="ar-nav", card="<REDACTED_ID>", key="0")
        with mock.patch.object(amb, "launch_background") as bg:
            amb.cmd_align(a, cfg)
            amb.cmd_align(a, cfg)  # 同一条只排一次
        pending = list(Path(self.tmp.name, "_alignment", "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        entry = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertEqual((entry["card"], entry["title"]), ("<REDACTED_ID>", "自动研究 nav · 章程"))
        self.assertIn("三队方向", entry["body"])
        bg.assert_called_once_with("_alignment")
        body.write_text("  \n", encoding="utf-8")
        with self.assertRaises(amb.AmbError):
            amb.cmd_align(a, cfg)

    def test_leader_prompt_asks_for_goal(self):
        tool = next(tool for tool in amb.amb_router.tool_list() if tool["name"] == "dispatch")
        self.assertIn("goal", tool["inputSchema"]["required"])

    def test_prompts_ask_for_goal_and_open_items(self):
        self.assertIn("open_items", amb.exec_prompt.__code__.co_consts.__repr__() + Path(amb.__file__).read_text())
        src = Path(amb.__file__).read_text(encoding="utf-8")
        self.assertIn('"goal_met": false', src)
        self.assertNotIn("brief[:6000]", src)
        self.assertNotIn("[:500]\n    after_exec", src)



    def test_format_rejects_items_without_owner_or_eta(self):
        item = {"what": "评测", "eta": "2026-11-22T09:00:00+08:00", "owner": "Sol"}
        self.assertEqual(amb.validate_open_items({"open_items": [item]}), [item])
        self.assertIsNone(amb.validate_open_items({}))
        for key in ("eta", "owner"):
            with self.subTest(key=key), self.assertRaises(amb.StepError):
                amb.validate_open_items({"open_items": [{k: v for k, v in item.items() if k != key}]})

    def test_unknown_eta_remains_an_open_item(self):
        item = {"what": "收评测", "eta": None, "owner": "Sol"}
        self.assertEqual(amb.validate_open_items({"open_items": [item]}), [item])
        self.assertIn("完成时间待定", amb.open_item_text(item))
        for value in (123, "", "2026-11-22T09:00:00"):
            with self.subTest(eta=value), self.assertRaises(amb.StepError):
                amb.validate_open_items({"open_items": [{**item, "eta": value}]})

    def test_progress_fallback_keeps_questions_and_distinct_updates(self):
        path = Path(self.tmp.name, "progress.md")
        args = SimpleNamespace(id="J-T", file=str(path), resolve=None, ask="请选择评测方案")
        cfg = {"linear": {"dry_run": False}, "webhook": {"config_file": "stub"}}
        job = {"id": "J-T", "title": "评测"}
        with mock.patch.object(amb, "root", return_value=self.tmp.name), \
                mock.patch.object(amb, "load", return_value=(job, {})), \
                mock.patch.object(amb, "wake_job_events"), \
                mock.patch.object(amb, "linear_app", return_value=None) as app:
            path.write_text("## 现在到哪了\n等待决定。\n\n## 下一步\n确认后继续。\n")
            amb.cmd_progress(args, cfg)
            amb.cmd_progress(args, cfg)
            app.assert_not_called()
            job["linear"] = "<REDACTED_ID>"
            path.write_text("## 现在到哪了\n已收到样本。\n\n## 下一步\n确认后继续。\n")
            amb.cmd_progress(args, cfg)
            app.assert_called_once()
        events = [json.loads(p.read_text())["body"]
                  for p in amb.amb_events.event_dir(self.tmp.name).glob("*.json")]
        self.assertEqual(len(events), 2)
        self.assertEqual(len({e["event_id"] for e in events}), 2)
        for event in events:
            self.assertEqual(event["state"], "progress")
            self.assertIn(args.ask, event["summary"])
            self.assertEqual(Path(event["result_path"]).read_text(), event["summary"])

    def test_stale_report_is_rate_limited(self):
        job = {"id": "J-T", "linear": "<REDACTED_ID>"}
        st = {"state": "exec", "started_at": "2026-11-22T00:00:00Z", "linear_tracking": True}
        config = {"linear": {"dry_run": False}}
        with mock.patch.object(amb.time, "time", return_value=amb.parse_time("2026-11-22T03:00:00Z").timestamp()), \
                mock.patch.object(amb, "card_event") as event:
            amb.maintain_card(job, st, config)
            amb.maintain_card(job, st, config)
        self.assertEqual([call[0] for call in self.calls], ["progress"])
        self.assertEqual(self.calls[0][-3:-1], ("--stale", "--if-older-than"))
        event.assert_called_once()

    def test_due_open_item_reminds_once_and_blocks_later_round(self):
        item = {"what": "收评测", "eta": "2026-11-22T06:00:00Z", "owner": "Sol"}
        job = {"id": "J-T", "linear": "<REDACTED_ID>", "linear_auto": True}
        st = {"state": "done", "summary": "交付。", "open_items": [item]}
        config = {"linear": {"dry_run": False}}
        amb.linear_sync_state(job, st, config)
        with mock.patch.object(amb.time, "time", return_value=amb.parse_time("2026-11-22T06:01:00Z").timestamp()), \
                mock.patch.object(amb, "card_event") as event:
            amb.maintain_card(job, st, config)
            amb.maintain_card(job, st, config)
        event.assert_called_once()
        amb.linear_sync_state({**job, "id": "J-Next"}, {**st, "open_items": []}, config)
        self.assertEqual([call[0] for call in self.calls], ["progress", "progress"])

    def test_progress_updates_two_sections_and_removes_resolved_item(self):
        item = {"what": "收评测", "eta": "2026-11-22T06:00:00Z", "owner": "Sol"}
        jid = "J-T"
        job = {"id": jid, "linear": "<REDACTED_ID>", "linear_auto": True}
        cfg = {"linear": {"dry_run": False}}
        st = {"state": "done", "summary": "交付。", "open_items": [item]}
        amb.linear_sync_state(job, st, cfg)
        _, activity = amb.card_activity("<REDACTED_ID>")
        pending_id = next(iter(activity["pending"]))
        path = Path(self.tmp.name, "progress.md")
        path.write_text("## 现在到哪了\n收到结果。\n\n## 下一步\n整理结论。\n")
        with mock.patch.object(amb, "root", return_value=self.tmp.name), \
                mock.patch.object(amb, "load", return_value=(job, st)):
            amb.cmd_progress(SimpleNamespace(id=jid, file=str(path), resolve=[pending_id], ask=None), cfg)
        self.assertEqual([call[0] for call in self.calls], ["progress", "progress"])
        self.assertEqual(Path(self.calls[-1][2]).read_text(), path.read_text())
        self.assertEqual(amb.card_activity("<REDACTED_ID>")[1]["pending"], {})


if __name__ == "__main__":
    unittest.main()


def test_campaign_skips_all_automatic_card_entry_points(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("AMB_ROOT", str(tmp_path))
    job = {"id": "J-test", "campaign": "test", "linear": "<REDACTED_ID>", "linear_auto": True, "long": True}
    cfg = {"linear": {"auto_create": True, "dry_run": False}}
    st = {"state": "cancelled"}
    with mock.patch.object(amb, "linear_app") as app, mock.patch.object(amb, "load", return_value=(job, st)):
        amb.linear_auto_card({**job, "linear": ""}, "", cfg)
        amb.maintain_card(job, st, cfg)
        assert amb.linear_update(job["id"], job, st, cfg, 0) is True
        assert amb.linear_sync_state(job, st, cfg) is None
        with pytest.raises(amb.AmbError, match="AutoResearch"):
            amb.cmd_progress(SimpleNamespace(id=job["id"]), cfg)
        with mock.patch.object(amb, "launch_background"):
            amb.queue_alignment(job, {**cfg, "feishu": {"cmd": "mock"}}, "标题", "内容")
        pending = next(tmp_path.glob("jobs/_alignment/pending/*.json"))
        assert json.loads(pending.read_text())["card"] == ""
        app.assert_not_called()


def test_progress_limits_return_oversize_content_to_author():
    import pytest
    def progress(current, next_line):
        return f"## 现在到哪了\n{current}\n\n## 下一步\n{next_line}\n"
    amb.validate_card_progress(progress("甲" * 400, "- " + "乙" * 100))
    for current, next_line in (("甲" * 401, "乙"), ("甲", "乙" * 101), ("甲", "- 乙\n" * 11)):
        with pytest.raises(ValueError):
            amb.validate_card_progress(progress(current, next_line))


def test_oversize_result_is_clipped_on_card_not_failed(monkeypatch):
    """<REDACTED_ID>：超长摘要和下一步不再判 bad_output，只在写卡时截断。"""
    monkeypatch.setattr(amb, "lead_children", lambda _jid: [])
    monkeypatch.setattr(amb, "do_merge", lambda *a: None)
    job = {"id": "J-test", "linear": "<REDACTED_ID>", "land": "merge"}
    st = {"summary": "甲" * 500, "open_items": [{"what": "乙" * 150, "eta": "2026-11-23T06:00:00+08:00",
          "owner": "x"}] * 12, "self_goal_met": False, "card_limits": True,
          "review": {"verdict": "PASS", "goal_met": False}}
    amb.after_review(job["id"], job, st, {}, None)
    text = amb.card_progress_text(job, {**st, "delegated_open_items": st["open_items"]})
    now = text.split("## 下一步")[0].split("\n", 1)[1].split("\n\n还差")[0].strip()
    assert len(now) <= 400
    items = [l[2:] for l in text.split("## 下一步\n")[1].splitlines() if l.startswith("- ")]
    assert len(items) <= 10 and all(len(i) <= 100 for i in items)
