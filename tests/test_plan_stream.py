"""Live planner, delegated results, and campaign routing."""
import json
import shlex
import tempfile
import time
from pathlib import Path
from unittest import mock

import amb
import amb_sentinel
from tests.test_t5 import T5Base
from tests.test_t2 import JOB_ID


class TestLivePlanner(T5Base):
    def test_replan_publishes_new_graph_and_uses_new_worktree(self):
        plans = [
            {"thinking": "当前轮的计划", "subs": [
                {"title": f"{label} first", "checklist": ["验证结果"]},
                {"title": f"{label} second", "checklist": ["验证结果"], "depends_on": [0]},
            ]}
            for label in ("original", "replacement")
        ]
        planner = self.script("replan.sh", f"""
case "$2" in
  */a1/plan/*|*/attempts/1/plan/*) printf '%s' {shlex.quote(json.dumps(plans[0]))} > "$3" ;;
  *) printf '%s' {shlex.quote(json.dumps(plans[1]))} > "$3" ;;
esac
""")
        review = self.script("review_by_attempt.sh", """
case "$2" in
  */a1/review*/*|*/attempts/1/review*/*) exit 1 ;;
esac
sha_line=$(grep -m 1 '^SHA: ' "$2")
printf '{"verdict":"PASS","sha":"%s","findings":[]}\n' "${sha_line#SHA: }" > "$3"
""")
        self.set_role("plan.opus", planner)
        self.set_role("review.codex", review)
        self.set_role("review.opus", review)
        self.submit(tier="L3")
        st = self.run_to_end(timeout=40)
        self.assertEqual((st["state"], st["attempt"]), ("done", 2), st)
        self.assertEqual(st["stream_snapshot"]["subs"], plans[1]["subs"])
        self.assertEqual(st["subs"], [f"{JOB_ID}.a2s01", f"{JOB_ID}.a2s02"])
        self.assertEqual(self.state(f"{JOB_ID}.a1s01")["state"], "done")
        self.assertEqual(Path(st["wt"]).parent.name, "a2")

    def test_child_starts_before_plan_finishes_and_inbox_can_redirect(self):
        release = self.root / "release"
        planner = self.script("wait_plan.sh", f"""
while ! test -f {shlex.quote(str(release))}; do sleep 0.05; done
cp {shlex.quote(str(release))} "$3"
""")
        self.set_role("plan.opus", planner)
        self.submit(tier="L3")
        st = self.tick_until(lambda state: state["state"] == "planning")
        directory = Path(st["run"]["dir"])
        self.assertIn("开工 5 分钟", (directory / "prompt.md").read_text())
        self.assertIn("inbox.ack", (directory / "prompt.md").read_text())

        graph_file = self.root / "graph.json"
        first = {"title": "基线", "checklist": ["验证结果"]}
        graph = {"thinking": "先跑基线", "subs": [first]}
        graph_file.write_text(json.dumps(graph))
        self.amb("plan-update", JOB_ID, "1", str(graph_file))
        child = self.state()["subs"][0]
        self.assertEqual(self.state()["state"], "planning")
        self.assertEqual(self.state(child)["state"], "exec")
        self.amb("plan-update", JOB_ID, "1", str(graph_file))
        self.assertEqual(self.state()["subs"], [child])

        self.amb("send", JOB_ID, "改做第二个方案")
        self.assertEqual(json.loads((directory / "inbox").read_text())["message"], "改做第二个方案")
        result = self.amb("plan-update", JOB_ID, "1", str(graph_file), expected=2)
        self.assertIn("先处理追加指令", result.stderr)
        (directory / "inbox.ack").write_text("1\n")

        graph = {"thinking": "按你的追加改做第二个方案", "drop": [0],
                 "subs": [first, {"title": "新方案", "checklist": ["验证新方案"]}]}
        graph_file.write_text(json.dumps(graph))
        self.amb("plan-update", JOB_ID, "1", str(graph_file))
        old, new = self.state()["subs"]
        self.assertEqual(self.state(old)["state"], "cancelled")
        self.assertEqual(self.state(new)["state"], "exec")
        self.assertEqual(self.state()["plan_order"], [new])

        release.write_text(json.dumps(graph))
        st = self.run_to_end(timeout=25)
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["plan_order"], [new])
        self.assertEqual(self.state(new)["state"], "done")

    def test_conflicting_final_plan_preserves_published_children(self):
        release = self.root / "release"
        planner = self.script("finish_plan.sh", f"""
while ! test -f {shlex.quote(str(release))}; do sleep 0.05; done
cp {shlex.quote(str(release))} "$3"
""")
        self.set_role("plan.opus", planner)
        self.submit(tier="L3")
        self.tick_until(lambda state: state["state"] == "planning")

        graph = {"thinking": "先派任务", "subs": [{"title": "取证", "checklist": ["验证结果"]}]}
        graph_file = self.root / "graph.json"
        graph_file.write_text(json.dumps(graph))
        self.amb("plan-update", JOB_ID, "1", str(graph_file))
        child, = self.state()["subs"]

        changed = {**graph, "subs": [{"title": "取证", "checklist": ["验证最终结果"]}]}
        graph_file.write_text(json.dumps(changed))
        self.amb("plan-update", JOB_ID, "1", str(graph_file), expected=2)
        graph_file.replace(release)

        st = self.run_to_end(timeout=25)
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["plan_order"], [child])
        self.assertEqual(self.state(child)["state"], "done")
        attempt = self.jobs / JOB_ID / "attempts/1"
        self.assertEqual(json.loads((attempt / "plan.json").read_text())["subs"], graph["subs"])
        self.assertEqual(json.loads((attempt / "rejected-final-plan.json").read_text())["subs"], changed["subs"])

    def test_file_publication_wakes_local_dispatch_without_cron(self):
        self.config["runner_wake"] = True
        self.set_role("plan.opus", self.script("wait_plan.sh", "sleep 25\n"))
        self.submit(tier="L3")
        st = self.tick_until(lambda state: state["state"] == "planning")
        directory = Path(st["run"]["dir"])
        graph = {"thinking": "先取证", "subs": [{"title": "取证", "checklist": ["输出证据"]}]}
        temporary = directory / "dispatch.tmp"
        temporary.write_text(json.dumps(graph))
        temporary.replace(directory / "dispatch.json")
        deadline = time.monotonic() + 8
        while not self.state().get("subs") and time.monotonic() < deadline:
            time.sleep(0.05)
        child, = self.state()["subs"]
        while self.state(child)["state"] == "queued" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn(self.state(child)["state"], ("exec", "done"))
        self.assertEqual(self.state()["state"], "planning")
        self.amb("cancel", JOB_ID)

    def test_fable_child_is_linked_and_fanned_out(self):
        scripted = self.script("claude", "sleep 15\n")
        self.set_role("exec.fable", scripted)
        self.submit(exec="fable", tier="L1", linear="TCZ-100")
        self.tick_until(lambda state: state["state"] == "exec")
        self.assertIn("mcp__amb_router__dispatch", (self.jobs / JOB_ID / "attempts/1/exec/prompt.md").read_text())

        child = self.amb("new", "--kind", "code", "--repo", str(self.repo), "--lead", JOB_ID).stdout.strip()
        draft = self.jobs / "_draft" / child / "job.json"
        job = json.loads(draft.read_text())
        self.assertEqual(job["linear_parent"], "TCZ-100")
        job.update(title="核查", user_words="原始目标")
        draft.write_text(json.dumps(job))
        self.amb("submit", child, expected=2)
        note = self.root / "thinking.md"
        note.write_text("先核查关键结果")
        self.amb("submit", child, "--thinking", str(note))
        self.assertTrue((self.jobs / JOB_ID / "led" / child).is_file())
        self.amb("send", JOB_ID, "新要求")
        self.amb("cancel", JOB_ID)
        self.assertEqual(self.state(child)["state"], "cancelled")
        result_path = self.jobs / JOB_ID / "result.json"
        self.assertEqual(json.loads(result_path.read_text())["led"][child]["state"], "cancelled")


