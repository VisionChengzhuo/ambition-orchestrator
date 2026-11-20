"""Sentinel rules and their integration into genuine ticks with a local runner."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import amb
import amb_sentinel as sentinel
from tests.test_t2 import JOB_ID, T2Base, pid_alive, write_json


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class TestSentinel(T2Base):
    def setUp(self):
        super().setUp()
        self.config["sentinel"] = True
        self.save_config()

    def events(self):
        return sentinel.recent_events(self.root)

    def test_stalled_execution_is_detected_once_without_interrupting_runner(self):
        executor = self.script("stalls.sh", "echo began\nsleep 30\n")
        self.set_role("exec.sol", executor)
        self.submit()
        self.tick()
        st = self.state()
        run_dir = Path(st["run"]["dir"])
        pid = int((run_dir / "pid").read_text().split()[0])
        log = run_dir / "stdout"
        for _ in range(100):
            if log.exists() and log.stat().st_size:
                break
            time.sleep(0.02)
        self.assertEqual(log.read_text().strip(), "began")

        old = time.time() - 610
        st["run"]["started"] = old
        write_json(self.jobs / JOB_ID / "state.json", st)
        check = self.tick()
        self.assertEqual(check.stdout, "")
        self.assertEqual(check.stderr, "")
        self.assertEqual(self.events(), [], "recently growing output should be silent")

        # Simulate a pre-upgrade runner without progress telemetry. Poll fallback
        # must find the stale file while the original runner is still alive.
        runner = run_dir / "runner.py"
        script = runner.read_text()
        runner.write_text(script.replace(',\n                      "progress": output_progress(d)', ''))
        # Clear staging-only telemetry to model a real upgrade from the old runner.
        (self.jobs / "_sentinel" / "progress" / (JOB_ID + ".json")).unlink()
        os.utime(log, (old, old))
        self.tick()
        self.tick()
        [event] = self.events()
        self.assertEqual(event["event_id"], f"sentinel:{JOB_ID}:exec")
        self.assertEqual(event["action"], "needs_decision")
        self.assertGreaterEqual(event["age_s"], 610)
        self.assertEqual(self.state()["state"], "exec")
        self.assertTrue(pid_alive(pid))
        self.amb("cancel", JOB_ID)

    def test_touching_output_without_growth_does_not_reset_stall(self):
        now = time.time()
        run = {"step": "exec", "started": now - 601}
        silent = {"progress": {"stdout": [100, now - 599]}}
        sentinel.observe_run(self.root, JOB_ID, {}, run, silent, None, now)
        self.assertEqual(self.events(), [])
        touched = {"progress": {"stdout": [100, now]}}
        sentinel.observe_run(self.root, JOB_ID, {}, run, touched, None, now + 2)
        self.assertEqual([event["phase"] for event in self.events()], ["exec"])

    def test_ready_queue_is_repaired_in_same_tick(self):
        self.submit()
        st = self.state()
        st["history"][-1]["ts"] = iso(time.time() - 121)
        write_json(self.jobs / JOB_ID / "state.json", st)
        self.tick()
        [event] = self.events()
        self.assertEqual((event["phase"], event["action"]), ("queue", "tick"))
        self.assertNotEqual(self.state()["state"], "queued")

    def test_dependency_waiting_and_due_thresholds(self):
        now = int(time.time())
        dependency = self.jobs / "prerequisite"
        dependency.mkdir(parents=True)
        queue = {"state": "queued", "history": [{"ts": iso(now - 200)}]}
        job = {"parent": "parent", "depends_on": [dependency.name]}
        write_json(dependency / "state.json", queue)
        sentinel.observe_state(self.root, JOB_ID, job, queue, now)
        self.assertEqual(self.events(), [])
        write_json(dependency / "state.json", {"state": "done", "history": [{"ts": iso(now - 120)}]})
        sentinel.observe_state(self.root, JOB_ID, job, queue, now)
        self.assertEqual(self.events(), [])
        sentinel.observe_state(self.root, JOB_ID, job, queue, now + 1)
        [event] = self.events()
        self.assertEqual(event["action"], "start_ready")

    def test_plan_review_and_human_wait_thresholds(self):
        clock = int(time.time())
        for phase in ("plan", "review"):
            started = clock - sentinel.LIMITS[phase]
            run = {"step": phase, "started": started}
            sentinel.observe_run(self.root, JOB_ID, {}, run, {}, None, clock)
            self.assertNotIn(phase, [event["phase"] for event in self.events()])
            sentinel.observe_run(self.root, JOB_ID, {}, run, {}, None, clock + 1)
        blocked = {"state": "blocked", "history": [{"ts": iso(clock - 600)}], "needs_human": "decision"}
        sentinel.observe_state(self.root, JOB_ID, {}, blocked, clock)
        self.assertNotIn("human", [event["phase"] for event in self.events()])
        sentinel.observe_state(self.root, JOB_ID, {}, blocked, clock + 1)
        self.assertEqual({event["phase"] for event in self.events()}, {"plan", "review", "human"})

    def test_supervisor_outbox_append_deduplicates_retries(self):
        outbox = self.root / "outbox.jsonl"
        self.config["supervisor"] = {"enabled": True, "host": "local", "outbox": str(outbox)}
        entry = {"ts": iso(time.time()), "task": JOB_ID, "action": "needs_decision",
                 "event_id": f"sentinel:{JOB_ID}:review", "reason": "建议"}
        sentinel.append_outbox(amb, self.config, entry)
        sentinel.append_outbox(amb, self.config, {**entry, "reason": "duplicate request"})
        self.assertEqual([json.loads(line) for line in outbox.read_text().splitlines()], [entry])
