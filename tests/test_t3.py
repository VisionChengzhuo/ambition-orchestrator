"""T3 黑盒测试：复核、升级链、合并（复用 T2 的夹具）。"""
import shlex
import unittest

try:
    from test_t2 import FAKES, JOB_ID, TERMINAL, T2Base
except ImportError:  # python3 -m unittest tests.test_t3
    from tests.test_t2 import FAKES, JOB_ID, TERMINAL, T2Base


class T3Base(T2Base):
    def setUp(self):
        super().setUp()
        self.config["repos"][f"local:{self.repo}"]["auto_merge"] = True
        self.save_config()

    def run_to_end(self, timeout=90):
        return self.tick_until(lambda s: s["state"] in TERMINAL, timeout=timeout, what="terminal")

    def main_sha(self):
        return self.git("rev-parse", "refs/heads/main").strip()

    def exec_that(self, name, extra):
        """exec_ok 之后再执行一段 bash（$1 = workdir）。"""
        return self.script(name, f"{shlex.quote(str(FAKES / 'exec_ok.sh'))} \"$@\"\n{extra}")


class TestT3Ladder(T3Base):
    def test_sol_failure_escalates_to_opus(self):
        self.set_role("exec.sol", FAKES / "exec_bad.sh")
        self.submit()
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["attempt"], 2)
        self.assertEqual(st["exec"], "opus")
        self.assertEqual(st["attempts"]["2"]["exec"], "exec.opus")
        self.assertTrue((self.jobs / JOB_ID / "attempts" / "1" / "failure.json").is_file())

    def test_blocked_after_max_attempts(self):
        for role in ("exec.sol", "exec.opus", "exec.opus-high"):
            self.set_role(role, FAKES / "exec_bad.sh")
        self.submit(limits={"step_minutes": 45, "max_attempts": 2})
        st = self.run_to_end()
        self.assertEqual(st["state"], "blocked", st)
        self.assertEqual(st["attempt"], 2)
        self.assertEqual(len(self.failures()), 2)

    def test_same_signature_skips_a_rung(self):
        self.config["ladder"]["sol"] = ["sol", "opus", "opus-high", "replan"]
        for role in ("exec.sol", "exec.opus"):
            self.set_role(role, FAKES / "exec_bad.sh")
        self.submit(limits={"step_minutes": 45, "max_attempts": 5})
        st = self.run_to_end()
        self.assertEqual(st["state"], "blocked", st)
        self.assertEqual([st["attempts"][k]["exec"] for k in sorted(st["attempts"])],
                         ["exec.sol", "exec.opus"])
        self.assertIn("accept:A1", st["needs_human"])



class TestT3Review(T3Base):
    def test_review_fail_does_not_merge(self):
        self.set_role("review.opus", FAKES / "review_fail.sh")
        self.set_role("review.codex", FAKES / "review_fail.sh")
        self.submit(tier="L2", land="merge")
        st = self.run_to_end()
        self.assertNotEqual(st["state"], "done")
        self.assertFalse(st.get("merged"))
        self.assertEqual(self.main_sha(), self.base_sha)
        self.assertIn("review", [h["to"] for h in st["history"]])

    def test_review_wrong_sha_does_not_merge(self):
        self.set_role("review.opus", FAKES / "review_wrong_sha.sh")
        self.set_role("review.codex", FAKES / "review_wrong_sha.sh")
        self.submit(tier="L2", land="merge")
        st = self.run_to_end()
        self.assertNotEqual(st["state"], "done")
        self.assertEqual(self.main_sha(), self.base_sha)

    def test_review_uses_cross_family_reviewer_and_merges(self):
        self.submit(tier="L2", land="merge")
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["attempts"]["1"]["review"], "review.opus")  # sol → opus 复核
        self.assertEqual(st["review"]["verdict"], "PASS")
        self.assertEqual(st["review"]["sha"], st["sha"])
        self.assertEqual(self.main_sha(), st["sha"])

    def test_l1_touching_protected_paths_forces_review(self):
        self.set_role("exec.sol", FAKES / "exec_touch_tests.sh")
        self.submit(protect=["tests/**"])
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        self.assertIn("review", [h["to"] for h in st["history"]])

    def test_l1_without_protected_hit_skips_review(self):
        self.submit(protect=["tests/**"])
        st = self.run_to_end()
        self.assertNotIn("review", [h["to"] for h in st["history"]])

    def test_reviewer_modifying_worktree_fails_review(self):
        dirty = self.script("review_dirty.sh",
                            f"echo x > \"$1/evil.txt\"\nexec {shlex.quote(str(FAKES / 'review_pass.sh'))} \"$@\"\n")
        self.set_role("review.opus", dirty)
        self.set_role("review.codex", dirty)
        self.submit(tier="L2", land="merge", limits={"step_minutes": 45, "max_attempts": 1})
        st = self.run_to_end()
        self.assertEqual(st["state"], "blocked", st)
        self.assertEqual(st["review"]["verdict"], "FAIL")
        self.assertEqual(self.main_sha(), self.base_sha)


class TestT3Merge(T3Base):
    def test_fast_forward_merge_records_pre_merge_sha(self):
        self.submit(land="merge")
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        self.assertTrue(st["merged"])
        self.assertEqual(st["pre_merge_sha"], self.base_sha)
        self.assertEqual(self.main_sha(), st["sha"])
        self.assertTrue((self.repo / "DONE.txt").is_file(), "checked-out main should be fast-forwarded")
        import json
        res = json.loads((self.jobs / JOB_ID / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(res["pre_merge_sha"], self.base_sha)
        self.assertTrue(res["merged"])

    def test_base_moved_reruns_accept_then_merges(self):
        mover = self.exec_that("exec_move_base.sh",
                               f"echo other > {shlex.quote(str(self.repo))}/OTHER.txt\n"
                               f"git -C {shlex.quote(str(self.repo))} add OTHER.txt\n"
                               f"git -C {shlex.quote(str(self.repo))} commit -q -m moved\n")
        self.set_role("exec.sol", mover)
        self.submit(land="merge")
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        moved = self.git("rev-parse", "main^2").strip()  # 合并提交的第二父 = 前进后的 base
        self.assertEqual(st["pre_merge_sha"], moved)
        self.assertEqual(len([h for h in st["history"] if h["to"] == "accept"]), 2, st["history"])
        files = self.git("ls-tree", "--name-only", "main")
        self.assertIn("DONE.txt", files)
        self.assertIn("OTHER.txt", files)

    def test_merge_conflict_blocks_and_leaves_base(self):
        repo = shlex.quote(str(self.repo))
        conflict = self.script("exec_conflict.sh",
                               "echo mine > \"$1/README\"\n"
                               f"echo theirs > {repo}/README\ngit -C {repo} commit -q -am theirs\n"
                               "printf '%s\\n' '{\"summary\":\"ok\",\"unverified\":[]}' > \"$3\"\n")
        self.set_role("exec.sol", conflict)
        self.submit(land="merge", accept=[{"id": "A1", "cmd": "true"}])
        st = self.run_to_end()
        self.assertEqual(st["state"], "blocked", st)
        self.assertEqual(self.git("log", "-1", "--format=%s", "main").strip(), "theirs")
        self.assertFalse(st.get("merged"))

    def test_dirty_main_worktree_blocks(self):
        self.submit(land="merge")
        (self.repo / "README").write_text("local edit\n", encoding="utf-8")
        st = self.run_to_end()
        self.assertEqual(st["state"], "blocked", st)
        self.assertEqual(self.main_sha(), self.base_sha)
        self.assertIn("不干净", st["needs_human"])


if __name__ == "__main__":
    unittest.main()