def test_alignment_retries_only_failed_channel_and_preserves_text():
    with tempfile.TemporaryDirectory() as root, mock.patch.dict("os.environ", {"AMB_ROOT": root}):
        cfg = {"feishu": {"cmd": "fake"}, "linear": {"dry_run": False}}
        job = {"id": JOB_ID, "linear": "TCZ-100"}
        content = "要保留原文 {event_id} 和换行\n" * 320
        feishu_calls, linear_calls = [], []

        def send_feishu(message, cfg, key, deadline):
            feishu_calls.append((message, key))
            return True

        def post_linear(cfg, verb, ident, text, once):
            assert (verb, ident, once) == ("comment", "TCZ-100", "--once")
            linear_calls.append(text)
            return None if len(linear_calls) == 2 else ""

        with (mock.patch.object(amb, "launch_background"),
              mock.patch.object(amb, "send_feishu_text", side_effect=send_feishu),
              mock.patch.object(amb, "linear_app", side_effect=post_linear)):
            amb.queue_alignment(job, cfg, "计划思路", content)
            amb.flush_alignment(cfg)
            amb.flush_alignment(cfg)
            amb.queue_alignment(job, cfg, "计划思路", content)
            amb.flush_alignment(cfg)
        assert len(feishu_calls) == len(amb.alignment_chunks(content))
        assert "".join(message.split("\n\n", 1)[1] for message, _ in feishu_calls) == content
        assert linear_calls[1] == linear_calls[2]
        assert len({key for _, key in feishu_calls}) == len(feishu_calls)


def test_campaign_terminal_and_watchdog_stay_in_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("AMB_ROOT", str(tmp_path / "amb"))
    cfg = {"autoresearch_root": str(tmp_path / "campaigns"), "notifications_since": "2099-01-01T00:00:00Z"}
    job = {"id": JOB_ID, "campaign": "study"}
    state = {"state": "done"}
    with mock.patch.object(amb.amb_events, "emit") as emit:
        amb.webhook_send(JOB_ID, job, state, cfg, "result.md", "结束")
        amb_sentinel.record(amb.root(), JOB_ID, job, "queue", 1000, 20, "排队告警")
        amb.route_campaign_sentinel(cfg, job, ("queue",))
        amb.route_campaign_sentinel(cfg, job, ("queue",))
        amb.card_event(job, "card-stale", "卡片进度告警", cfg)
        emit.assert_not_called()
    events = [json.loads(line) for line in Path(amb.campaign_events_path(cfg, "study")).read_text().splitlines()]
    assert [event["state"] for event in events] == ["done", "alert", "alert"]
    assert not amb_sentinel.recent_events(amb.root())
