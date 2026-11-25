"""挂靠单（linear_attach）的进度提示不再要求加 --ask；其他单保持原样。"""
import unittest

try:
    from test_h200_rules import H200Base, PLAIN_BRIEF
except ImportError:
    from tests.test_h200_rules import H200Base, PLAIN_BRIEF

ASK = "需用户决定时加 --ask 并注明要决定哪件事。"
ATTACH = "不要加 --ask；首句写阻塞和要童成卓决定的事，并写进 goal_gap。"


class TestAttachProgressHint(H200Base):
    def test_attached_job_prompts_drop_ask(self):
        self.submit(brief=PLAIN_BRIEF, tier="L2", linear_attach=True)
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        for step in ("exec", "review"):
            prompt = self.prompt(step=step)
            self.assertIn(ATTACH, prompt)
            self.assertNotIn("加 --ask 并注明", prompt)

    def test_plain_job_keeps_ask(self):
        self.submit(brief=PLAIN_BRIEF, tier="L2")
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        for step in ("exec", "review"):
            prompt = self.prompt(step=step)
            self.assertIn(ASK, prompt)
            self.assertNotIn(ATTACH, prompt)


if __name__ == "__main__":
    unittest.main()
