import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

try:
    from test_t4 import T4Base
    from test_t2 import JOB_ID
except ImportError:
    from tests.test_t4 import T4Base
    from tests.test_t2 import JOB_ID

REQ = "req_" + "a" * 32
TOOL = Path(__file__).resolve().parent.parent / "ambition_job_deliver.py"


class TestRequesterChannel(T4Base):
    def setup_cmd(self, ok=True, **rr):
        log = self.root / "requester.log"
        rec = self.script("requester.sh", f"printf '%s\\n' \"$1\" >> '{log}'\n"
                                          f"echo '{{\"ok\": {'true' if ok else 'false'}}}'\n")
        self.config["requester_reply"] = {"cmd": f"{rec} {{job_id}}", "enabled": True, **rr}
        self.save_config()
        return log

    def test_done_job_with_request_is_delivered_once(self):
        log = self.setup_cmd()
        self.done_job(feishu_request_id=REQ)
        for _ in range(3):
            self.tick()
        self.assertEqual(log.read_text().splitlines(), [JOB_ID])
        self.assertEqual(self.state(JOB_ID)["notified"]["requester"], "done")

    def test_failed_delivery_is_retried_on_next_tick(self):
        log = self.setup_cmd(ok=False)
        self.done_job(feishu_request_id=REQ)
        self.tick()
        self.assertGreaterEqual(len(log.read_text().splitlines()), 2)
        self.assertNotIn("requester", self.state(JOB_ID)["notified"])

    def test_no_request_or_before_since_is_not_delivered(self):
        log = self.setup_cmd()
        self.done_job()
        self.assertFalse(log.exists())

    def test_since_gate(self):
        log = self.setup_cmd(since="2999-01-01T00:00:00+00:00")
        self.done_job(feishu_request_id=REQ)
        self.assertFalse(log.exists())

    def test_group_dispatch_saves_request_and_replies_only_to_requester(self):
        log = self.setup_cmd()
        owner_log = self.root / "owner.log"
        owner = self.script("owner.sh", f"touch '{owner_log}'\n")
        self.config["feishu"] = {"cmd": str(owner), "enabled": True}
        self.save_config()
        jid = self.amb("new", "--once", "--repo", str(self.repo), "--feishu-request-id", REQ).stdout.strip()
        draft = self.jobs / "_draft" / jid / "job.json"
        job = json.loads(draft.read_text())
        self.assertEqual(job["feishu_request_id"], REQ)
        job.update(user_words="原群请求", title="群任务")
        draft.write_text(json.dumps(job))
        self.amb("submit", jid)
        self.tick_until(lambda state: state["state"] == "done", job_id=jid)
        self.assertEqual(json.loads((self.jobs / jid / "job.json").read_text())["feishu_request_id"], REQ)
        self.assertEqual(log.read_text().splitlines(), [jid])
        self.assertFalse(owner_log.exists())
        self.assertFalse(list((self.jobs / "_webhook" / "events").glob("*.json")))

    def test_reopen_inherits_or_replaces_request(self):
        self.done_job(feishu_request_id=REQ)
        first = self.amb("reopen", JOB_ID, "--reason", "重跑").stdout.strip()
        other = "req_" + "b" * 32
        second = self.amb("reopen", first, "--reason", "新的群请求", "--feishu-request-id", other).stdout.strip()
        for jid, request_id in ((first, REQ), (second, other)):
            job = json.loads((self.jobs / jid / "job.json").read_text())
            self.assertEqual(job["feishu_request_id"], request_id)


