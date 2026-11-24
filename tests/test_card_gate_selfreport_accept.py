"""J-1122-130：无复核单要执行者自报 goal_met 才关卡；长期卡目标达成且无未收尾时自动申请验收、从不设 Done。"""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import amb


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls, self.fail, self.ask_limit, self.children = [], set(), None, {}

        def fake(cfg, *a):
            amb.LINEAR_ERROR["last"] = ""
            self.calls.append(a)
            if a[0] in ("progress", "ask", "state"):
                written = a[2] if a[0] == "progress" else a[3] if a[0] == "state" else a[2]
                self.calls[-1] = a + (Path(written).read_text() if a[0] != "ask" else written,)
            if a[0] == "ask" and self.ask_limit is not None and len(a[2].encode()) > self.ask_limit:
                amb.LINEAR_ERROR["last"] = amb.CARD_TOO_LONG + "\n"
                return None  # 模拟 linear-app 因卡面超过 12,000 字节而拒绝（写卡之前）
            if a[0] in self.fail:
                amb.LINEAR_ERROR["last"] = "issueUpdate 失败\n"
                return None
            return "ok"
        for p in (mock.patch.object(amb, "jobs_dir", lambda *p: os.path.join(self.tmp.name, *p)),
                  mock.patch.object(amb, "linear_app", fake),
                  mock.patch.object(amb, "lead_children", lambda jid: list(self.children))):
            p.start()
            self.addCleanup(p.stop)

    def sync(self, job, st, result="结果全文：到达率 92%。\n"):
        job = {"id": "J-T", "linear": "TCZ-1", "linear_auto": True, **job}
        os.makedirs(os.path.join(self.tmp.name, "J-T"), exist_ok=True)
        Path(self.tmp.name, "J-T", "result.md").write_text(result)
        for child, cst in self.children.items():
            os.makedirs(os.path.join(self.tmp.name, child), exist_ok=True)
            Path(self.tmp.name, child, "state.json").write_text(json.dumps(cst))
        st = {"state": "done", "summary": "这一轮做完了。", "history": [], **st}
        amb.linear_sync_state(job, st, {})
        return [c[0] for c in self.calls], st

    # 第 1 处：没有复核的单
    def test_no_review_missing_goal_met_stays_in_progress(self):
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

    # 第 2 处：长期卡
    def test_long_goal_met_requests_acceptance_not_done(self):
        cmds, st = self.sync({"long": True}, {"open_items": [], "review": {"goal_met": True}})
        self.assertEqual(cmds, ["ask"])
        ask = self.calls[0]
        self.assertEqual(ask[1], "TCZ-1")
        self.assertIn("--accept", ask)
        self.assertIn("这一轮做完了。", ask[2])
        self.assertIn("结果全文：到达率 92%。", ask[2])
        self.assertEqual(st["acceptance_request"], "sent")

    def test_long_accept_failure_falls_back_to_progress(self):
        self.fail.add("ask")
        cmds, st = self.sync({"long": True}, {"open_items": [], "review": {"goal_met": True}})
        self.assertEqual(cmds, ["ask", "progress"])
        self.assertIn("自动申请验收没成功", self.calls[1][-1])
        self.assertNotIn("state", cmds)

    def test_long_never_asks_without_goal_or_with_open_items(self):
        for st in ({"open_items": [], "review": {"goal_met": False}},
                   {"open_items": [{"what": "H200 tmux eval", "eta": "2026-11-23T10:00:00+08:00", "owner": "Sol"}],
                    "review": {"goal_met": True}},
                   {"review": {"goal_met": True}},
                   {"open_items": [], "self_goal_met": True}):
            self.calls.clear()
            with self.subTest(st=st):
                cmds, _ = self.sync({"long": True}, dict(st))
                self.assertEqual(cmds, ["progress"])

    def test_long_never_asks_while_led_child_running(self):
        self.children = {"J-T.c1": {"state": "exec"}}
        cmds, st = self.sync({"long": True}, {"open_items": [], "review": {"goal_met": True}})
        self.assertEqual(cmds, ["progress"])
        self.assertIn("带队派出的子单仍有未收尾的活", st["card_open_reasons"])
        self.children = {"J-T.c1": {"state": "done", "open_items": []}}
        self.calls.clear()
        cmds, _ = self.sync({"long": True}, {"open_items": [], "review": {"goal_met": True}})
        self.assertEqual(cmds, ["ask"])

    def test_long_accept_evidence_fits_card_limit(self):
        big = "评测明细一行。\n" * 2000  # 约 4 万字节，远超卡面 12,000 字节
        cmds, st = self.sync({"long": True}, {"open_items": [], "review": {"goal_met": True}}, result=big)
        self.assertEqual(cmds, ["ask"])
        evidence = self.calls[0][2]
        self.assertLessEqual(len(evidence.encode()), amb.ACCEPT_EVIDENCE_BYTES[0])
        self.assertIn("这一轮做完了。", evidence)
        full = os.path.join(self.tmp.name, "J-T", "linear_acceptance.md")
        self.assertIn(full, evidence)
        self.assertIn(big.strip(), Path(full).read_text())  # 全文完整落盘
        self.assertEqual(st["acceptance_request"], "sent")

    def test_long_accept_retries_with_shorter_evidence(self):
        self.ask_limit = 2_000  # 卡上别的段已占了大半
        cmds, st = self.sync({"long": True}, {"open_items": [], "review": {"goal_met": True}},
                             result="评测明细一行。\n" * 2000)
        self.assertEqual(cmds, ["ask", "ask"])
        self.assertLessEqual(len(self.calls[1][2].encode()), amb.ACCEPT_EVIDENCE_BYTES[-1])
        self.assertIn("这一轮做完了。", self.calls[1][2])
        self.assertEqual(st["acceptance_request"], "sent")
        self.ask_limit, self.calls[:] = 100, []
        cmds, st = self.sync({"long": True}, {"open_items": [], "review": {"goal_met": True}},
                             result="评测明细一行。\n" * 2000)
        self.assertEqual(cmds, ["ask", "ask", "progress"])
        self.assertEqual(st["acceptance_request"], "failed")

    def test_long_accept_no_shorter_retry_after_other_failure(self):
        # 申请已写、提醒已发、改状态失败：不能换短证据再送（会再发一条提醒、覆盖申请时间）
        self.fail.add("ask")
        cmds, st = self.sync({"long": True}, {"open_items": [], "review": {"goal_met": True}},
                             result="评测明细一行。\n" * 2000)
        self.assertEqual(cmds, ["ask", "progress"])
        self.assertEqual(st["acceptance_request"], "failed")
        self.assertNotIn("state", cmds)

    def test_attached_long_card_never_asks(self):
        cmds, _ = self.sync({"long": True, "linear_auto": False, "linear_attach": True},
                            {"open_items": [], "review": {"goal_met": True}})
        self.assertEqual(cmds, ["progress"])


