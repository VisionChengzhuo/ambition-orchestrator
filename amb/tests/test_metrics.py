"""Measure completed timing samples and deduplicate repeated exports."""
import json
import tempfile
import unittest
from pathlib import Path

import amb_metrics as metrics


class TestMetrics(unittest.TestCase):
    def test_medians_rework_and_corrections(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            inputs = root / "inputs"
            base = "2026-11-20T00:"
            now = metrics.timestamp(base + "20:00Z")

            def entry(source, jid, event, **fields):
                metrics.append(root, {"source": source, "id": jid, "event": event, **fields})

            entry("amb", "J-test", "done", asked_at=base + "00:00Z", finished_at=base + "03:00Z", attempt=1)
            entry("amb", "J-test", "delivered", delivered_at=base + "04:00Z")
            entry("amb", "J-test", "reopen", new_id="J-testr1")
            entry("amb", "J-testr1", "done", finished_at=base + "16:00Z", attempt=1)
            entry("direct", "smoke", "done", asked_at=base + "00:00Z", finished_at=base + "05:00Z")
            entry("direct", "smoke", "delivered", delivered_at=base + "10:00Z")
            correction = {"id": "c1", "at": base + "03:00Z", "text": "synthetic"}
            for slot in ("20261120-08", "20261120-09"):
                directory = inputs / slot
                directory.mkdir(parents=True)
                (directory / "corrections.jsonl").write_text(json.dumps(correction) + "\n")
            result = metrics.snapshot(root, inputs, now=now)
            self.assertEqual(result["delivery_median_minutes"], 7)
            self.assertEqual(result["delivery_max_minutes"], 10)
            self.assertEqual(result["post_completion_median_minutes"], 3)
            self.assertEqual(result["rework_rate_pct"], 50)
            self.assertEqual(result["corrections_24h"], 1)
            recent = metrics.snapshot(root, inputs, now=now, since=metrics.timestamp(base + "06:00Z"))
            self.assertEqual(recent["delivery_median_minutes"], 10)
            self.assertEqual(recent["post_completion_median_minutes"], 5)
