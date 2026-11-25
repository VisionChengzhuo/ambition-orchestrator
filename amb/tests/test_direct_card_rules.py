"""Use the actual direct-run entry point's card sync with an isolated Linear fake."""
import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = (_ROOT / "bin" if (_ROOT / "bin" / "amb-direct-run").is_file() else Path.home() / "bin") / "amb-direct-run"
_LOADER = importlib.machinery.SourceFileLoader("direct_card_test", str(_SCRIPT))
_SPEC = importlib.util.spec_from_loader(_LOADER.name, _LOADER)
direct = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(direct)


class DirectCardTests(unittest.TestCase):
    def test_only_explicit_empty_open_items_and_goal_can_close_card(self):
        cases = [
            ({"summary": "结果", "open_items": [], "goal_met": True}, False, "state"),
            ({"summary": "结果", "open_items": []}, False, "progress"),
            ({"summary": "结果", "open_items": [], "goal_met": False, "goal_gap": "差评测"}, False, "progress"),
            ({"summary": "结果", "open_items": [], "goal_met": True}, True, "state"),
            ({"summary": "结果"}, False, "progress"),
            ({"summary": "结果", "open_items": [{"what": "缺时间"}]}, False, "progress"),
            ({"summary": "结果", "open_items": [
                {"what": "评测", "eta": "2026-11-22T09:00:00+08:00", "owner": "Sol"}]}, False, "progress"),
        ]
        for result, long, expected in cases:
            with self.subTest(result=result, long=long), tempfile.TemporaryDirectory() as path, \
                    patch.object(direct, "linear_app", return_value="ok") as linear:
                directory = Path(path)
                (directory / "out.md").write_text("全文\n\n" + "证据" * 9000)
                status = {"linear": "<REDACTED_ID>", "phase": "done", "long": long}
                direct.sync_card(directory, status, json.dumps(result, ensure_ascii=False))
                call = linear.call_args.args
                self.assertEqual(call[0], expected)
                if expected == "state":
                    self.assertEqual(call[2], "Done")
                    self.assertTrue(Path(call[3]).read_text().endswith("证据" * 9000))
                else:
                    self.assertIn("## 下一步", Path(call[2]).read_text())


class DirectGoalTests(unittest.TestCase):
    """linear_plan 6.2：直通任务建卡同样要求目标，不填拒绝，不建目录、不建卡。"""

    def args(self, prompt, goal=None):
        from types import SimpleNamespace
        return SimpleNamespace(prompt=str(prompt), name="goal-test", after=[], under="<REDACTED_ID>", long=False,
                               project=None, goal=goal, title="写清卡面", agent="codex", asked_at=None,
                               model=None, effort=None)

    def test_missing_goal_rejected_before_any_side_effect(self):
        import os
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, {}, clear=False), \
                patch.object(direct, "linear_app") as linear:
            os.environ.pop("AMB_DIRECT_ROOT", None)
            prompt = Path(path, "p.md")
            prompt.write_text("只有任务说明。\n")
            with self.assertRaises(ValueError) as err:
                direct.run_task(self.args(prompt), Path(path, "base"))
            self.assertIn("--goal", str(err.exception))
            self.assertFalse(Path(path, "base").exists())
            linear.assert_not_called()

    def test_goal_section_in_prompt_is_accepted(self):
        import os
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, {}, clear=False), \
                patch.object(direct, "linear_app", side_effect=RuntimeError("stop")) as linear:
            os.environ.pop("AMB_DIRECT_ROOT", None)
            prompt = Path(path, "p.md")
            prompt.write_text("# t\n\n## 目标\n写清卡面。完成标准：测试通过。\n\n## 材料\n略\n")
            with self.assertRaises(RuntimeError):
                direct.run_task(self.args(prompt), Path(path, "base"))
            call = linear.call_args.args
            self.assertEqual(call[call.index("--goal") + 1], "写清卡面。完成标准：测试通过。")



def test_running_progress_and_long_cancel_use_correct_state(tmp_path):
    directory = tmp_path / "active"
    directory.mkdir()
    status = {"phase": "running", "long": True, "linear": "<REDACTED_ID>"}
    (directory / "status.json").write_text(json.dumps(status))
    report = tmp_path / "progress.md"
    report.write_text("## 现在到哪了\n正在验收\n\n## 下一步\n更新产物\n")
    args = SimpleNamespace(name="active", file=str(report), ask=None)
    with patch.object(direct, "linear_app", return_value="<REDACTED_ID>") as linear:
        assert direct.progress(args, tmp_path) == 0
        call = linear.call_args.args
        assert call[:2] == ("progress", "<REDACTED_ID>")
        assert Path(call[2]).read_text() == report.read_text()
        direct.sync_card(directory, {**status, "phase": "cancelled", "error": "用户调整方向"}, "")
        call = linear.call_args.args
        assert call[:3] == ("state", "<REDACTED_ID>", "Canceled")
        assert Path(call[3]).read_text().strip() == "用户调整方向"


def test_failed_direct_finish_updates_card_and_keeps_exit_code(tmp_path):
    status = {"name": "failed-step", "title": "核对评测", "linear": "<REDACTED_ID>", "speed_hint": False}
    with patch.object(direct, "amb_root", return_value=tmp_path), \
            patch.object(direct, "linear_app", side_effect=[None, "ok"]) as linear, \
            patch.object(direct.time, "sleep"):
        direct.finish(tmp_path, status, 17, error="评测进程已退出")
    calls = linear.call_args_list
    assert len(calls) == 2
    assert calls[-1].args[:2] == ("progress", "<REDACTED_ID>")
    assert calls[-1].args[-1] == "--paused"
    assert "评测进程已退出" in Path(calls[-1].args[2]).read_text()
    assert (tmp_path / "exit").read_text() == "17\n"
    assert json.loads((tmp_path / "status.json").read_text())["linear_synced_at"]


def test_oversize_card_result_is_clipped_and_written(tmp_path):
    """<REDACTED_ID>：超长只在写卡时截断，照常写卡，不判失败。"""
    item = {"what": "甲" * 150, "eta": "2026-11-23T00:00:00+08:00", "owner": "Ambition"}
    cases = [
        ("甲" * 401, [], False, "progress", 400),
        ("甲" * 601, [], True, "state", 600),
        ("甲", [item], False, "progress", 400),
        ("甲", [{**item, "what": "待验收"}] * 11, False, "progress", 400),
    ]
    for summary, items, goal_met, verb, cap in cases:
        result = {"summary": summary, "open_items": items, "goal_met": goal_met}
        status = {"linear": "<REDACTED_ID>", "phase": "done"}
        with patch.object(direct, "linear_app", return_value="ok") as linear:
            direct.sync_card(tmp_path, status, json.dumps(result, ensure_ascii=False))
            assert linear.call_args.args[0] == verb
        assert "linear_report_error" not in status
        text = (tmp_path / "linear_result.md").read_text()
        if verb == "progress":
            now, nxt = text.split("## 下一步\n")
            assert len(now.split("\n", 1)[1].strip()) <= cap
            lines = [l[2:] for l in nxt.splitlines() if l.startswith("- ")]
            assert len(lines) <= 10 and all(len(l) <= 100 for l in lines)
        else:
            assert len(text.split("\n\n")[0]) <= cap
