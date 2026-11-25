"""与人类对齐（<REDACTED_ID> 子任务）：边规划边派、思路和提示词同时发给用户、按审阅意见纠正某一步。

覆盖 alignment_design.md 第 5 节：首个子任务在完整规划结束前启动；思路与提示词同步发给用户；
原地重来（redo）；话传给谁（send / --all）；子单结果回流带队收件箱和 amb wait；
规划单单个子任务取消不卡父单、有依赖时拒绝；带队规矩按类型附加；开关关掉等于现状。
"""
import json
import re
import shlex
import tempfile
import time
from pathlib import Path
from unittest import mock

import amb
from tests.test_t2 import FAKES, JOB_ID, pid_alive
from tests.test_t5 import T5Base


class AlignBase(T5Base):
    def setUp(self):
        super().setUp()
        self.sent = self.root / "feishu-sent.txt"
        feishu = self.script("feishu.sh", f'printf "%s\\n=====\\n" "$1" >> {shlex.quote(str(self.sent))}\n')
        self.config["feishu"] = {"cmd": f"{shlex.quote(str(feishu))} {{text}} {{event_id}}", "enabled": False}
        self.save_config()

    def gated(self, name, gate):
        """执行者：等 gate 文件出现才按 exec_ok 交卷。"""
        return self.script(name, f"""
while ! test -f {shlex.quote(str(gate))}; do sleep 0.05; done
exec {shlex.quote(str(FAKES / 'exec_ok.sh'))} "$@"
""")

    def until(self, predicate, timeout=20, what="condition"):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.1)


