from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import amb
import amb_metrics


class TestDispatchStart(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"AMB_ROOT": str(self.root)}))
        self.jid = "J-1120-99"
        self.job = amb.skeleton(self.jid, kind="ask", tier="L1", words="fixture")
        self.cfg = amb.load_config()
        self.cfg["hosts"]["local"]["work_root"] = str(self.root / "work")
        self.invocations = self.root / "invocations"
        script = (f"with open({str(self.invocations)!r}, 'a') as log:\n"
                  "    log.write('started\\n')\n"
                  'print(\'{"summary": "ok"}\')\n')
        self.cfg["roles"]["ask"] = {"cmd": shlex.join([sys.executable, "-c", script])}
        with patch.object(amb.subprocess, "Popen"):
            amb.submit_job(self.job, "fixture", self.cfg)

    def events(self):
        with (self.root / "stats.jsonl").open() as stream:
            return [row["event"] for row in amb_metrics.records(stream)]

    def test_queue_retry_at_boundary_only_once(self):
        _, state = amb.load(self.jid)
        due = amb.dispatch_start_due(self.job, state)
        with patch.object(amb, "start_attempt", side_effect=amb.HostDown):
            with patch.object(amb.time, "time", return_value=due - 0.001):
                amb.tick_job(self.jid, self.cfg)
            self.assertNotIn("dispatch_start_watchdog", self.events())
            with patch.object(amb.time, "time", return_value=due):
                amb.tick_job(self.jid, self.cfg)
                amb.tick_job(self.jid, self.cfg)
        self.assertEqual(self.events().count("dispatch_start_watchdog"), 1)
        _, state = amb.load(self.jid)
        self.assertEqual(state["attempt"], 1)
        self.assertEqual(state["dispatch_start_retry"]["delay_s"], 120)
        self.assertFalse(any(entry["from"] == entry["to"] for entry in state["history"]))

    def test_detached_watchdog_wakes_again_at_deadline(self):
        _, state = amb.load(self.jid)
        due = amb.dispatch_start_due(self.job, state)
        clock = Mock()
        clock.time.return_value = due - 120
        clock.sleep.side_effect = lambda _: setattr(clock.time, "return_value", due)
        with patch.object(amb, "time", clock), patch.object(amb, "start_attempt", side_effect=amb.HostDown) as start:
            amb.cmd_watch_dispatch_start(SimpleNamespace(id=self.jid), self.cfg)
        clock.sleep.assert_called_once_with(120)
        self.assertEqual(start.call_count, 2)
        self.assertEqual(self.events().count("dispatch_start_watchdog"), 1)

    def test_retry_recovers_a_saved_run_with_an_unowned_launch_claim(self):
        with patch.object(amb.Host, "launch", return_value=""):
            amb.tick_job(self.jid, self.cfg)
        _, state = amb.load(self.jid)
        step = Path(state["run"]["dir"])
        (step / "claim").touch()
        amb.tick_job(self.jid, self.cfg)
        self.assertFalse(self.invocations.exists())
        state["history"][0]["ts"] = datetime.fromtimestamp(
            time.time() - amb.DISPATCH_START_LIMIT_S, timezone.utc).isoformat()
        amb.save(self.jid, state)

        amb.tick_job(self.jid, self.cfg)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and amb.load(self.jid)[1]["state"] != "done":
            amb.tick_job(self.jid, self.cfg)
            time.sleep(0.02)

        _, state = amb.load(self.jid)
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["attempt"], 1)
        self.assertEqual(self.invocations.read_text().splitlines(), ["started"])
        self.assertEqual(self.events(), ["timing_dispatched", "dispatch_start_watchdog", "timing_started", "done"])

    def test_existing_start_evidence_prevents_redispatch_after_lost_ack(self):
        launch = amb.Host.launch

        def lose_ack(host, *args, **kwargs):
            launch(host, *args, **kwargs)
            raise amb.HostDown

        with patch.object(amb.Host, "launch", lose_ack):
            amb.tick_job(self.jid, self.cfg)
        _, state = amb.load(self.jid)
        state["history"][0]["ts"] = datetime.fromtimestamp(
            time.time() - amb.DISPATCH_START_LIMIT_S, timezone.utc).isoformat()
        amb.save(self.jid, state)
        with patch.object(amb.Host, "launch") as restart:
            amb.tick_job(self.jid, self.cfg)
            restart.assert_not_called()
        self.assertIn("timing_started", self.events())
        self.assertNotIn("dispatch_start_watchdog", self.events())
