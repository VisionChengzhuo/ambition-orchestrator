"""T4 黑盒测试：结果文件与通知、心跳、ask、reopen、stats。"""
import json
import time
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
import amb_events

try:
    from test_t2 import FAKES, JOB_ID, TERMINAL, T2Base
except ImportError:
    from tests.test_t2 import FAKES, JOB_ID, TERMINAL, T2Base


class Recorder:
    def __init__(self, codes):
        self.codes, self.requests = list(codes), []
        rec = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                rec.requests.append((dict(self.headers), json.loads(body)))
                self.send_response(rec.codes.pop(0) if rec.codes else 200)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("<REDACTED_IP>", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://<REDACTED_IP>:{self.server.server_port}/hook"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class T4Base(T2Base):
    def done_job(self, **kw):
        self.submit(**kw)
        return self.tick_until(lambda s: s["state"] in TERMINAL, what="terminal")

    def hook(self, codes=()):
        rec = Recorder(codes)
        self.addCleanup(rec.close)
        return rec

    def wait_requests(self, recorder, count):
        deadline = time.monotonic() + 5
        while len(recorder.requests) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(recorder.requests), count)


class TestT4Results(T4Base):
    def test_result_files(self):
        st = self.done_job()
        res = json.loads((self.jobs / JOB_ID / "result.json").read_text(encoding="utf-8"))
        for k in ("id", "state", "attempt", "exec_model", "review_model", "sha", "pre_merge_sha", "merged",
                  "accept", "review", "summary", "unverified", "needs_human", "evidence_dir"):
            self.assertIn(k, res)
        self.assertEqual(res["sha"], st["sha"])
        self.assertEqual(res["accept"][0]["result"], "pass")
        self.assertTrue((self.jobs / JOB_ID / res["accept"][0]["log"]).is_file())
        md = self.amb("result", JOB_ID).stdout
        self.assertLessEqual(len(md.strip().splitlines()), 6)
        self.assertIn(JOB_ID, md.splitlines()[0])

    def test_heartbeat_and_stats(self):
        self.done_job()
        self.assertTrue((self.root / "heartbeat").is_file())
        stats = [json.loads(l) for l in (self.root / "stats.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([s["event"] for s in stats if s["id"] == JOB_ID],
                         ["timing_dispatched", "timing_started", "done"])
        done = stats[-1]
        for key in ("asked_at", "dispatched_at", "started_at", "finished_at"):
            self.assertIsNotNone(done[key], key)
        self.assertIsNone(done["delivered_at"])
        first = self.amb("delivered", JOB_ID).stdout.strip()
        self.assertEqual(self.amb("delivered", JOB_ID).stdout.strip(), first)
        speed = json.loads(self.amb("speed", "--json").stdout)
        self.assertEqual(speed["samples"]["delivery"], 1)
        self.assertIsNotNone(speed["post_completion_median_minutes"])

    def test_ask_has_fixed_unverified(self):
        jid = self.amb("ask", "--host", "local", "磁盘还剩多少？$(touch pwned)").stdout.strip()
        st = self.tick_until(lambda s: s["state"] in TERMINAL, job_id=jid, what="ask done")
        self.assertEqual(st["state"], "done", st)
        self.assertIn("查询回答未经验收", self.amb("result", jid).stdout)
        self.assertFalse((self.root / "pwned").exists())

    def test_reopen_creates_new_job_and_stats(self):
        self.done_job()
        nid = self.amb("reopen", JOB_ID, "--reason", "结果不对：少了一行").stdout.strip()
        self.assertEqual(nid, f"{JOB_ID}r1")
        self.assertEqual(self.state()["state"], "reopened")
        self.assertEqual(self.state(nid)["history"][0]["to"], "queued")
        brief = (self.jobs / nid / "brief.md").read_text(encoding="utf-8")
        self.assertIn("结果不对：少了一行", brief)
        self.assertIn(JOB_ID, brief)
        stats = (self.root / "stats.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event": "reopen"', stats)
        self.amb("reopen", JOB_ID, "--reason", "again", expected=0)
        self.assertTrue((self.jobs / f"{JOB_ID}r2").is_dir())


class TestT4Webhook(T4Base):
    def test_empty_url_then_backfill_exactly_once(self):
        self.done_job()
        self.assertIs(self.state()["notified"]["webhook"], False)
        rec = self.hook()
        self.config["webhook"] = {"url": rec.url, "secret": "<REDACTED_CREDENTIAL>", "header": "X-Amb-Token"}
        self.save_config()
        self.tick()
        self.tick()
        self.wait_requests(rec, 1)
        headers, body = rec.requests[0]
        self.assertEqual(headers.get("X-Amb-Token"), "<REDACTED_CREDENTIAL>")
        self.assertEqual(body["id"], JOB_ID)
        self.assertEqual(body["state"], "done")
        sent = amb_events.event_dir(self.root) / "sent"
        # 收件端先记下请求再回 200，发送端拿到响应后才写 sent；满载时两者有先后，等落盘再数
        deadline = time.monotonic() + 5
        while not list(sent.glob("*.json")) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(list(sent.glob("*.json"))), 1)

    def test_non_2xx_is_retried(self):
        rec = self.hook(codes=[500])
        self.config["webhook"] = {"url": rec.url, "secret": "", "header": ""}
        self.save_config()
        self.done_job()
        self.wait_requests(rec, 1)
        event_dir = amb_events.event_dir(self.root)
        pending, = event_dir.glob("*.json")
        deadline = time.monotonic() + 5
        while not json.loads(pending.read_text()).get("retry_at") and time.monotonic() < deadline:
            time.sleep(0.02)
        retry = json.loads(pending.read_text())
        self.assertEqual(retry["http_status"], 500)
        retry["retry_at"] = 0
        pending.write_text(json.dumps(retry))
        self.amb("_job-events")
        self.wait_requests(rec, 2)
        self.assertFalse(pending.exists())


class TestT4Feishu(T4Base):
    def test_feishu_once_per_state_and_short(self):
        log = self.root / "feishu.log"
        rec = self.script("feishu.sh", f"printf '%s\\n' \"$1\" >> '{log}'\n")
        self.config["feishu"] = {"cmd": f"{rec} {{text}}", "enabled": True}
        self.save_config()
        self.done_job()
        for _ in range(3):
            self.tick()
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertLessEqual(len(lines[0]), 40)
        self.assertIn(JOB_ID, lines[0])

    def test_feishu_disabled_by_default(self):
        log = self.root / "feishu.log"
        rec = self.script("feishu.sh", f"echo \"$1\" >> '{log}'\n")
        self.config["feishu"] = {"cmd": f"{rec} {{text}}"}
        self.save_config()
        self.done_job()
        self.assertFalse(log.exists())


class TestT4Linear(T4Base):
    def test_dry_run_block_has_five_lines_and_missing_fields(self):
        self.done_job(linear="<REDACTED_ID>")
        text = (self.jobs / JOB_ID / "linear_block.md").read_text(encoding="utf-8")
        head, body = text.split("\n", 1)
        self.assertIn("linear.api_key_file", head)
        start = body.index("<!-- amb:status -->")
        end = body.index("<!-- /amb:status -->")
        inner = [l for l in body[start:end].splitlines()[1:] if l.strip()]
        self.assertEqual(len(inner), 5, inner)
        self.assertIn("done", inner[0])

    def test_legacy_key_config_does_not_write_to_linear_directly(self):
        recorder = self.hook()
        key = self.root / "linear.key"
        key.write_text("lin_test_key\n", encoding="utf-8")
        self.config["linear"] = {"api_key_file": str(key), "dry_run": False,
                                 "endpoint": recorder.url}
        self.save_config()
        self.done_job(linear="<REDACTED_ID>")
        self.assertEqual(recorder.requests, [])
        self.assertFalse((self.jobs / JOB_ID / "linear_block.md").exists())


if __name__ == "__main__":
    unittest.main()
