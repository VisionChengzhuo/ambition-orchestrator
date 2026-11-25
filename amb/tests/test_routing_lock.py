"""锁住路由：L1/简单活不进规划；任何档位默认都不用 Fable；只有 L3 或执行者明确请求才规划。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import amb

CONFIG = json.loads((Path(amb.__file__).with_name("config.json")).read_text(encoding="utf-8"))


def fable(role):
    spec = CONFIG["roles"].get(role) or {}
    return "fable" in role or "fable" in str(spec.get("model", "")) or "fable" in spec.get("cmd", "")


class RoutingLock(unittest.TestCase):
    def test_no_default_uses_fable(self):
        self.assertNotIn("fable", amb.DEFAULT_PLANNER)
        self.assertFalse(fable(CONFIG.get("planner") or amb.DEFAULT_PLANNER))
        for start, rungs in CONFIG["ladder"].items():
            if start == "fable":  # 只有显式 amb new --exec fable 才走这一档（AutoResearch 总指挥），不是默认
                continue
            for rung in [start, *rungs]:
                if rung != "replan":
                    self.assertFalse(fable("exec." + rung), (start, rung))
        reviewer = CONFIG.get("reviewer")
        values = [reviewer] if isinstance(reviewer, str) else list((reviewer or {}).values())
        self.assertFalse(any(fable(v) for v in values), reviewer)

    def test_only_l3_or_explicit_request_plans(self):
        for tier in ("L1", "L2", "L3"):
            for kind in ("code", "doc", "ask"):
                for parent in (None, "J-1"):
                    job = {"tier": tier, "kind": kind, "parent": parent}
                    expect = tier == "L3" and kind != "ask" and parent is None
                    self.assertEqual(amb.can_plan(job, {}), expect, job)
                    asked = {"escalated_to_l3": True, "escalation_reason": "依赖未知"}
                    explicit = tier in ("L2", "L3") and kind != "ask" and parent is None
                    self.assertEqual(amb.can_plan(job, asked), explicit, job)
        # L1 即使执行者请求，也不进规划（阻塞交回 Ambition）。
        self.assertFalse(amb.planning_allowed({"tier": "L1", "kind": "code"}))
        # 只设 escalated 而无原因不算明确请求。
        self.assertFalse(amb.can_plan({"tier": "L2", "kind": "code"}, {"escalated_to_l3": True}))

    def test_kml_sol_failure_blocks_before_queuing_opus(self):
        jid = "J-test"
        job = {"id": jid, "host": "<REDACTED_HOST_A>", "tier": "L1", "exec": "sol"}
        st = amb.initial_state(job)
        st.update(state="exec", run={"host": "<REDACTED_HOST_A>"})

        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"AMB_ROOT": root}):
            amb.fail(jid, job, st, CONFIG, "exec:exit", "original failure detail")
            failure = amb.read_json(amb.att_dir(jid, st, "failure.json"))

        self.assertEqual((st["state"], st["exec"], st["attempt"], st["rung"]), ("blocked", "sol", 1, 0))
        self.assertIsNone(st["run"])
        self.assertEqual(st["last_sig"], "exec:exit")
        self.assertIn("<REDACTED_HOST_A>", st["needs_human"])
        self.assertIn("Opus", st["needs_human"])
        self.assertIn("failure.json", st["needs_human"])
        self.assertEqual(st["history"][-1]["why"], st["needs_human"])
        self.assertEqual((failure["sig"], failure["detail"]), ("exec:exit", "original failure detail"))

    def test_kml_guard_preserves_other_failure_routes(self):
        cases = (
            ("other host", {"host": "<REDACTED_HOST_C>"}, {}, "queued", "opus", 2, 1),
            ("explicit Opus", {"exec": "opus"}, {}, "queued", "opus-high", 2, 1),
            ("ask uses its own role", {"kind": "ask"}, {}, "queued", "opus", 2, 1),
            ("override", {"effort": "high"}, {}, "queued", "sol", 2, 0),
            ("planning", {"tier": "L3"}, {}, "queued", "sol", 2, 1),
            ("repeat skips Opus", {}, {"last_sig": "exec:exit"}, "blocked", "sol", 1, 0),
            ("attempt limit", {"limits": {"max_attempts": 1}}, {}, "blocked", "sol", 1, 0),
        )
        with patch.object(amb, "write_json"):
            for name, job_changes, state_changes, status, executor, attempt, rung in cases:
                with self.subTest(route=name):
                    job = {"id": "J-test", "host": "<REDACTED_HOST_A>", "tier": "L1", "exec": "sol", **job_changes}
                    st = amb.initial_state(job)
                    st.update(state="exec", **state_changes)
                    amb.fail(job["id"], job, st, CONFIG, "exec:exit", "original detail")
                    self.assertEqual((st["state"], st["exec"], st["attempt"], st["rung"]),
                                     (status, executor, attempt, rung))
                    if name == "planning":
                        self.assertTrue(st["plan_needed"])
                    if status == "blocked":
                        self.assertEqual(st["needs_human"], "exec:exit：original detail")


if __name__ == "__main__":
    unittest.main()