class AcceptRetryThroughRealWrapper(unittest.TestCase):
    """不替换 amb.linear_app，用一个假的 linear-app 可执行文件走真实子进程和报错解析。"""

    def run_case(self, mode):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = Path(tmp.name, "calls.jsonl")
        app = Path(tmp.name, "linear-app")
        app.write_text(f"""#!/usr/bin/env python3
import json, sys
open({str(log)!r}, "a").write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
if sys.argv[1] != "ask":
    print("ok"); sys.exit(0)
body = sys.argv[3]
if {mode!r} == "too_long" and len(body.encode()) > 2000:
    sys.exit("卡面超过安全长度")
if {mode!r} == "partial":
    open({str(log)!r}, "a").write(json.dumps(["REMINDER", body[:20]], ensure_ascii=False) + "\\n")
    sys.exit("issueUpdate 失败")
print("ok")
""")
        app.chmod(app.stat().st_mode | stat.S_IXUSR)
        with mock.patch.object(amb, "jobs_dir", lambda *p: os.path.join(tmp.name, *p)), \
             mock.patch.object(amb, "lead_children", lambda jid: []):
            os.makedirs(os.path.join(tmp.name, "J-T"), exist_ok=True)
            Path(tmp.name, "J-T", "result.md").write_text("评测明细一行。\n" * 2000)
            st = {"state": "done", "summary": "这一轮做完了。", "history": [],
                  "open_items": [], "review": {"goal_met": True}}
            amb.linear_sync_state({"id": "J-T", "linear": "TCZ-1", "linear_auto": True, "long": True},
                                  st, {"linear": {"app_cmd": str(app)}})
        rows = [json.loads(x) for x in log.read_text().splitlines()]
        return [r[0] for r in rows], st

    def test_card_too_long_retries_shorter_once(self):
        cmds, st = self.run_case("too_long")
        self.assertEqual(cmds, ["ask", "ask"])
        self.assertEqual(st["acceptance_request"], "sent")

    def test_partial_success_does_not_send_second_reminder(self):
        cmds, st = self.run_case("partial")
        self.assertEqual(cmds, ["ask", "REMINDER", "progress"])
        self.assertEqual(cmds.count("REMINDER"), 1)
        self.assertEqual(st["acceptance_request"], "failed")
        self.assertNotIn("state", cmds)


if __name__ == "__main__":
    unittest.main()
