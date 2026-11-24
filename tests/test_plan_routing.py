"""J-1123-14 a2：规划按本单指定的机器和执行者走，不再无条件用 VPS plan.opus。

用户原话（12:22）：开发机的 Claude 接的是第三方 API；计划里点名
`--host kml-dev --exec fable --model claude-fable-5-1` 的 L3 单仍走 VPS plan.opus，
VPS 额度耗尽后又降成 astra，没在开发机 Fable 上带队。
"""
import os
import sys
import unittest
from pathlib import Path

AMB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AMB_DIR))
import amb  # noqa: E402

CFG = {
    "lead_mode": True,
    "planner": "plan.opus",
    "defaults": {"ssh_timeout_s": 20, "plan_minutes": 20},
    "hosts": {
        "local": {"ssh": None, "work_root": "~/amb-work"},
        "kml-dev": {"ssh": "kml-dev", "work_root": "~/amb-work",
                    "models": {"claude-fable-5-2": "claude-fable-5-1"}},
    },
    "roles": {
        "exec.sol": {"model": "gpt-6.2-sol",
                     "cmd": "codex exec -m gpt-6.2-sol -c model_reasoning_effort=medium -"},
        "exec.opus": {"model": "claude-opus-5-5",
                      "cmd": "claude -p --model claude-opus-5-5 --effort medium",
                      "host_commands": {
                          "kml-dev": "bash /x/claude-full -p --model claude-opus-5-5 --effort high"}},
        "exec.fable": {"model": "claude-fable-5-2",
                       "cmd": "claude -p --model claude-fable-5-2 --effort low",
                       "host_commands": {
                           "kml-dev": "bash /x/claude-full -p --model claude-fable-5-1 --effort low"}},
        "plan.opus": {"model": "claude-opus-5-5", "host": "local",
                      "cmd": "claude -p --model claude-opus-5-5 --effort high --permission-mode plan"},
        "plan.fable.dev": {"model": "claude-fable-5-1", "host": "kml-dev",
                           "cmd": "bash /x/claude-full -p --model claude-fable-5-1 "
                                  "--effort high --permission-mode plan"},
    },
}


class PlanRoleTests(unittest.TestCase):
    def test_local_job_uses_configured_planner(self):
        self.assertEqual(amb.plan_role(CFG, {"host": "local", "exec": "sol"}), "plan.opus")

    def test_kml_dev_fable_plans_on_dev_fable(self):
        job = {"host": "kml-dev", "exec": "fable", "model": "claude-fable-5-1", "effort": "high"}
        self.assertEqual(amb.plan_role(CFG, job), "plan.fable.dev")

    def test_kml_dev_opus_without_dedicated_plan_role_uses_exec_role(self):
        self.assertEqual(amb.plan_role(CFG, {"host": "kml-dev", "exec": "opus"}), "exec.opus")

    def test_kml_dev_sol_uses_exec_role(self):
        self.assertEqual(amb.plan_role(CFG, {"host": "kml-dev", "exec": "sol"}), "exec.sol")

    def test_vps_planner_fallback_stays_on_vps(self):
        # 没有可用的目标机规划角色时，不能把 VPS 的 plan.opus 命令搬到开发机上跑。
        self.assertEqual(amb.plan_host(CFG, "plan.opus", {"host": "kml-dev"}), "local")
        self.assertEqual(amb.plan_host(CFG, "exec.sol", {"host": "kml-dev"}), "kml-dev")
        self.assertEqual(amb.plan_host(CFG, "plan.fable.dev", {"host": "kml-dev"}), "kml-dev")


class PlanArgvTests(unittest.TestCase):
    def job(self, **kw):
        base = {"id": "J-T", "host": "kml-dev", "exec": "fable"}
        base.update(kw)
        return base

    def test_explicit_model_and_effort_are_honoured_on_dev(self):
        argv = amb.plan_argv(CFG, "plan.fable.dev", {},
                             "kml-dev", self.job(model="claude-fable-5-1", effort="high"))
        self.assertEqual(amb.model_arg(argv), "claude-fable-5-1")
        self.assertIn("claude-full", " ".join(argv))
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_model_that_only_exists_on_vps_is_rewritten_for_dev(self):
        argv = amb.plan_argv(CFG, "plan.fable.dev", {},
                             "kml-dev", self.job(model="claude-fable-5-2"))
        self.assertEqual(amb.model_arg(argv), "claude-fable-5-1")
        self.assertNotIn("claude-fable-5-2", argv)

    def test_planner_can_write_dispatch_json(self):
        argv = amb.plan_argv(CFG, "plan.fable.dev", {}, "kml-dev", self.job())
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")

    def test_exec_role_without_permission_mode_gets_one(self):
        argv = amb.plan_argv(CFG, "exec.opus", {}, "kml-dev", self.job(exec="opus"))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(amb.model_arg(argv), "claude-opus-5-5")

    def test_local_planner_still_honours_job_model(self):
        argv = amb.plan_argv(CFG, "plan.opus", {}, "local",
                             {"id": "J-T", "host": "local", "exec": "sol", "model": "claude-fable-5-2"})
        self.assertEqual(amb.model_arg(argv), "claude-fable-5-2")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")