def load_tool():
    spec = importlib.util.spec_from_file_location("ajd", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDeliverTool(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.wt = self.tmp / "wt"
        (self.wt / "videos").mkdir(parents=True)
        (self.wt / "out.md").write_text("| 模型 | 成功 |\n| A | 7/12 |\n详见 <REDACTED_PATH>\n", encoding="utf-8")
        (self.wt / "videos" / "a.mp4").write_bytes(b"mp4a")
        (self.wt / "videos" / "b.mp4").write_bytes(b"mp4b")
        (self.wt / "deliver.json").write_text(json.dumps({"files": ["videos/*.mp4", "../escape.txt"]}))
        (self.tmp / "escape.txt").write_text("no")
        self.calls = self.tmp / "calls.jsonl"
        fake = self.tmp / "helper.py"
        fake.write_text(
            "import json,sys\n"
            f"m=json.load(sys.stdin);open({str(self.calls)!r},'a').write(json.dumps(m)+'\\n')\n"
            "p=m['params']\n"
            "if m['method']=='deliver':\n"
            "  r={'outputs':[{'output_id':'job:t0','state':'pending'}]} if p['part']=='text' else {'output_id':'f:'+p['filename'],'state':'pending'}\n"
            "elif m['method']=='status': r={'all_sent':True,'outputs':[]}\n"
            "else: r={'stuck':[],'rearmed':[]}\n"
            "print(json.dumps({'ok':True,'result':r}))\n")
        self.amb = self.tmp / "amb"
        jd = self.amb / "jobs" / "J-9"
        jd.mkdir(parents=True)
        (jd / "job.json").write_text(json.dumps({"id": "J-9", "feishu_request_id": REQ}))
        (jd / "state.json").write_text(json.dumps({"state": "done", "wt": str(self.wt),
                                                   "history": [{"ts": "2026-01-01T00:00:00+00:00"}]}))
        (jd / "result.json").write_text(json.dumps({"summary": "总结"}))
        (self.amb / "config.json").write_text("{}")
        os.environ["AMB_ROOT"] = str(self.amb)
        os.environ["AMB_JOB_DELIVER_HELPER"] = f"python3 {fake}"
        self.addCleanup(os.environ.pop, "AMB_ROOT")
        self.addCleanup(os.environ.pop, "AMB_JOB_DELIVER_HELPER")
        self.tool = load_tool()

    def sent(self):
        return [json.loads(l) for l in self.calls.read_text().splitlines()]

    def test_text_then_files_inside_worktree_only(self):
        r = self.tool.deliver("J-9")
        self.assertTrue(r["ok"], r)
        calls = self.sent()
        self.assertEqual([c["params"]["part"] for c in calls], ["text", "file", "file"])
        self.assertTrue(all(c["params"]["request_id"] == REQ and c["params"]["job_id"] == "J-9" for c in calls))
        self.assertIn("7/12", calls[0]["params"]["text"])
        self.assertNotIn("<REDACTED_PATH>", calls[0]["params"]["text"])
        self.assertEqual([c["params"]["filename"] for c in calls[1:]], ["a.mp4", "b.mp4"])
        receipt = json.loads((self.amb / "jobs" / "J-9" / "requester_delivery.json").read_text())
        self.assertTrue(receipt["queued"])

    def test_failed_job_sends_short_notice_without_files(self):
        st = self.amb / "jobs" / "J-9" / "state.json"
        data = json.loads(st.read_text()); data["state"] = "failed"; st.write_text(json.dumps(data))
        self.assertTrue(self.tool.deliver("J-9")["ok"])
        calls = self.sent()
        self.assertEqual(len(calls), 1)
        self.assertIn("没有做成", calls[0]["params"]["text"])

    def test_check_heals_missing_receipt_and_reports_ok(self):
        r = self.tool.check(heal=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["healed"], ["J-9:未排队"])
        r = self.tool.check(heal=False)
        self.assertEqual((r["ok"], r["delivered"]), (True, 1))

    def test_check_without_heal_reports_problem(self):
        r = self.tool.check(heal=False)
        self.assertFalse(r["ok"])
        self.assertEqual(r["problems"], ["J-9:未排队"])

    def test_invalid_request_id_refused(self):
        (self.amb / "jobs" / "J-9" / "job.json").write_text(json.dumps({"id": "J-9", "feishu_request_id": "x"}))
        with self.assertRaises(SystemExit):
            self.tool.deliver("J-9")


if __name__ == "__main__":
    unittest.main()


class TestBriefNote(unittest.TestCase):
    def test_brief_gets_delivery_note_only_for_feishu_jobs(self):
        import amb
        self.assertEqual(amb.augment_brief({"_brief": "x"}, {}), "x")
        note = amb.augment_brief({"_brief": "x", "feishu_request_id": REQ}, {})
        self.assertIn("deliver.json", note)
        self.assertEqual(amb.augment_brief({"_brief": note, "feishu_request_id": REQ}, {}), note)