class TestPlanWhileExec(AlignBase):
    def setUp(self):
        super().setUp()
        self.comments = self.root / "linear-comments.txt"
        linear = self.script("linear.sh", f"""
test "$1" = comment
test "$2" = <REDACTED_ID>
test "$4" = --once
printf '%s\\n=====\\n' "$3" >> {shlex.quote(str(self.comments))}
""")
        self.config["linear"] = {"app_cmd": str(linear), "dry_run": False}
        self.save_config()

    def assert_dispatch_delivery(self, child, step, summary, heading, prompt):
        title = f"派出 {child} · {step}（"
        expected = f"{summary}\n\n## {heading}\n{prompt}"

        def feishu_body():
            if not self.sent.exists():
                return ""
            messages = self.sent.read_text().split("\n=====\n")
            return "".join(message.split("\n\n", 1)[1] for message in messages if message.startswith(title))

        self.until(lambda: feishu_body() == expected, what="飞书收到提示词全文")
        self.until(self.comments.exists, what="Linear 收到父卡评论")
        visible = re.sub(r"\n\n<!-- [0-9a-f]{32}-1 -->", "", self.comments.read_text())
        self.assertEqual(visible, f"**已派出一步**\n\n这一步：{step}\n\n{summary}\n\n"
                                  "这一步的完整提示词已发到飞书\n=====\n")

    def test_first_child_runs_before_planning_ends_and_idea_plus_prompt_reach_user(self):
        release = self.root / "release"
        planner = self.script("slow_plan.sh", f"""
while ! test -f {shlex.quote(str(release))}; do sleep 0.05; done
cp {shlex.quote(str(release))} "$3"
""")
        self.set_role("plan.opus", planner)
        self.submit(tier="L3", linear="<REDACTED_ID>")
        self.tick_until(lambda s: s["state"] == "planning")
        first = {"title": "取证基线", "checklist": ["列出三台机器的磁盘"]}
        graph = {"thinking": "目标：查清磁盘\n怎么拆：先取证再汇总\n关键判断：只读\n不可逆的事：没有", "subs": [first]}
        graph_file = self.root / "graph.json"
        graph_file.write_text(json.dumps(graph, ensure_ascii=False))
        self.amb("plan-update", JOB_ID, "1", str(graph_file))
        child, = self.state()["subs"]
        # 规划者还没交卷，第一步已经在跑
        self.assertEqual(self.state()["state"], "planning")
        self.assertIsNone(self.state()["run"].get("collected"))
        self.assertEqual(self.state(child)["state"], "exec")
        first_exec = next(h["ts"] for h in self.state(child)["history"] if h["to"] == "exec")
        # 思路和这一步的执行提示词同时发给用户，没有等规划结束
        prompt = (self.jobs / child / "attempts/1/exec/prompt.md").read_text()
        self.assertIn("列出三台机器的磁盘", prompt)
        self.assert_dispatch_delivery(child, first["title"], graph["thinking"], "执行提示词", prompt)
        self.assertEqual(self.state()["state"], "planning")
        release.write_text(json.dumps(graph, ensure_ascii=False))
        st = self.run_to_end(timeout=40)
        self.assertEqual(st["state"], "done", st)
        plan_done = next(h["ts"] for h in st["history"] if h["to"] == "subs")
        self.assertLess(first_exec, plan_done)

    def test_lead_child_dispatch_sends_thinking_and_prompt_and_reports_back(self):
        self.set_role("exec.fable", self.script("claude", "sleep 30\n"))
        gate = self.root / "child-gate"
        self.set_role("exec.sol", self.gated("child.sh", gate))
        self.submit(exec="fable", tier="L1", linear="<REDACTED_ID>")
        self.tick_until(lambda s: s["state"] == "exec")
        lead_dir = self.jobs / JOB_ID / "attempts/1/exec"
        prompt = (lead_dir / "prompt.md").read_text()
        self.assertIn("用 inbox", prompt)
        self.assertIn("child_action", prompt)

        child = self.amb("new", "--kind", "code", "--repo", str(self.repo), "--lead", JOB_ID).stdout.strip()
        draft = self.jobs / "_draft" / child
        job = json.loads((draft / "job.json").read_text())
        job.update(title="查磁盘", user_words="查三台机器的磁盘")
        (draft / "job.json").write_text(json.dumps(job, ensure_ascii=False))
        (draft / "brief.md").write_text("第 1 步提示词：只读查看三台机器 df -h\n")
        note = self.root / "thinking.md"
        note.write_text("目标：磁盘汇总\n怎么拆：先查再汇总\n关键判断：只读\n不可逆的事：没有\n")
        self.amb("submit", child, "--thinking", str(note))
        # 提交即启动，同时思路+提示词发给用户
        self.tick_until(lambda s: s["state"] == "exec", job_id=child, timeout=10)
        brief = (self.jobs / child / "brief.md").read_text()
        self.assertIn("第 1 步提示词：只读查看三台机器 df -h", brief)
        self.assert_dispatch_delivery(child, job["title"], note.read_text().strip(), "派单提示词", brief)

        # 发给带队单的话只进带队收件箱；--all 才广播到各步
        child_inbox = self.jobs / child / "attempts/1/exec/inbox"
        self.amb("send", JOB_ID, "第 1 步别动线上")
        self.assertIn("第 1 步别动线上", (lead_dir / "inbox").read_text())
        self.assertEqual(child_inbox.read_text(), "")
        self.amb("send", JOB_ID, "所有步骤都只读", "--all")
        self.assertIn("所有步骤都只读", child_inbox.read_text())
        (lead_dir / "inbox.ack").write_text("2\n")

        # 子单结束 → 带队收件箱多一行，amb wait 立刻返回
        waited = self.amb("wait", JOB_ID, "--timeout", "1", "--interval", "0.1")
        self.assertIn("没有新消息", waited.stdout)
        gate.write_text("go")
        self.tick_until(lambda s: s["state"] == "done", job_id=child, timeout=30)
        lines = [json.loads(l) for l in (lead_dir / "inbox").read_text().splitlines() if l.strip()]
        self.assertEqual(lines[-1]["child"], child)
        self.assertIn("已结束：done", lines[-1]["message"])
        waited = self.amb("wait", JOB_ID, "--timeout", "5", "--interval", "0.1")
        self.assertIn("未确认的第 3-3 条", waited.stdout)
        self.amb("cancel", JOB_ID)


