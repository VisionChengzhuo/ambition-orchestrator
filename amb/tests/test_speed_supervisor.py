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
        alert = {"task": "<REDACTED_ID>", "phase": "review", "action": "needs_decision"}
        self.assertTrue(amb_speed.eligible({"runs": [now - 1]}, [alert], now))
        state = {"runs": [now - 3, now - 2, now - 1, now], "incidents": []}
        self.assertFalse(amb_speed.eligible(state, [alert], now))
        self.assertEqual(amb_speed.advice_events([alert], state), [alert])
        state["incidents"] = ["<REDACTED_ID>:review"]
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

    def test_direct_stall_reports_require_ten_minutes_without_nested_writes(self):
        directory = self.direct / "fixture"
        artifact = directory / "sim" / "runs" / "episodes.csv"
        artifact.parent.mkdir(parents=True)
        now = time.time()
        for path in [artifact, *(directory / name for name in ("prompt.md", "out.md", "err.log", "log.txt"))]:
            path.touch()
            os.utime(path, (now - 601, now - 601))
        (directory / "inbox").write_text("incoming message")
        report = {"task": "fixture", "key": "fixture:无写入"}

        def metadata():
            return amb_speed.direct_metadata(amb, self.direct, self.work, now)

        with patch.object(amb_speed.time, "time", return_value=now):
            idle = metadata()
            self.assertTrue(idle[0]["idle_10m"])
            self.assertEqual(amb_speed.filter_direct_reports([report], idle, self.direct), [report])

            artifact.write_text("episode completed")
            os.utime(artifact, (now - 60, now - 60))
            active = metadata()
            self.assertEqual(active[0]["last_write_minutes"], 1)
            self.assertFalse(active[0]["idle_10m"])
            self.assertEqual(amb_speed.filter_direct_reports([report], active, self.direct), [])
            self.assertEqual(amb_speed.filter_direct_reports([report], idle, self.direct), [])

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


def test_leader_first_dispatch_measures_from_execution_start(tmp_path, monkeypatch):
    monkeypatch.setenv("AMB_ROOT", str(tmp_path))
    leader = {"id": "J-lead", "mode": "lead", "tier": "L1"}
    children = [
        ({"id": "J-late", "lead_parent": "J-lead"}, "exec", "1970-01-01T00:02:00Z"),
        ({"id": "J-early", "lead_parent": "J-lead"}, "done", "1970-01-01T00:01:30Z"),
    ]
    for job, state, time in [(leader, "exec", "1970-01-01T00:00:30Z"), *children]:
        amb.write_json(amb.jobs_dir(job["id"], "job.json"), job)
        amb.write_json(amb.jobs_dir(job["id"], "state.json"), {
            "state": state, "started_at": time, "dispatched_at": time,
        })
    active, _ = amb_speed.amb_metadata(amb, {}, 600)
    row = next(item for item in active if item["id"] == "J-lead")
    assert row["lead_first_dispatch_seconds"] == 60
    assert row["lead_started_seconds_ago"] == 570
    assert row["lead_children_active"] == 1
