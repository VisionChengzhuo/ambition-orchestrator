import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import amb_events


class JobEventsTest(unittest.TestCase):
    def test_http_retry_is_per_event_and_success_is_deduplicated(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((body, self.headers["<REDACTED_AUTH_HEADER>"]))
                self.send_response(400 if len(calls) == 1 else 204)
                self.end_headers()

            def log_message(self, *_):
                pass

        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("<REDACTED_IP>", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                root = Path(tmp)
                wh = {"url": f"http://<REDACTED_IP>:{server.server_port}/", "secret": "<REDACTED_CREDENTIAL>",
                      "header": "<REDACTED_AUTH_HEADER>", "client_error_backoff_s": 900}
                amb_events.emit(root, "J-test", "编排器任务", "failed", "重试此事件", "/orch/result.md")
                amb_events.flush(root, wh)
                amb_events.emit(root, "direct-test", "直通任务", "cancelled",
                                "第一行\napi_key=stub-token\n第三行\n第四行", "/direct/result.md")
                amb_events.flush(root, wh)
                self.assertEqual([body["id"] for body, _ in calls], ["J-test", "direct-test"])
                self.assertEqual(calls[1], ({
                    "type": "amb_job_event", "id": "direct-test", "title": "直通任务", "state": "cancelled",
                    "summary": "第一行\n[已隐去]\n第三行", "result_path": "/direct/result.md",
                    "reply_guidance": (
                        "用任务名称指代事项，先说结论。正文、列表和链接文字默认不展示 TCZ 卡号；"
                        "要附卡片链接时，用任务名称作链接文字。只有用户明确要卡号时才给。再写「执行模型：<模型名或 ID>｜推理档位：<实际档位>」。"
                        "按实际完成工作的执行记录填写，无法核实的字段写「未确认」。省略 Codex 等执行工具名。"
                    ),
                }, wh["secret"]))
                directory = amb_events.event_dir(root)
                pending, = directory.glob("*.json")
                entry = json.loads(pending.read_text())
                self.assertEqual(entry["http_status"], 400)
                entry["retry_at"] = 0
                pending.write_text(json.dumps(entry))
                amb_events.flush(root, wh)
                amb_events.emit(root, "J-test", "编排器任务", "failed", "重试此事件", "/orch/result.md")
                amb_events.flush(root, wh)
                self.assertEqual([body["id"] for body, _ in calls], ["J-test", "direct-test", "J-test"])
                self.assertEqual(len(list((directory / "sent").glob("*.json"))), 2)
                self.assertEqual(list(directory.glob("*.json")), [])
            finally:
                server.shutdown()
                worker.join()