class TestRedo(AlignBase):
    def test_redo_stops_step_and_restarts_same_job_with_reason(self):
        gate = self.root / "gate"
        self.set_role("exec.sol", self.gated("slow.sh", gate))
        self.submit()
        st = self.tick_until(lambda s: s["state"] == "exec")
        old_pid = int((self.jobs / JOB_ID / "attempts/1/exec/pid").read_text().split()[0]) \
            if (self.jobs / JOB_ID / "attempts/1/exec/pid").exists() else None
        old_wt = st["wt"]
        self.amb("redo", JOB_ID, "--reason", "第 1 步改成只读，别动线上")
        st = self.state()
        self.assertEqual(st["attempt"], 2)
        self.assertEqual(st["exec"], "sol")
        self.assertEqual(st["redo"]["prev_wt"], old_wt)
        self.assertIn("user_redo", [r["reason"] for r in st["rework"]])
        self.assertEqual(st["state"], "exec")  # redo 后立刻起下一轮
        prompt = (self.jobs / JOB_ID / "attempts/2/exec/prompt.md").read_text()
        self.assertIn("第 1 步改成只读，别动线上", prompt)
        self.assertIn("原地重来", prompt)
        self.assertIn(old_wt, prompt)
        if old_pid:
            self.until(lambda: not pid_alive(old_pid), what="旧执行进程退出")
        self.assertEqual(sorted(p.name for p in self.jobs.iterdir() if p.name.startswith("J-")), [JOB_ID])
        gate.write_text("go")
        st = self.run_to_end(timeout=40)
        self.assertEqual(st["state"], "done", st)
        self.amb("redo", JOB_ID, "--reason", "x", expected=2)  # 已结束只能 reopen


class TestCancelOneStep(AlignBase):
    def plan(self, subs):
        body = json.dumps({"subs": subs}, ensure_ascii=False)
        self.set_role("plan.opus", self.script("plan.sh", f"cat > \"$3\" <<'PLAN'\n{body}\nPLAN\n"))

    def test_cancel_independent_child_lets_parent_finish(self):
        gate = self.root / "gate"
        slow = self.script("maybe_slow.sh", f"""
if grep -q 慢的那步 "$2"; then while ! test -f {shlex.quote(str(gate))}; do sleep 0.05; done; fi
exec {shlex.quote(str(FAKES / 'exec_ok.sh'))} "$@"
""")
        self.set_role("exec.sol", slow)
        self.plan([{"title": "快的那步", "checklist": ["a"]}, {"title": "慢的那步", "checklist": ["b"]}])
        self.submit(tier="L3", brief="分两步做：" + "取证" * 30)
        st = self.tick_until(lambda s: len(s.get("subs") or []) == 2)
        fast, slow_id = st["subs"]
        self.tick_until(lambda s: s["state"] == "exec", job_id=slow_id)
        self.amb("cancel", slow_id)
        st = self.run_to_end(timeout=40)
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["skipped_subs"], [slow_id])
        self.assertEqual(self.state(fast)["state"], "done")

    def test_cancel_child_with_dependents_is_refused(self):
        gate = self.root / "gate"
        self.set_role("exec.sol", self.gated("slow.sh", gate))
        self.plan([{"title": "基线", "checklist": ["a"]}, {"title": "汇总", "checklist": ["b"], "depends_on": [0]}])
        self.submit(tier="L3", brief="分两步做：" + "取证" * 30)
        st = self.tick_until(lambda s: len(s.get("subs") or []) == 2)
        first, _ = st["subs"]
        result = self.amb("cancel", first, expected=2)
        self.assertIn("amb redo", result.stderr)
        self.assertNotEqual(self.state(first)["state"], "cancelled")
        self.amb("cancel", JOB_ID)


def test_lead_rules_attach_by_job_type_not_model_name():
    cfg = {}
    assert amb.is_leader({"id": "J"}, {"exec": "fable"}, cfg)
    assert not amb.is_leader({"id": "J.a1s01", "parent": "J", "plan_attempt": 1}, {"exec": "fable"}, cfg)
    assert not amb.is_leader({"id": "J-2", "lead_parent": "J"}, {"exec": "fable"}, cfg)
    assert amb.is_leader({"id": "J", "mode": "lead"}, {"exec": "opus"}, cfg)
    assert not amb.is_leader({"id": "J"}, {"exec": "opus"}, cfg)
    assert not amb.is_leader({"id": "J", "mode": "lead"}, {"exec": "fable"}, {"lead_mode": False})


