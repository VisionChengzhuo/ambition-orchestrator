"""J-1123-14：模型按机器改写、配额降级链、Router 只派活。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

AMB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AMB_DIR))
import amb  # noqa: E402

CFG = {
    "lead_mode": True,
    "defaults": {"ssh_timeout_s": 20},
    "hosts": {
        "local": {"ssh": None, "work_root": "~/amb-work"},
        "kml-dev": {"ssh": "kml-dev", "work_root": "~/amb-work",
                    "models": {"claude-fable-5-2": "claude-fable-5-1"}},
    },
    "roles": {
        "exec.fable": {
            "model": "claude-fable-5-2",
            "cmd": "claude -p --model claude-fable-5-2 --effort low",
            "host_commands": {
                "kml-dev": "bash /x/claude-full -p --model claude-fable-5-1 --effort low"},
        },
        "exec.opus": {"model": "claude-opus-5-5",
                      "cmd": "claude -p --model claude-opus-5-5 --effort medium"},
        "exec.astra": {"model": "gpt-6.2-astra", "cmd": "codex exec -m gpt-6.2-astra -"},
        "plan.opus": {"model": "claude-opus-5-5", "host": "local",
                      "cmd": "claude -p --model claude-opus-5-5 --permission-mode plan"},
        "plan.fable.dev": {"model": "claude-fable-5-1", "host": "kml-dev",
                           "cmd": "bash /x/claude-full -p --model claude-fable-5-1"},
    },
    "quota_fallback": {"plan": ["plan.fable.dev", "exec.astra"], "exec": ["exec.astra"]},
}


class ModelRoutingTests(unittest.TestCase):
    def test_kml_dev_rewrites_declared_model(self):
        argv = amb.role_argv(CFG, "exec.fable", {}, host="kml-dev")
        self.assertEqual(amb.model_arg(argv), "claude-fable-5-1")

    def test_local_keeps_declared_model(self):
        argv = amb.role_argv(CFG, "exec.fable", {}, host="local")
        self.assertEqual(amb.model_arg(argv), "claude-fable-5-2")

    def test_override_is_rewritten_for_target_host(self):
        argv = amb.exec_argv(CFG, "exec.opus", {}, "kml-dev", {"model": "claude-fable-5-2"})
        self.assertEqual(amb.model_arg(argv), "claude-fable-5-1")
        self.assertNotIn("claude-fable-5-2", argv)

    def test_dev_fable_job_model_is_rewritten_before_launch(self):
        # J-1123-10：开发机上 --model claude-fable-5-2 报“模型不存在”，降级成 opus-high。
        argv = amb.exec_argv(CFG, "exec.fable", {}, "kml-dev",
                             {"model": "claude-fable-5-2", "effort": "high"})
        self.assertEqual(amb.model_arg(argv), "claude-fable-5-1")
        self.assertNotIn("claude-fable-5-2", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertIn("claude-full", " ".join(argv))

    def test_dev_fable_retry_keeps_rewritten_model(self):
        argv = amb.exec_argv(CFG, "exec.fable", {}, "kml-dev", {"model": "claude-fable-5-1"})
        self.assertEqual(amb.model_arg(argv), "claude-fable-5-1")

    def test_override_keeps_other_flags(self):
        argv = amb.exec_argv(CFG, "exec.opus", {}, "local", {"model": "claude-fable-5-2"})
        self.assertEqual(amb.model_arg(argv), "claude-fable-5-2")
        self.assertIn("--effort", argv)

    def test_set_model_arg_handles_equals_form(self):
        self.assertEqual(amb.set_model_arg(["claude", "--model=old", "-p"], "new"),
                         ["claude", "--model=new", "-p"])
        self.assertEqual(amb.set_model_arg(["-m", "old", "-p"], "new"), ["-m", "new", "-p"])

    def test_effective_model_reports_wire_name(self):
        self.assertEqual(amb.effective_model(CFG, "exec.fable", host="kml-dev"),
                         "claude-fable-5-1")


class QuotaFallbackTests(unittest.TestCase):
    def test_plan_falls_back_to_dev_fable_first(self):
        self.assertEqual(amb.quota_fallback_choice(CFG, "plan", {}, "local", True),
                         ("plan.fable.dev", "kml-dev"))

    def test_exec_does_not_switch_host(self):
        choice = amb.quota_fallback_choice(CFG, "exec", {}, "local", False,
                                           ["claude", "--model", "claude-opus-5-5"])
        self.assertEqual(choice, ("exec.astra", "local"))

    def test_same_host_same_model_is_not_a_fallback(self):
        cfg = {"hosts": CFG["hosts"], "roles": {
            "exec.fable": CFG["roles"]["exec.fable"],
            "exec.astra": CFG["roles"]["exec.astra"]},
            "quota_fallback": {"exec": ["exec.fable", "exec.astra"]}}
        choice = amb.quota_fallback_choice(cfg, "exec", {}, "kml-dev", False,
                                           ["claude", "--model", "claude-fable-5-1"])
        self.assertEqual(choice, ("exec.astra", "kml-dev"))

    def test_unknown_host_is_skipped(self):
        cfg = {"hosts": {"local": CFG["hosts"]["local"]},
               "roles": {"plan.fable.dev": CFG["roles"]["plan.fable.dev"]},
               "quota_fallback": {"plan": ["plan.fable.dev"]}}
        self.assertIsNone(amb.quota_fallback_choice(cfg, "plan", {}, "local", True))

    def test_default_chain_without_config(self):
        cfg = {"hosts": CFG["hosts"], "roles": CFG["roles"]}
        self.assertEqual(amb.quota_fallback_choice(cfg, "exec", {}, "local"),
                         ("exec.astra", "local"))


class DevClaudeLauncherTests(unittest.TestCase):
    """复核意见：VPS 配置可能还没给 review.opus / ask 补 kml-dev 覆写。

    开发机上的 Claude 一律走第三方 API 包装器，不能依赖每个角色都写了
    host_commands.kml-dev；框架按主机兜底，线上配置缺项也照样走 claude-full。
    """

    def test_bare_claude_role_on_kml_dev_uses_wrapper(self):
        argv = amb.role_argv(CFG, "exec.opus", {}, host="kml-dev")
        self.assertIn("claude-full", " ".join(argv))
        self.assertEqual(os.path.basename(argv[0]), "bash")
        self.assertEqual(amb.model_arg(argv), "claude-opus-5-5")

    def test_already_wrapped_command_is_unchanged(self):
        argv = amb.role_argv(CFG, "exec.fable", {}, host="kml-dev")
        self.assertEqual(sum(os.path.basename(t) == "claude-full" for t in argv), 1)
        self.assertEqual(argv[:2], ["bash", "/x/claude-full"])

    def test_local_keeps_bare_claude(self):
        argv = amb.role_argv(CFG, "exec.opus", {}, host="local")
        self.assertEqual(argv[0], "claude")

    def test_env_prefixed_command_still_wraps(self):
        cfg = {"hosts": CFG["hosts"], "roles": {"ask": {
            "model": "claude-opus-5-5",
            "cmd": "env PATH=/usr/bin:/bin claude -p --model claude-opus-5-5"}}}
        argv = amb.role_argv(cfg, "ask", {}, host="kml-dev")
        self.assertEqual(argv[:3], ["env", "PATH=/usr/bin:/bin", "bash"])


class ShippedConfigDevLauncherTests(unittest.TestCase):
    """复核意见：线上 kml-dev 的 ask 和 review.opus 仍生成裸 claude 命令。

    路由测试配置 必须给这两个角色写 kml-dev 覆写，配置缺项时也由代码兜底。
    """

    def setUp(self):
        import json
        self.cfg = json.loads((AMB_DIR / "tests/fixtures/routing.json").read_text(encoding="utf-8"))

    def test_ask_and_review_use_wrapper_on_dev(self):
        for role in ("ask", "review.opus", "exec.opus", "review.codex"):
            argv = amb.role_argv(self.cfg, role, {}, host="kml-dev")
            if "claude-full" not in " ".join(argv):
                self.assertTrue(role.startswith("review.codex"), f"{role}: {argv}")
        for role in ("ask", "review.opus"):
            argv = amb.role_argv(self.cfg, role, {}, host="kml-dev")
            self.assertIn("claude-full", " ".join(argv))
            self.assertEqual(os.path.basename(argv[0]), "bash")

    def test_missing_host_override_still_wraps(self):
        cfg = {"hosts": self.cfg["hosts"], "roles": {
            "ask": self.cfg["roles"]["ask"], "review.opus": self.cfg["roles"]["review.opus"]}}
        for role in ("ask", "review.opus"):
            argv = amb.role_argv(cfg, role, {}, host="kml-dev")
            self.assertIn("claude-full", " ".join(argv))

    def test_local_roles_keep_official_claude(self):
        for role in ("ask", "review.opus", "exec.opus"):
            argv = amb.role_argv(self.cfg, role, {}, host="local")
            self.assertEqual(argv[0], "claude")
            self.assertNotIn("claude-full", " ".join(argv))


class RouterOnlyTests(unittest.TestCase):
    def test_no_delegation_is_a_violation(self):
        sig, _ = amb.leader_violation([], {}, {})
        self.assertEqual(sig, "lead:no_delegation")

    def test_result_files_are_allowed(self):
        self.assertIsNone(amb.leader_violation(["J-sub"], {}, {"out.md": "a", "RESULT.md": "b"}))

    def test_self_work_is_rejected(self):
        sig, detail = amb.leader_violation(
            ["J-sub"], {}, {"src/a.py": "a", "notes.txt": "b", "out.md": "c"})
        self.assertEqual(sig, "lead:self_work")
        self.assertIn("src/a.py", detail)
        self.assertNotIn("out.md", detail)

    def test_baseline_changes_are_not_blamed_on_leader(self):
        self.assertIsNone(amb.leader_violation(
            ["J-sub"], {"src/a.py": "a"}, {"src/a.py": "a", "out.md": "b"}))

    def test_leader_rules_forbid_worktree_edits(self):
        self.assertIn("Router 只负责协调", amb.LEADER_RULES)
        self.assertNotIn("只改工作树", amb.LEADER_RULES)

    def test_exec_prompt_swaps_rules_for_leaders(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("AMB_ROOT")
            os.environ["AMB_ROOT"] = tmp
            try:
                host = amb.Host(CFG, "local")
                base = {"user_words": "把活派出去", "host": "local", "title": "t"}
                leader = {**base, "id": "J-T-LEAD", "exec": "fable"}
                worker = {**base, "id": "J-T-WORK", "exec": "opus"}
                text = amb.exec_prompt(leader, {"attempt": 1}, CFG, host, "", tmp)
                self.assertIn("Router 只负责协调", text)
                self.assertNotIn("只改工作树", text)
                self.assertIn("mcp__amb_router__dispatch", text)
                text = amb.exec_prompt(worker, {"attempt": 1}, CFG, host, "", tmp)
                self.assertIn("只改工作树", text)
            finally:
                if old is None:
                    os.environ.pop("AMB_ROOT", None)
                else:
                    os.environ["AMB_ROOT"] = old


class ShippedConfigTests(unittest.TestCase):
    """路由测试配置：开发机的 Claude 一律走第三方 API 包装器，模型名按机器改写。"""

    def setUp(self):
        import json
        self.cfg = json.loads((AMB_DIR / "tests/fixtures/routing.json").read_text(encoding="utf-8"))

    def test_dev_host_maps_fable_model(self):
        self.assertEqual(self.cfg["hosts"]["kml-dev"]["models"], {"claude-fable-5-2": "claude-fable-5-1"})
        self.assertIn("第三方", self.cfg["hosts"]["kml-dev"]["provider"])

    def test_dev_claude_roles_use_third_party_wrapper(self):
        for name in ("exec.fable", "exec.opus", "exec.opus-high", "review.opus", "ask",
                     "plan.fable.dev", "exec.fable.dev"):
            with self.subTest(role=name):
                role = self.cfg["roles"][name]
                cmd = (role.get("host_commands") or {}).get("kml-dev")
                if not cmd and role.get("host") == "kml-dev":
                    cmd = role["cmd"]
                self.assertIsNotNone(cmd, name)
                self.assertIn("/opt/amb/claude-full", cmd)

    def test_no_dev_command_keeps_vps_only_model(self):
        for name, role in self.cfg["roles"].items():
            cmd = (role.get("host_commands") or {}).get("kml-dev")
            if not cmd:
                continue
            with self.subTest(role=name):
                model = amb.model_arg(cmd.split())
                self.assertNotEqual(model, "claude-fable-5-2")
                if model and model.startswith("claude-fable"):
                    self.assertEqual(model, "claude-fable-5-1")

    def test_shipped_config_parses_into_roles(self):
        cfg = self.cfg
        self.assertEqual(amb.plan_role(cfg, {"host": "kml-dev", "exec": "fable",
                                             "model": "claude-fable-5-1"}), "plan.fable.dev")
        self.assertEqual(amb.plan_host(cfg, "plan.fable.dev", {"host": "kml-dev"}), "kml-dev")


if __name__ == "__main__":
    unittest.main()
