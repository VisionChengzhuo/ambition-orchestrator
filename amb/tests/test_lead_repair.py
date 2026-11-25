"""<REDACTED_ID> a3：Router（带队单）收工检查覆盖修复路径。

用户原话（12:24）："我他妈不是让你只当 root 吗？Router 啊。怎么我看你还在自己做些事情？"
带队单在复核失败后的 repair 步骤同样是带队模型在跑，必须和 exec 一样过
"已派活 + 没动工作树" 这一关，不能因为走了 repair 分支就放行。
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AMB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AMB_DIR))
import amb  # noqa: E402

CFG = {
    "lead_mode": True,
    "defaults": {"ssh_timeout_s": 20, "step_minutes": 45},
    "hosts": {"local": {"ssh": None, "work_root": "~/amb-work"}},
    "roles": {"ask": {"cmd": "claude -p"},
              "exec.fable": {"model": "claude-fable-5-2",
                             "cmd": "claude -p --model claude-fable-5-2 --effort low"}},
}

SUMMARY = ('{"summary": "收工", "unverified": [], "open_items": [], '
           '"goal_met": true, "goal_gap": ""}')


class LeaderBreachTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.enterContext(self.root)
        self.enterContext(mock.patch.dict(os.environ, {"AMB_ROOT": self.root.name}))
        self.job = {"id": "J-LEAD", "mode": "lead", "kind": "doc", "exec": "fable",
                    "host": "local", "user_words": "派出去", "title": "带队",
                    "deadline": "2026-11-23T18:00:00+08:00", "accept": [], "checklist": []}
        os.makedirs(amb.jobs_dir("J-LEAD"), exist_ok=True)

    HEAD = "a" * 40

    def state(self, step):
        return {"attempt": 1, "attempts": {"1": {}}, "state": step, "history": [],
                "wt": "/wt", "lead_baseline": " M out.md\n", "repair_baseline": "",
                "start_sha": self.HEAD,
                "run": {"step": step, "dir": "/step", "local": "/step", "cwd": "/wt",
                        "argv": ["claude"], "stdin": "/step/prompt.md", "sha": self.HEAD}}

    def finish(self, step, status=" M out.md\n", state=None, head=None, texts=None, git_rc=0,
               subject="", paths="out.md\0", tree=None):
        state = copy.deepcopy(state) if state else self.state(step)
        head = head or self.HEAD

        def git(repo, *args, **kwargs):
            if git_rc == 128 or git_rc == args[0]:
                return mock.Mock(stdout="", stderr="fatal: not a git repository",
                                 returncode=128)
            if args[:1] == ("status",):
                return mock.Mock(stdout=status, returncode=0)
            if args[:2] == ("rev-parse", "HEAD"):
                return mock.Mock(stdout=head + "\n", returncode=0)
            if args[:2] == ("rev-parse", "HEAD^{tree}"):
                return mock.Mock(stdout=(tree or "") + "\n", returncode=0)
            if args[:3] == ("show", "-s", "--format=%P"):
                return mock.Mock(stdout=self.HEAD + "\n", returncode=0)
            if args[:3] == ("show", "-s", "--format=%s"):
                return mock.Mock(stdout=subject + "\n", returncode=0)
            if args[:1] == ("diff",):
                return mock.Mock(stdout=paths, returncode=0)
            return mock.Mock(stdout="", returncode=0)

        host = mock.Mock()
        host.git.side_effect = git
        host.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        host.get.return_value = ""
        texts = texts or {"stdout": SUMMARY, "out": SUMMARY, "stderr": "", "exit": "0"}
        with mock.patch.object(amb, "finish_repair") as repair, \
                mock.patch.object(amb, "after_exec", return_value="after"), \
                mock.patch.object(amb, "tr", lambda *a, **k: None):
            amb.finish_step("J-LEAD", self.job, state, CFG, host, texts)
        return repair

    def delegate(self, child="J-CHILD"):
        atomic = os.path.join(amb.jobs_dir("J-LEAD", "led"), child)
        os.makedirs(os.path.dirname(atomic), exist_ok=True)
        Path(atomic).write_text("")
        amb.write_json(amb.jobs_dir(child, "job.json"),
                       {"id": child, "title": "子单", "deadline": "2026-11-23T18:00:00+08:00"})
        amb.write_json(amb.jobs_dir(child, "state.json"), {"state": "exec"})

    def test_repair_without_delegation_is_rejected(self):
        with self.assertRaises(amb.StepError) as caught:
            self.finish("repair")
        self.assertEqual(caught.exception.sig, "lead:no_delegation")

    def test_repair_that_edits_worktree_is_rejected(self):
        self.delegate()
        with self.assertRaises(amb.StepError) as caught:
            self.finish("repair", status=" M out.md\n M src/a.py\n")
        self.assertEqual(caught.exception.sig, "lead:self_work")
        self.assertIn("src/a.py", caught.exception.detail)

    def test_repair_after_delegation_and_coordination_only_passes(self):
        self.delegate()
        self.assertTrue(self.finish("repair").called)

    def test_worker_repair_is_not_gated(self):
        self.job = {**self.job, "mode": "", "exec": "opus"}
        self.assertTrue(self.finish("repair", status=" M src/a.py\n").called)

    def test_exec_path_still_gated(self):
        with self.assertRaises(amb.StepError) as caught:
            self.finish("exec")
        self.assertEqual(caught.exception.sig, "lead:no_delegation")

    def test_repair_with_only_old_children_is_rejected(self):
        # 复核意见：修复轮不能把上一轮派出的旧子单算作本轮派活。
        self.delegate("J-OLD")
        state = self.state("repair")
        state["repair_children_before"] = ["J-OLD"]
        with self.assertRaises(amb.StepError) as caught:
            self.finish("repair", state=state)
        self.assertEqual(caught.exception.sig, "lead:no_delegation")
        self.assertIn("本轮", caught.exception.detail)

    def test_repair_with_new_child_passes(self):
        self.delegate("J-OLD")
        self.delegate("J-NEW")
        state = self.state("repair")
        state["repair_children_before"] = ["J-OLD"]
        self.assertTrue(self.finish("repair", state=state).called)

    def test_exec_with_clean_tree_but_moved_head_is_rejected(self):
        # 复核意见：带队单自己 commit 后 git status 是干净的，必须比对 HEAD。
        self.delegate()
        state = self.state("exec")
        state["lead_baseline"] = ""
        with self.assertRaises(amb.StepError) as caught:
            self.finish("exec", status="", head="b" * 40, state=state)
        self.assertEqual(caught.exception.sig, "lead:self_commit")
        self.assertIn("HEAD", caught.exception.detail)

    def test_repair_with_moved_head_is_rejected(self):
        self.delegate()
        with self.assertRaises(amb.StepError) as caught:
            self.finish("repair", head="b" * 40)
        self.assertEqual(caught.exception.sig, "lead:self_commit")

    def test_exec_with_unchanged_head_passes(self):
        self.delegate()
        state = self.state("exec")
        state["lead_baseline"] = ""
        self.assertFalse(self.finish("exec", status="", state=state).called)

    def test_orchestrator_commit_is_not_charged_to_leader(self):
        # 编排器在收工后自己提交时（崩溃重收），HEAD 变化不算带队单违规。
        self.delegate()
        state = self.state("exec")
        state["lead_baseline"] = ""
        state["run"]["exec_committed"] = True
        state["sha"] = "b" * 40
        self.finish("exec", status="", head=state["sha"], state=state)
        with self.assertRaises(amb.StepError) as caught:
            self.finish("exec", status="", head="c" * 40, state=state)
        self.assertEqual(caught.exception.sig, "lead:self_commit")

    def test_needs_plan_path_is_still_gated(self):
        # 复核意见：exec 返回 needs_plan 的路径也得过 Router 检查，不能借交卷跳过。
        text = json.dumps({"summary": "要先规划", "needs_plan": True})
        with self.assertRaises(amb.StepError) as caught:
            self.finish("exec", status="",
                        texts={"stdout": text, "out": text, "stderr": "", "exit": "0"})
        self.assertEqual(caught.exception.sig, "lead:no_delegation")

    def test_needs_plan_with_self_work_is_rejected(self):
        self.delegate()
        text = json.dumps({"summary": "要先规划", "needs_plan": True})
        with self.assertRaises(amb.StepError) as caught:
            self.finish("exec", status=" M src/a.py\n",
                        texts={"stdout": text, "out": text, "stderr": "", "exit": "0"})
        self.assertEqual(caught.exception.sig, "lead:self_work")

    def test_git_failure_fails_closed(self):
        # 复核意见：git status / rev-parse 都失败时不能放行。
        self.delegate()
        for git_rc in (128, "rev-parse"):
            with self.subTest(git_rc=git_rc), self.assertRaises(amb.StepError) as caught:
                self.finish("exec", status="", git_rc=git_rc)
            self.assertEqual(caught.exception.sig, "lead:check_failed")

    def test_orchestrator_pending_commit_is_not_charged(self):
        # 复核意见：编排器在提交前记下 pre_commit_head 后进程被杀，重收时 HEAD 已移动，
        # 这次移动属于编排器，不能判成 lead:self_commit 把单子卡住。
        self.delegate()
        state = self.state("exec")
        state["lead_baseline"] = ""
        state["run"].update(pre_commit_head=self.HEAD, tree="expected-tree")
        self.assertFalse(self.finish("exec", status="", head="b" * 40, state=state,
                                     tree="expected-tree").called)
        with self.assertRaises(amb.StepError) as caught:
            self.finish("exec", status="", head="b" * 40, state=state, tree="other-tree")
        self.assertEqual(caught.exception.sig, "lead:self_commit")

    def test_leader_commit_after_pending_marker_still_rejected(self):
        # pre_commit_head 只免编排器那一次提交：重新收工时它不再匹配新的基准。
        self.delegate()
        state = self.state("exec")
        state["lead_baseline"] = ""
        state["run"]["pre_commit_head"] = "c" * 40
        with self.assertRaises(amb.StepError) as caught:
            self.finish("exec", status="", head="b" * 40, state=state)
        self.assertEqual(caught.exception.sig, "lead:self_commit")

    def test_legacy_recovery_only_accepts_orchestrator_coordination_commit(self):
        self.delegate()
        state = self.state("exec")
        state["lead_baseline"] = ""
        state["run"]["legacy_exec_recovery"] = True
        subject = "amb J-LEAD a1: 带队"
        self.assertFalse(self.finish("exec", status="", head="b" * 40, state=state,
                                     subject=subject).called)
        for evidence in ({"subject": subject, "paths": "src/a.py\0"},
                         {"subject": "amb J-LEAD a2: 带队"}):
            with self.subTest(evidence=evidence), self.assertRaises(amb.StepError) as caught:
                self.finish("exec", status="", head="b" * 40, state=state, **evidence)
            self.assertEqual(caught.exception.sig, "lead:self_commit")

    def test_normal_exec_cannot_spoof_orchestrator_commit_subject(self):
        self.delegate()
        with self.assertRaises(amb.StepError) as caught:
            self.finish("exec", status="", head="b" * 40, subject="amb J-LEAD a1: 带队")
        self.assertEqual(caught.exception.sig, "lead:self_commit")

    def test_explicit_ask_leader_requires_delegation(self):
        self.job.update(kind="ask", exec="ask")
        with self.assertRaises(amb.StepError) as caught:
            self.finish("exec", status="")
        self.assertEqual(caught.exception.sig, "lead:no_delegation")
        self.delegate()
        self.finish("exec", status="")

    def test_reopened_ask_leader_requires_its_own_delegation(self):
        old = {**self.job, "id": "J-OLD"}
        amb.write_json(amb.jobs_dir("J-OLD", "job.json"), old)
        self.delegate("J-OLD-CHILD")
        os.rename(amb.jobs_dir("J-LEAD", "led"), amb.jobs_dir("J-OLD", "led"))
        self.job.update(kind="ask", reopened_from="J-OLD")
        self.assertEqual(amb.delegated_children(self.job), ["J-OLD-CHILD"])

        with self.assertRaises(amb.StepError) as caught:
            self.finish("exec", status="")
        self.assertEqual(caught.exception.sig, "lead:no_delegation")

        self.delegate()
        self.finish("exec", status="")

    def test_exec_retry_requires_new_delegation(self):
        self.delegate("J-EARLIER")
        state = self.state("exec")
        state["lead_children_before"] = ["J-EARLIER"]
        with self.assertRaises(amb.StepError) as caught:
            self.finish("exec", status="", state=state)
        self.assertEqual(caught.exception.sig, "lead:no_delegation")

        self.delegate()
        self.finish("exec", status="", state=state)

    def test_ordinary_ask_still_finishes_without_a_git_worktree_or_child(self):
        self.job.update(kind="ask", mode="", exec="ask")
        state = self.state("exec")
        state.pop("wt")
        self.finish("exec", state=state, git_rc=128)

    def test_fable_ask_finishes_directly_without_delegation(self):
        self.job.update(kind="ask", mode="")
        for streamed in (False, True):
            with self.subTest(streamed=streamed):
                state = self.state("exec")
                state.update(exec="fable", attempt=2)
                state["run"]["structured_open_items"] = True
                out = SUMMARY
                if streamed:
                    state["run"]["router_session"] = "previous-release-session"
                    out = json.dumps({"type": "result", "result": SUMMARY})
                texts = {"exit": "0", "stdout": out, "out": "", "stderr": ""}
                host = mock.Mock()
                host.git.side_effect = AssertionError("direct ask must not run Router checks")
                amb.finish_step(self.job["id"], self.job, state, CFG, host, texts)
                self.assertEqual(state["state"], "done")
                self.assertTrue(state["self_goal_met"])
                self.assertEqual(state["open_items"], [])

    def test_fable_ask_starts_as_direct_fable_execution(self):
        self.job.update(kind="ask", mode="", tier="L1")
        wt = Path(self.root.name) / "job" / "wt"
        state = {"state": "queued", "attempt": 1, "attempts": {"1": {}},
                 "exec": "fable", "history": []}
        with mock.patch.object(amb, "workdir_base", return_value=str(wt.parent)), \
                mock.patch.object(amb, "start_step") as start:
            amb.start_attempt(self.job["id"], self.job, state, CFG)
        self.assertEqual(state["attempts"]["1"]["exec"], "exec.fable")
        self.assertIn("claude-fable-5-2", start.call_args.args[6])
        self.assertTrue(wt.is_dir())
        self.assertFalse((wt / ".git").exists())
        self.assertNotIn("lead_baseline", state)

    def test_ask_leader_starts_with_git_baseline(self):
        self.job.update(kind="ask", tier="L1")
        wt = Path(self.root.name) / "job" / "wt"
        state = self.state("exec")
        state["exec"] = self.job["exec"]
        with mock.patch.object(amb, "workdir_base", return_value=str(wt.parent)), \
                mock.patch.object(amb, "start_step"):
            amb.start_attempt(self.job["id"], self.job, state, CFG)
        host = amb.Host(CFG, "local")
        self.assertEqual(state["lead_baseline"], {})
        self.assertEqual(state["start_sha"], host.git(str(wt), "rev-parse", "HEAD").stdout.strip())
        self.assertEqual(state["attempts"]["1"]["exec"], "exec.fable")

    def test_repair_checks_content_and_index_when_status_lines_stay_the_same(self):
        host = amb.Host(CFG, "local")
        wt = self.root.name + "/wt"
        amb.init_doc_repo(host, wt)
        source = Path(wt) / "dirty source.py"
        source.write_text("original\n")
        host.git(wt, "add", "--", source.name)
        host.git(wt, "commit", "-qm", "fixture")
        sha = host.git(wt, "rev-parse", "HEAD").stdout.strip()
        self.delegate()
        state = self.state("repair")
        state["wt"], state["run"]["sha"] = wt, sha

        for staged in (False, True):
            with self.subTest(staged=staged):
                source.write_text("worker change\n")
                if staged:
                    host.git(wt, "add", "--", source.name)
                state["repair_baseline"] = amb.leader_snapshot(host, wt)
                status = host.git(wt, "status", "--porcelain").stdout
                self.assertIsNone(amb.leader_breach(self.job, state, CFG, host))
                source.write_text("router change\n")
                if staged:
                    host.git(wt, "add", "--", source.name)
                self.assertEqual(host.git(wt, "status", "--porcelain").stdout, status)
                self.assertEqual(amb.leader_breach(self.job, state, CFG, host)[0], "lead:self_work")

        state["repair_baseline"] = status
        self.assertEqual(amb.leader_breach(self.job, state, CFG, host)[0], "lead:check_failed")

    def test_only_verified_child_copies_are_exempt(self):
        host = amb.Host(CFG, "local")
        parent_wt = Path(self.root.name) / "leader"
        child_wt = Path(self.root.name) / "child"
        for wt in (parent_wt, child_wt):
            amb.init_doc_repo(host, str(wt))
        start = host.git(str(child_wt), "rev-parse", "HEAD").stdout.strip()
        name = "report [draft].md"
        body = b"child delivery\n"
        (child_wt / name).write_bytes(body)
        host.git(str(child_wt), "add", "--", name)
        host.git(str(child_wt), "commit", "-qm", "delegated report")
        sha = host.git(str(child_wt), "rev-parse", "HEAD").stdout.strip()
        self.delegate()
        amb.write_json(amb.jobs_dir("J-CHILD", "job.json"),
                       {"lead_parent": self.job["id"], "host": "local"})
        amb.write_json(amb.jobs_dir("J-CHILD", "state.json"),
                       {"state": "done", "wt": str(child_wt), "start_sha": start, "accepted_sha": sha})
        state = self.state("exec")
        state.update(wt=str(parent_wt), lead_baseline={}, lead_children_before=[])
        state["start_sha"] = host.git(str(parent_wt), "rev-parse", "HEAD").stdout.strip()
        report = parent_wt / name
        report.write_bytes(body)
        self.assertIsNone(amb.leader_breach(self.job, state, CFG, host))
        (parent_wt / "unrelated.md").write_bytes(body)
        self.assertIn("unrelated.md", amb.leader_breach(self.job, state, CFG, host)[1])
        (parent_wt / "unrelated.md").unlink()
        host.git(str(parent_wt), "add", "--", name)
        self.assertEqual(amb.leader_breach(self.job, state, CFG, host)[0], "lead:self_work")
        state["run"]["leader_staging"] = True
        self.assertIsNone(amb.leader_breach(self.job, state, CFG, host))
        report.write_bytes(b"leader edit\n")
        self.assertEqual(amb.leader_breach(self.job, state, CFG, host)[0], "lead:self_work")


class PlanQuotaStaysOnHostTests(unittest.TestCase):
    """<REDACTED_ID>：开发机单的规划配额降级不能跳回 VPS 的 plan.opus。"""

    CFG = {
        "hosts": {"local": {"ssh": None, "work_root": "~/amb-work"},
                  "<REDACTED_HOST_A>": {"ssh": "<REDACTED_HOST_A>", "work_root": "~/amb-work",
                              "models": {"claude-fable-5-2": "claude-fable-5-1"}}},
        "roles": {
            "plan.opus": {"model": "claude-opus-5-5", "host": "local",
                          "cmd": "claude -p --model claude-opus-5-5 --permission-mode plan"},
            "plan.fable.dev": {"model": "claude-fable-5-1", "host": "<REDACTED_HOST_A>",
                               "cmd": "bash <REDACTED_PATH> -p --model claude-fable-5-1"},
            "exec.astra": {"model": "gpt-6.2-astra", "cmd": "codex exec -m gpt-6.2-astra -"},
        },
        "quota_fallback": {"plan": ["plan.fable.dev", "exec.astra"]},
    }

    def test_dev_plan_fallback_does_not_return_to_vps_opus(self):
        choice = amb.quota_fallback_choice(
            self.CFG, "plan", {}, "<REDACTED_HOST_A>", True,
            ["bash", "<REDACTED_PATH>", "-p", "--model", "claude-fable-5-1"])
        self.assertEqual(choice, ("exec.astra", "<REDACTED_HOST_A>"))

    def test_local_plan_falls_back_to_dev_fable(self):
        choice = amb.quota_fallback_choice(
            self.CFG, "plan", {}, "local", True,
            ["claude", "-p", "--model", "claude-opus-5-5"])
        self.assertEqual(choice, ("plan.fable.dev", "<REDACTED_HOST_A>"))


if __name__ == "__main__":
    unittest.main()