class StartPlanIntegrationTests(unittest.TestCase):
    """start_plan 必须把本单的机器/执行者/模型/档位带进规划命令。"""

    def test_start_plan_launches_on_requested_host(self):
        from unittest import mock
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("AMB_ROOT")
            os.environ["AMB_ROOT"] = tmp
            try:
                job = {"id": "J-PLAN-1", "host": "kml-dev", "exec": "fable",
                       "model": "claude-fable-5-1", "effort": "high", "kind": "doc",
                       "repo": "", "user_words": "让开发机 Fable 带队",
                       "accept": [], "checklist": ["结果是否回应用户原话"],
                       "linear": False, "parent": None, "depends_on": []}
                st = amb.initial_state(job)
                os.makedirs(amb.jobs_dir(job["id"]))
                amb.atomic_write(amb.jobs_dir(job["id"], "brief.md"), "需求" * 500)

                class FakeHost:
                    name = "kml-dev"
                    ssh = "kml-dev"
                    cfg = CFG["hosts"]

                    def __init__(self, cfg, name):
                        self.name = name
                        self.ssh = (cfg.get("hosts", {}).get(name) or {}).get("ssh")
                        self.cfg = cfg.get("hosts", {}).get(name) or {}

                    def path(self, p):
                        return "/home/u" + p[1:] if str(p).startswith("~") else str(p)

                    def run(self, *a, **k):
                        return mock.Mock(returncode=0, stdout="", stderr="")

                    def put(self, *a, **k):
                        return None

                with mock.patch.object(amb, "Host", FakeHost), \
                        mock.patch.object(amb, "start_step") as launch:
                    amb.start_plan(job["id"], job, st, CFG)
                args = launch.call_args.args
                argv = args[6]
                self.assertEqual(args[4].name, "kml-dev")
                self.assertEqual(amb.model_arg(argv), "claude-fable-5-1")
                self.assertEqual(argv[argv.index("--effort") + 1], "high")
                self.assertEqual(st["attempts"]["1"]["plan"], "plan.fable.dev")
                self.assertEqual(st["attempts"]["1"]["plan_model"], "claude-fable-5-1")
            finally:
                if old is None:
                    os.environ.pop("AMB_ROOT", None)
                else:
                    os.environ["AMB_ROOT"] = old


class PackagedConfigLaunchTests(unittest.TestCase):
    """用路由测试配置 只读计算 J-1123-12 的启动参数（复核意见第 1 条）。

    复核指出原单记录仍是 VPS 的 plan.opus，后续也没有开发机 Fable 完成规划的实跑证据。
    这里把「会下发什么命令」固定成可重复的检查：机器、角色、模型名、包装器、降级链
    都必须与线上配置一致，不靠一次性的手工计算。
    """

    def setUp(self):
        import json
        self.cfg = json.loads((AMB_DIR / "tests/fixtures/routing.json").read_text(encoding="utf-8"))

    def j1123_12(self):
        return {"id": "J-1123-12", "host": "kml-dev", "exec": "fable",
                "model": "claude-fable-5-1", "effort": "high", "kind": "doc",
                "repo": "", "user_words": "让开发机 Fable 带队", "accept": [],
                "checklist": ["结果是否回应用户原话"], "mode": "lead"}

    def test_j1123_12_plans_on_kml_dev_fable(self):
        job = self.j1123_12()
        role = amb.plan_role(self.cfg, job)
        self.assertEqual(role, "plan.fable.dev")
        self.assertEqual(amb.plan_host(self.cfg, role, job), "kml-dev")
        argv = amb.plan_argv(self.cfg, role, {}, "kml-dev", job)
        self.assertEqual(amb.model_arg(argv), "claude-fable-5-1")
        self.assertIn("claude-full", " ".join(argv))
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")

    def test_j1123_12_quota_fallback_stays_on_dev(self):
        job = self.j1123_12()
        argv = amb.plan_argv(self.cfg, amb.plan_role(self.cfg, job), {}, "kml-dev", job)
        self.assertEqual(
            amb.quota_fallback_choice(self.cfg, "plan", {}, "kml-dev", True, argv),
            ("exec.astra", "kml-dev"))

    def test_live_config_missing_review_and_ask_overrides_still_wraps(self):
        # 复核意见第 2 条：线上 VPS 配置可能仍缺 review.opus / ask 的 kml-dev 覆写。
        for role in ("review.opus", "ask"):
            self.cfg["roles"][role].get("host_commands", {}).pop("kml-dev", None)
            argv = amb.role_argv(self.cfg, role, {}, host="kml-dev")
            self.assertIn("claude-full", " ".join(argv))
            self.assertNotEqual(os.path.basename(argv[0]), "claude")

    def test_every_claude_role_wraps_on_kml_dev(self):
        for role, rcfg in (self.cfg.get("roles") or {}).items():
            if not (rcfg.get("cmd") or "").lstrip().startswith(("claude", "env ")):
                continue
            argv = amb.role_argv(self.cfg, role, {}, host="kml-dev")
            if amb.launcher_family(argv) == "claude":
                self.assertIn("claude-full", " ".join(argv), role)
                self.assertNotEqual(os.path.basename(argv[0]), "claude")


if __name__ == "__main__":
    unittest.main()
