"""路由、复核证据与监工转发的回归测试。"""
import json
from collections import Counter
from pathlib import Path

import amb
from tests.test_t2 import FAKES, JOB_ID, TERMINAL, T2Base, role_cmd
from tests.test_t4 import T4Base


class TestRouterV4(T2Base):
    def test_opus_host_command_and_l1_doc_skip_review(self):
        local = self.script("local_opus.sh", """echo local-host-probe > host.txt
printf '%s\n' '{"summary":"local override"}' > "$3"
""")
        self.set_role("exec.opus", FAKES / "exec_garbage.sh")
        self.config["roles"]["exec.opus"]["host_commands"] = {"local": role_cmd(local)}
        self.config["hosts"]["local"]["work_root"] = str(self.repo / "nested-work")
        self.save_config()
        self.submit(kind="doc", tier="L1", exec="opus", accept=[], checklist=["交付"])
        st = self.tick_until(lambda s: s["state"] in TERMINAL)
        self.assertEqual(st["state"], "done")
        self.assertEqual((Path(st["wt"]) / "host.txt").read_text().strip(), "local-host-probe")
        self.assertFalse(self.history_to(st, "review"))
        self.assertTrue((Path(st["wt"]) / ".git").is_dir())
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base_sha)
        fallback = amb.role_argv(self.config, "exec.opus", {}, host="<REDACTED_HOST_A>")
        self.assertEqual(Path(fallback[0]), FAKES / "exec_garbage.sh")

    def test_simple_code_l1_without_accept_skips_review(self):
        self.submit(tier="L1", accept=[])
        st = self.tick_until(lambda s: s["state"] in TERMINAL)
        self.assertEqual(st["state"], "done", st)
        self.assertFalse(self.history_to(st, "review"))

    def test_code_l2_without_accept_uses_review_with_executor_evidence(self):
        executor = self.script("evidence.sh", """printf '%s\n' 'receipt: recorded-123' '{"summary":"did it","unverified":["first run"]}' > "$3"
""")
        self.set_role("exec.sol", executor)
        self.submit(tier="L2", accept=[])
        st = self.tick_until(lambda s: s["state"] in TERMINAL)
        self.assertEqual(st["state"], "done", st)
        self.assertFalse(st.get("merged"))
        self.assertTrue(self.history_to(st, "review"))
        prompt = (self.jobs / JOB_ID / "attempts" / "1" / "review" / "prompt.md").read_text()
        for evidence in ("did it", "first run", "receipt: recorded-123"):
            self.assertIn(evidence, prompt)


class TestSupervisor(T4Base):
    def test_archive_filter_retry_and_dedup(self):
        outbox = self.root / "outbox.jsonl"
        pending = self.root / "pending.md"
        before, after = "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
        items = [
            {"ts": before, "task": "old", "action": "blocked"},
            {"ts": after, "task": "routine", "action": "FYI"},
            {"ts": after, "task": "decision", "action": "blocked", "reason": "需要确认", "event_id": "stable"},
            {"ts": "2026-01-02T00:01:00Z", "task": "decision", "action": "blocked", "reason": "需要确认", "event_id": "stable"},
        ]
        outbox.write_text("".join(json.dumps(x) + "\n" for x in items) + '{"unfinished":true}')
        pending.write_text(f"- {after}｜提案：范围待定\n")
        self.config["supervisor"] = {
            "enabled": True, "host": "local", "outbox": str(outbox),
            "pending": str(pending), "since": after,
        }
        recorder = self.hook(codes=[500])
        self.config["webhook"] = {"url": recorder.url}
        self.save_config()
        self.tick()
        outbox.write_text("")
        self.tick()
        self.tick()
        bodies = [body for _, body in recorder.requests]
        self.assertEqual([b["task"] for b in bodies], ["decision", "supervisor", "decision"])
        self.assertEqual(bodies[0]["event_id"], bodies[2]["event_id"])
        ledger = json.loads((self.jobs / "_supervisor" / "receipts.json").read_text())
        self.assertEqual(Counter(x["outcome"] for x in ledger.values()), {
            "archived_before_activation": 1, "archived_no_explicit_human_action": 1, "delivered": 2,
        })
        evidence = self.jobs / "_supervisor" / (bodies[2]["event_id"] + ".json")
        self.assertEqual(json.loads(evidence.read_text())["reason"], "需要确认")
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)


class TestNotificationActivation(T4Base):
    def test_activation_ignores_prior_state(self):
        recorder = self.hook()
        self.config["notifications_since"] = "2099-01-01T00:00:00Z"
        self.config["webhook"] = {"url": recorder.url, "client_error_backoff_s": 60, "metadata_only": True}
        self.save_config()
        self.done_job()
        self.amb("_job-events")
        self.assertEqual(recorder.requests, [])
