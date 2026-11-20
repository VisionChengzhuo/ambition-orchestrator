import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import amb
import amb_speed


class TestSpeedSupervisor(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.direct = self.root / "direct"
        self.work = self.root / "speed"
        self.work.mkdir()
        env = patch.dict(os.environ, {"AMB_ROOT": str(self.root), "AMB_DIRECT_ROOT": str(self.direct)})
        env.start()
        self.addCleanup(env.stop)

    def test_rate_limit_and_sentinel_only_for_new_decisions(self):
        now = 5000
        alert = {"task": "J-0101-01", "phase": "review", "action": "needs_decision"}
        self.assertTrue(amb_speed.eligible({"runs": [now - 1]}, [alert], now))
        state = {"runs": [now - 3, now - 2, now - 1, now], "incidents": []}
        self.assertFalse(amb_speed.eligible(state, [alert], now))
        self.assertEqual(amb_speed.advice_events([alert], state), [alert])
        state["incidents"] = ["J-0101-01:review"]
        self.assertEqual(amb_speed.advice_events([alert], state), [])
        self.assertFalse(amb_speed.eligible({"runs": [now - 1], "incidents": state["incidents"]}, [alert], now))

    def test_direct_metadata_reports_dependencies_and_growth(self):
        active = self.direct / "fixture"
        active.mkdir(parents=True)
        (active / "prompt.md").write_text("private prompt must not go in digest")
        (active / "out.md").write_text("old")
        now = time.time()
        cfg = {"phase": "waiting", "created_at": now - 1200, "dependencies": ["prepare"]}
        amb.write_json(active / "status.json", cfg)
        first = amb_speed.direct_metadata(amb, self.direct, self.work, now - 601, sample=True)
        self.assertEqual(first[0]["waiting_on"], ["prepare"])
        (active / "out.md").write_text("longer")
        second = amb_speed.direct_metadata(amb, self.direct, self.work, now)
        self.assertEqual(second[0]["growth_10m"]["out.md"], 3)
        self.assertNotIn("private prompt", json.dumps(second))
        (active / "exit").write_text("0\n")
        self.assertEqual(amb_speed.direct_metadata(amb, self.direct, self.work, now), [])

    def test_reports_deduplicate_and_pending_survives_outbox_errors(self):
        path = self.work / "seen.json"
        seen = {}
        good = {"key": "fixture:超时", "task": "fixture", "text": "规划用时 25 分钟，拆单预计省 10 分钟。"}
        amb_speed.record_reports(amb, path, seen, [good, good, {**good, "task": "unknown"}], {"fixture"})
        self.assertEqual(list(seen), [good["key"]])
        with patch("amb_sentinel.append_outbox", side_effect=amb.HostDown):
            amb_speed.forward_pending(amb, {}, path, seen)
        self.assertTrue(amb.read_json(path)[good["key"]]["pending"])
        with patch("amb_sentinel.append_outbox") as send:
            amb_speed.forward_pending(amb, {}, path, seen)
            amb_speed.forward_pending(amb, {}, path, seen)
        send.assert_called_once()
        self.assertFalse(amb.read_json(path)[good["key"]]["pending"])

    def test_only_revalidated_ready_children_are_started(self):
        for jid, state, job in (
            ("J-parent", {"state": "subs", "subs": ["J-child"]}, {}),
            ("J-child", {"state": "queued"}, {"parent": "J-parent", "depends_on": ["J-dep"]}),
            ("J-dep", {"state": "exec"}, {}),
        ):
            amb.write_json(amb.jobs_dir(jid, "job.json"), job)
            amb.write_json(amb.jobs_dir(jid, "state.json"), state)
        actions = [{"do": "start_ready", "id": "J-child"}, {"do": "cancel", "id": "J-dep"}]
        with patch.object(amb, "tick_job") as tick:
            amb_speed.apply_actions(amb, {}, actions, {"J-child", "J-dep"})
            tick.assert_not_called()
            amb.write_json(amb.jobs_dir("J-dep", "state.json"), {"state": "done"})
            amb_speed.apply_actions(amb, {}, actions, {"J-child", "J-dep"})
            self.assertEqual(tick.call_args.args[0], "J-child")
            self.assertEqual(tick.call_count, 1)

    def test_digest_is_bounded_and_reply_must_be_json_on_last_line(self):
        items = [{"history": "阶段" * 200}] * 100
        sections = {"amb_status": items, "direct": items, "sentinel": items,
                    "today": {"reopened": 0}, "seen_keys": items}
        body, digest = amb_speed.encode_digest(sections)
        self.assertLessEqual(len(body.encode()), 8192)
        self.assertTrue(digest["amb_status"] and digest["direct"])
        self.assertEqual(amb_speed.parse_reply('thinking\n{"actions":[],"reports":[]}'),
                         {"actions": [], "reports": []})
        with self.assertRaises(ValueError):
            amb_speed.parse_reply('{"action": "shell"}')
