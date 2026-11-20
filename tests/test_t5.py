"""T5 黑盒测试：L3 规划/子任务/集成验收/replan，install-cron.sh。"""
import json
import os
import shlex
import subprocess
import time
import unittest
from pathlib import Path

try:
    from test_t2 import FAKES, JOB_ID, TERMINAL, T2Base, role_cmd, write_json
except ImportError:
    from tests.test_t2 import FAKES, JOB_ID, TERMINAL, T2Base, role_cmd, write_json

ROOT = Path(__file__).resolve().parents[1]


class T5Base(T2Base):
    def setUp(self):
        super().setUp()
        self.config["roles"]["plan.fable"] = {"cmd": role_cmd(FAKES / "plan_two_subs.sh"), "host": "local"}
        self.config["repos"][f"local:{self.repo}"]["auto_merge"] = True
        self.save_config()

    def run_to_end(self, timeout=120):
        return self.tick_until(lambda s: s["state"] in TERMINAL, timeout=timeout, what="terminal")


class TestT5Plan(T5Base):
    def test_two_subs_run_in_order_and_parent_done(self):
        self.submit(tier="L3", land="merge", accept=[{"id": "A1", "cmd": "test $(wc -l < DONE.txt) -eq 2"}])
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        subs = st["subs"]
        self.assertEqual(len(subs), 2)
        s1, s2 = (json.loads((self.jobs / s / "state.json").read_text(encoding="utf-8")) for s in subs)
        self.assertEqual((s1["state"], s2["state"]), ("done", "done"))
        self.assertLess(s1["history"][-1]["ts"], s2["history"][1]["ts"] + "~")
        # s2 建在 s1 之上：s1.sha 是 s2.sha 的祖先
        self.git("merge-base", "--is-ancestor", s1["sha"], s2["sha"])
        self.assertEqual(st["sha"], s2["sha"])
        states = [h["to"] for h in st["history"]]
        for a, b in [("planning", "subs"), ("subs", "accept"), ("accept", "review"), ("review", "merge")]:
            self.assertLess(states.index(a), states.index(b), states)
        self.assertEqual(self.git("rev-parse", "main").strip(), s2["sha"])
        self.assertTrue((self.jobs / JOB_ID / "attempts" / "1" / "plan.json").is_file())

    def test_sub_blocked_blocks_parent(self):
        for role in ("exec.sol", "exec.opus", "exec.opus-high"):
            self.set_role(role, FAKES / "exec_bad.sh")
        self.submit(tier="L3", limits={"step_minutes": 45, "max_attempts": 2})
        st = self.run_to_end()
        self.assertEqual(st["state"], "blocked", st)
        self.assertIn("子任务", st["needs_human"])

    def test_invalid_plan_is_bad_output(self):
        self.set_role("plan.fable", FAKES / "exec_garbage.sh")
        self.submit(tier="L3", limits={"step_minutes": 45, "max_attempts": 3})
        st = self.run_to_end()
        self.assertEqual(st["state"], "blocked", st)
        fail = json.loads((self.jobs / JOB_ID / "attempts" / "1" / "failure.json").read_text(encoding="utf-8"))
        self.assertEqual(fail["sig"], "bad_output")
        self.assertEqual(st["attempt"], 2, "L3 只 replan 一级")

    def test_plan_with_unknown_host_or_changed_accept_rejected(self):
        bad = self.script("plan_bad.sh", "cat > \"$3\" <<'J'\n"
                          '{"accept":[{"id":"A1","cmd":"true"}],"subs":[{"title":"s","host":"local",'
                          '"accept":[{"id":"A1","cmd":"true"}],"depends_on":[]}]}\nJ\n')
        self.set_role("plan.fable", bad)
        self.submit(tier="L3")
        st = self.run_to_end()
        self.assertEqual(st["state"], "blocked")
        self.assertIn("验收", (self.jobs / JOB_ID / "attempts" / "1" / "failure.json").read_text(encoding="utf-8"))

    def test_legacy_allow_replan_no_longer_bypasses_executor(self):
        self.set_role("exec.sol", FAKES / "exec_bad.sh")
        self.config["ladder"]["sol"] = ["sol", "replan"]
        self.save_config()
        self.submit(allow_replan=True)
        st = self.run_to_end()
        self.assertEqual((st["state"], st["attempt"]), ("blocked", 1))
        self.assertIn("accept:A1", st["needs_human"])
        self.assertFalse(self.history_to(st, "planning"))

    def test_executor_explicit_needs_plan_promotes_to_l3(self):
        signal = self.script("need_plan.sh", """printf '%s\n' '{"summary":"dependency unknown","needs_plan":true}' > "$3"
""")
        self.set_role("exec.opus", signal)
        self.submit(tier="L2", exec="opus")
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        self.assertTrue(st["escalated_to_l3"])
        self.assertEqual(len(self.history_to(st, "planning")), 1)
        prompt = (self.jobs / JOB_ID / "attempts" / "2" / "plan" / "prompt.md").read_text()
        self.assertIn("dependency unknown", prompt)

    def test_plan_timeout_blocks_on_first_attempt(self):
        self.set_role("plan.fable", self.script("plan_hang.sh", "sleep 30\n"))
        self.submit(tier="L3", limits={"plan_minutes": 0.01, "step_minutes": 45, "max_attempts": 3})
        self.tick()
        exit_file = self.jobs / JOB_ID / "attempts" / "1" / "plan" / "exit"
        deadline = time.monotonic() + 5
        while not exit_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(exit_file.read_text(), "timeout", "runner must time out without another tick")
        st = self.run_to_end()
        self.assertEqual((st["state"], st["attempt"]), ("blocked", 1))
        self.assertIn("plan:timeout", st["needs_human"])

    def test_l1_and_l2_default_ladder_stop_before_planning(self):
        self.set_role("exec.sol", FAKES / "exec_bad.sh")
        self.set_role("exec.opus", FAKES / "exec_bad.sh")
        self.submit()
        st = self.run_to_end()
        self.assertEqual((st["state"], st["attempt"], st["exec"]), ("blocked", 2, "opus"))
        self.assertFalse(self.history_to(st, "planning"))

        self.set_role("exec.sol", FAKES / "exec_ok.sh")
        self.set_role("exec.opus", FAKES / "exec_ok.sh")
        self.set_role("review.opus", FAKES / "review_fail.sh")
        self.set_role("review.codex", FAKES / "review_fail.sh")
        jid = f"{JOB_ID}-L2"
        self.submit(job_id=jid, tier="L2", kind="doc", accept=[], checklist=["证据充分"])
        st = self.tick_until(lambda s: s["state"] in TERMINAL, job_id=jid)
        self.assertEqual((st["state"], st["attempt"], st["exec"]), ("blocked", 2, "opus"))
        self.assertEqual(len(self.history_to(st, "review")), 2)
        self.assertFalse(self.history_to(st, "planning"))

    def test_stale_plan_flag_cannot_route_l2_to_planner(self):
        self.submit(tier="L2")
        st = self.state()
        st["plan_needed"] = True
        write_json(self.jobs / JOB_ID / "state.json", st)
        st = self.run_to_end()
        self.assertEqual(st["state"], "done")
        self.assertFalse(self.history_to(st, "planning"))

    def test_l3_large_stdout_plan_advances_to_subs(self):
        plan = self.script("long_plan.sh", """python3 - <<'PY'
import json
print(json.dumps({"subs": [{"title": "s", "brief": "x" * 210000, "accept": [{"cmd": "true"}]}]}))
PY
""")
        self.set_role("plan.fable", plan)
        self.submit(tier="L3")
        st = self.tick_until(lambda s: s["state"] == "subs")
        self.assertEqual(len(st["subs"]), 1)
        self.assertEqual(len(json.loads((self.jobs / JOB_ID / "attempts/1/plan.json").read_text())
                             ["subs"][0]["brief"]), 210000)

    def test_planner_nonzero_exit_is_reported_even_with_valid_json(self):
        plan = self.script("failed_plan.sh", f"{shlex.quote(str(FAKES / 'plan_two_subs.sh'))} \"$@\"\n"
                           "echo 'planner command failed' >&2\nexit 7\n")
        self.set_role("plan.fable", plan)
        self.submit(tier="L3", limits={"max_attempts": 1})
        st = self.run_to_end()
        fail = json.loads(self.failures()[0].read_text())
        self.assertEqual(st["state"], "blocked")
        self.assertEqual(fail["sig"], "plan:exit")
        self.assertIn("exit 7: planner command failed", fail["detail"])


class TestT5InstallCron(unittest.TestCase):
    def test_install_is_idempotent_and_keeps_other_lines(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "tab"
            store.write_text("0 1 * * * /other/job.sh # keep\n", encoding="utf-8")
            fake = Path(d) / "crontab"
            fake.write_text(f"#!/usr/bin/env bash\nif [ \"$1\" = -l ]; then cat {shlex.quote(str(store))}; "
                            f"else cat > {shlex.quote(str(store))}; fi\n", encoding="utf-8")
            fake.chmod(0o755)
            env = {**os.environ, "CRONTAB_BIN": str(fake)}
            for _ in range(3):
                subprocess.run(["bash", str(ROOT / "install-cron.sh")], env=env, check=True, capture_output=True)
            lines = store.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, ["0 1 * * * /other/job.sh # keep",
                                     "* * * * * flock -n ~/amb/.lock python3 ~/amb/amb.py tick"])


if __name__ == "__main__":
    unittest.main()
