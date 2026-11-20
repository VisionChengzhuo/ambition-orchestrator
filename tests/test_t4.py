"""T4 黑盒测试：结果文件与通知、心跳、ask、reopen、stats。"""
import json
import secrets
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

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

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"

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
        self.assertEqual([s["event"] for s in stats if s["id"] == JOB_ID], ["done"])

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
        self.assertEqual(self.state(nid)["state"], "queued")
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
        fixture_value = secrets.token_hex(16)
        self.config["webhook"] = {"url": rec.url, "secret": fixture_value, "header": "X-Amb-Token"}
        self.save_config()
        self.tick()
        self.tick()
        self.assertEqual(len(rec.requests), 1)
        headers, body = rec.requests[0]
        self.assertEqual(headers.get("X-Amb-Token"), fixture_value)
        self.assertEqual(body["id"], JOB_ID)
        self.assertEqual(body["state"], "done")
        self.assertEqual(self.state()["notified"]["webhook"], "done")

    def test_non_2xx_is_retried(self):
        rec = self.hook(codes=[500])
        self.config["webhook"] = {"url": rec.url, "secret": "", "header": ""}
        self.save_config()
        self.done_job()
        self.tick()
        self.tick()
        self.assertEqual(len(rec.requests), 2, rec.requests)
        self.assertEqual(self.state()["notified"]["webhook"], "done")


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
        self.done_job(linear="DEMO-999")
        text = (self.jobs / JOB_ID / "linear_block.md").read_text(encoding="utf-8")
        head, body = text.split("\n", 1)
        self.assertIn("linear.api_key_file", head)
        start = body.index("<!-- amb:status -->")
        end = body.index("<!-- /amb:status -->")
        inner = [l for l in body[start:end].splitlines()[1:] if l.strip()]
        self.assertEqual(len(inner), 5, inner)
        self.assertIn("done", inner[0])

    def test_real_mode_overwrites_block_between_markers(self):
        calls = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.headers.get("Authorization"), req))
                if req["query"].startswith("query"):
                    data = {"issue": {"id": "uuid-1", "description":
                            "背景\n<!-- amb:status -->\nold\n<!-- /amb:status -->\n尾部"}}
                else:
                    data = {"issueUpdate": {"success": True}}
                out = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        key = self.root / "linear.key"
        fixture_value = secrets.token_hex(16)
        key.write_text(f"{fixture_value}\n", encoding="utf-8")
        self.config["linear"] = {"api_key_file": str(key), "dry_run": False,
                                 "endpoint": f"http://127.0.0.1:{srv.server_port}/graphql"}
        self.save_config()
        self.done_job(linear="DEMO-999")
        updates = [r for _, r in calls if r["query"].startswith("mutation")]
        self.assertTrue(updates)
        desc = updates[-1]["variables"]["d"]
        self.assertTrue(desc.startswith("背景\n<!-- amb:status -->"), desc)
        self.assertTrue(desc.endswith("<!-- /amb:status -->\n尾部"), desc)
        self.assertNotIn("old", desc)
        self.assertIn("done", desc)
        self.assertEqual(calls[0][0], fixture_value)
        self.assertFalse(any("comment" in r["query"].lower() for _, r in calls))
        self.assertFalse((self.jobs / JOB_ID / "linear_block.md").exists())


if __name__ == "__main__":
    unittest.main()