def test_switches_off_mean_no_review_messages():
    with tempfile.TemporaryDirectory() as root, mock.patch.dict("os.environ", {"AMB_ROOT": root}):
        job = {"id": JOB_ID, "linear": ""}
        with mock.patch.object(amb, "launch_background") as bg:
            amb.queue_alignment(job, {"feishu": {"cmd": "fake"}, "review_events": False}, "派出", "思路")
            assert not Path(root, "jobs", "_alignment").exists()
            bg.assert_not_called()
            amb.queue_alignment(job, {"feishu": {"cmd": "fake"}}, "派出", "思路")
            assert list(Path(root, "jobs", "_alignment", "pending").glob("*.json"))


def test_legacy_pending_dispatch_retries_only_the_failed_channel(tmp_path):
    entry = {"title": "派出 <REDACTED_ID> · 查磁盘", "card": "<REDACTED_ID>",
             "body": "先取证再汇总\n\n## 执行提示词\n" + "read df -h\n" * 200}
    chunks = amb.alignment_chunks(entry["body"])
    path = tmp_path / "receipt.json"
    feishu = mock.Mock(return_value=True)
    linear = mock.Mock(side_effect=[False, True])
    assert amb.deliver_alignment(path, entry, chunks, "feishu", feishu, True)
    assert not amb.deliver_alignment(path, entry, chunks, "linear", linear, True)
    assert amb.deliver_alignment(path, entry, chunks, "feishu", feishu, True)
    assert amb.deliver_alignment(path, entry, chunks, "linear", linear, True)
    assert feishu.call_count == len(chunks)
    assert linear.call_count == 2
    assert linear.call_args_list[0] == linear.call_args_list[1]
    assert linear.call_args.args == (
        "**已派出一步**\n\n这一步：查磁盘\n\n先取证再汇总\n\n"
        "<!-- receipt-1 -->\n\n这一步的完整提示词已发到飞书",
        "receipt-1",
    )


def _flush_dispatch(root, feishu_result, *, feishu_cmd="fake"):
    """派一单后跑一次 flush，返回 (飞书调用列表, Linear 评论列表)。"""
    cfg = {"feishu": {"cmd": feishu_cmd} if feishu_cmd else {}, "linear": {"dry_run": False}}
    job = {"id": JOB_ID, "linear": "<REDACTED_ID>"}
    feishu_calls, comments = [], []

    def send_feishu(message, fs, key, deadline):
        feishu_calls.append(message)
        return feishu_result(len(feishu_calls))

    def post_linear(cfg, verb, ident, text, once):
        comments.append(text)
        return ""

    with (mock.patch.dict("os.environ", {"AMB_ROOT": root}),
          mock.patch.object(amb, "launch_background"),
          mock.patch.object(amb, "send_feishu_text", side_effect=send_feishu),
          mock.patch.object(amb, "linear_app", side_effect=post_linear)):
        amb.queue_dispatch_alignment(job, cfg, JOB_ID + ".a1s01", "查磁盘", "先取证再汇总",
                                     "执行提示词", "read df -h", key="k1")
        amb.flush_alignment(cfg)
        amb.flush_alignment(cfg)
    return feishu_calls, comments


def test_card_comment_says_sent_only_after_feishu_receipt():
    with tempfile.TemporaryDirectory() as root:
        feishu_calls, comments = _flush_dispatch(root, lambda n: True)
    assert len(feishu_calls) == 1 and len(comments) == 1
    assert comments[0].endswith("这一步的完整提示词已发到飞书")
    assert "read df -h" not in comments[0]


def test_card_comment_tells_truth_without_prompt_when_feishu_fails():
    with tempfile.TemporaryDirectory() as root:
        # 第一次飞书失败、第二次重试成功；卡片评论只发一次，按第一次真实回执写。
        feishu_calls, comments = _flush_dispatch(root, lambda n: n > 1)
        assert list(Path(root, "jobs", "_alignment", "sent").glob("*.json"))
    assert len(feishu_calls) == 2 and len(comments) == 1
    assert "已发到飞书" not in comments[0]
    assert comments[0].endswith("完整提示词这次没发到飞书，留在任务记录里")
    assert "## 执行提示词" not in comments[0] and "read df -h" not in comments[0]


