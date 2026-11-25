"""H200 派单约束黑盒测试：自动附加 h200_rules.md、重开时列出已成功的 H200 子任务。"""
import json
import unittest

try:
    from test_t2 import JOB_ID, TERMINAL
    from test_t5 import T5Base
except ImportError:
    from tests.test_t2 import JOB_ID, TERMINAL
    from tests.test_t5 import T5Base

MARK = "## H200 测试规则（编排器自动附加）"
RULES = MARK + "\n1. 压着占卡跑，不停占卡。\n2. 重开不重跑已成功的 H200 子任务。\n"
H200_BRIEF = "# 最小闭环\n\n在两台 H200（任务 <REDACTED_ID>）上跑通最小闭环，训练若干步后保存结果并写明证据路径。\n"
PLAIN_BRIEF = "# 普通任务\n\n在仓库里追加 DONE.txt，并用现成的验收命令核对结果，证据写进 RESULT.md。\n"


class H200Base(T5Base):
    def setUp(self):
        super().setUp()
        self.rules = self.root / "h200_rules.md"
        self.rules.write_text(RULES, encoding="utf-8")
        self.env["AMB_H200_RULES"] = str(self.rules)

    def prompt(self, job_id=JOB_ID, step="exec"):
        return (self.jobs / job_id / f"attempts/1/{step}/prompt.md").read_text(encoding="utf-8")

    def plan_script(self, titles):
        """规划出 len(titles) 个串行子任务，标题按给定值。"""
        body = ("repo_line=$(grep -m 1 '^REPO: ' -- \"$2\")\n"
                "python3 - \"${repo_line#REPO: }\" \"$3\" <<'PY'\n"
                "import json, sys\n"
                "repo, out = sys.argv[1:]\n"
                f"titles = {json.dumps(titles, ensure_ascii=False)}\n"
                "subs = [{'title': t, 'host': 'local', 'repo': repo, 'exec': 'sol',\n"
                "         'accept': [{'id': 'A1', 'cmd': 'test -f DONE.txt'}], 'depends_on': [i - 1] if i else []}\n"
                "        for i, t in enumerate(titles)]\n"
                "json.dump({'subs': subs}, open(out, 'w', encoding='utf-8'), ensure_ascii=False)\n"
                "PY\n")
        self.set_role("plan.opus", self.script("plan_titles.sh", body))


class TestH200RulesAttached(H200Base):
    def test_hit_attaches_rules_once_to_exec_prompt(self):
        self.submit(brief=H200_BRIEF)
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        prompt = self.prompt()
        self.assertEqual(prompt.count(MARK), 1, prompt)
        self.assertIn("压着占卡跑，不停占卡。", prompt)
        self.assertLess(prompt.index("## 用户原话"), prompt.index(MARK))
        self.assertLess(prompt.index(MARK), prompt.index("WORKDIR: "))

    def test_keyword_only_in_user_words_also_attaches(self):
        self.submit(brief=PLAIN_BRIEF, user_words="先别停占卡，直接跑")
        self.run_to_end()
        self.assertEqual(self.prompt().count(MARK), 1)

    def test_no_keyword_no_rules(self):
        self.submit(brief=PLAIN_BRIEF)
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        self.assertNotIn(MARK, self.prompt())

    def test_missing_rules_file_is_skipped(self):
        self.rules.unlink()
        self.submit(brief=H200_BRIEF)
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        self.assertNotIn(MARK, self.prompt())

    def test_rules_already_in_brief_are_not_repeated(self):
        self.submit(brief=H200_BRIEF + "\n" + RULES)
        self.run_to_end()
        self.assertEqual(self.prompt().count(MARK), 1)

    def test_planner_and_every_subtask_get_rules_once(self):
        self.plan_script(["H200 最小闭环", "整理文档"])
        self.submit(tier="L3", brief=H200_BRIEF)
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        plan = self.prompt(step="plan")
        self.assertEqual(plan.count(MARK), 1, plan)
        self.assertLess(plan.index(MARK), plan.index("## 父任务验收（不可修改）"))
        self.assertEqual(len(st["subs"]), 2)
        for cid in st["subs"]:
            brief = (self.jobs / cid / "brief.md").read_text(encoding="utf-8")
            self.assertNotIn(MARK, brief)                       # 规则在构造提示时附加，不写进 brief
            self.assertEqual(self.prompt(cid).count(MARK), 1, cid)


class TestH200Reopen(H200Base):
    def test_reopen_lists_done_h200_subtasks_only(self):
        self.plan_script(["H200 最小闭环", "整理文档"])
        self.submit(tier="L3", brief=H200_BRIEF)
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        gpu_sub, doc_sub = st["subs"]
        nid = self.amb("reopen", JOB_ID, "--reason", "补一段说明").stdout.strip()
        brief = (self.jobs / nid / "brief.md").read_text(encoding="utf-8")
        self.assertEqual(brief.count("## 已成功的 H200 子任务"), 1, brief)
        note = brief[brief.index("## 已成功的 H200 子任务"):]
        self.assertIn("不重跑", note)
        self.assertIn(f"- {gpu_sub}｜H200 最小闭环｜结果 {self.jobs / gpu_sub / 'result.md'}", note)
        self.assertIn("｜工作目录 local:", note)
        self.assertNotIn(doc_sub, note)
        self.assertLess(brief.index("## 上次结果"), brief.index("## 已成功的 H200 子任务"))

    def test_reopen_without_done_h200_subtasks_adds_nothing(self):
        self.submit(brief=H200_BRIEF)                           # 快速单：没有子任务
        self.run_to_end()
        nid = self.amb("reopen", JOB_ID, "--reason", "again").stdout.strip()
        self.assertNotIn("已成功的 H200 子任务", (self.jobs / nid / "brief.md").read_text(encoding="utf-8"))

    def test_reopen_of_plain_job_adds_nothing(self):
        self.plan_script(["GPU 冒烟", "整理文档"])              # 子任务像 GPU 活，但父任务与 H200 无关
        self.submit(tier="L3", brief=PLAIN_BRIEF)
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        nid = self.amb("reopen", JOB_ID, "--reason", "again").stdout.strip()
        self.assertNotIn("已成功的 H200 子任务", (self.jobs / nid / "brief.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