def test_card_comment_when_feishu_is_off_omits_prompt():
    with tempfile.TemporaryDirectory() as root:
        feishu_calls, comments = _flush_dispatch(root, lambda n: True, feishu_cmd="")
    assert feishu_calls == [] and len(comments) == 1
    assert "已发到飞书" not in comments[0]
    assert comments[0].endswith("完整提示词这次没发到飞书，留在任务记录里")
    assert "## 执行提示词" not in comments[0] and "read df -h" not in comments[0]


def test_legacy_linear_retry_reads_feishu_receipt_file(tmp_path):
    entry = {"title": "派出 <REDACTED_ID> · 查磁盘", "card": "<REDACTED_ID>",
             "body": "先取证再汇总\n\n## 执行提示词\nread df -h"}
    chunks = amb.alignment_chunks(entry["body"])
    linear = mock.Mock(return_value=True)
    assert amb.deliver_alignment(tmp_path / "r.json", entry, chunks, "linear", linear, True)
    assert "已发到飞书" not in linear.call_args.args[0]


def test_retried_leader_prompt_lists_every_delegated_job(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv("AMB_ROOT", str(tmp_path))
    leader = amb.skeleton("J-test", kind="doc", words="继续汇总")
    leader["exec"] = "fable"
    for child, state in (("J-first", "done"), ("J-second", "exec")):
        amb.atomic_write(amb.jobs_dir("J-test", "led", child), "")
        amb.write_json(amb.jobs_dir(child, "job.json"), {"title": child + " 的目标"})
        amb.write_json(amb.jobs_dir(child, "state.json"), {"state": state})
    prompt = amb.exec_prompt(leader, {"attempt": 2, "exec": "opus"}, {}, SimpleNamespace(cfg={}), "", "/work")
    assert "J-first「J-first 的目标」：done" in prompt
    assert "J-second「J-second 的目标」：exec" in prompt
    assert "避免重复派活" in prompt


def test_leader_can_finish_and_reports_unfinished_delegations(tmp_path, monkeypatch):
    monkeypatch.setenv("AMB_ROOT", str(tmp_path))
    jid = "J-lead"
    job = amb.skeleton(jid, kind="doc", words="汇总")
    job.update(mode="lead", reopened_from="J-prev")
    deadline = "2026-11-23T17:00:00+00:00"
    for parent, child, status in ((jid, "J-active", "exec"), ("J-prev", "J-queued", "queued"),
                                  ("J-prev", "J-done", "done")):
        amb.atomic_write(amb.jobs_dir(parent, "led", child), "")
        amb.write_json(amb.jobs_dir(child, "job.json"), {"title": child, "host": "local", "deadline": deadline})
        amb.write_json(amb.jobs_dir(child, "state.json"), {"state": status, "open_items": []})
    directory = str(tmp_path / "exec")
    manual = {"what": "留待人工确认", "eta": deadline, "owner": "Ambition"}
    amb.atomic_write(directory + "/exit", "0")
    amb.atomic_write(directory + "/out", json.dumps({"summary": "已提交", "needs_plan": False,
                                                    "goal_met": True, "open_items": [manual]}))
    state = amb.initial_state(job)
    state.update(wt=directory, start_sha="start", sha="start", lead_baseline={})
    state["run"] = {"step": "exec", "local": directory, "collected": True, "exec_committed": True}
    host = mock.Mock()
    host.run.return_value = mock.Mock(returncode=0, stdout="{}")
    host.git.return_value = mock.Mock(returncode=0, stdout="start")
    with mock.patch.object(amb, "after_exec", side_effect=lambda *args: amb.tr(state, "done", "完成执行")):
        amb.finish_collected_step(jid, job, state, {}, host)

    assert state["state"] == "done"
    assert state["run"] is None
    assert state["open_items"][0] == manual
    assert len(state["open_items"]) == 3
    assert all(item["eta"] == deadline and item["owner"] for item in state["open_items"])
    result = amb.build_result(jid, job, state)
    assert result["open_items"] == state["open_items"]
    pending = amb.result_md(result).split("## 未收尾的带队子单")[1]
    assert "J-active" in pending and "J-queued" in pending and "J-done" not in pending
    assert not amb.card_verdict(job, state)
    activity = {"pending": {}, "resolved": []}
    amb.record_open_items(job, state, activity)
    assert [entry["item"] for entry in activity["pending"].values()] == [manual]
