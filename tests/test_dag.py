"""Dependency planning and local runner integration for code and documents."""
import json
import shlex
from pathlib import Path

import amb

try:
    from test_t5 import T5Base
except ImportError:
    from tests.test_t5 import T5Base


class TestDAG(T5Base):
    def plan(self, *nodes):
        subs = [{"title": title, "depends_on": deps, "checklist": [f"check {title}"]} for title, deps in nodes]
        script = self.script("plan_graph.sh", "cat > \"$3\" <<'PLAN'\n" + json.dumps({"subs": subs}) + "\nPLAN\n")
        self.set_role("plan.fable", script)

    def test_plan_validation(self):
        job = {"host": "local", "repo": str(self.repo)}
        for deps, expected in (
            (([1], []), None),
            (([], []), None),
            (([1], [0]), "依赖环"),
            (([0], []), "其他子任务"),
            (([2], []), "其他子任务"),
            (([True], []), "其他子任务"),
            (([1, 1], []), "重复"),
        ):
            with self.subTest(deps=deps):
                plan = {"subs": [{"title": "node", "checklist": ["check"], "depends_on": d} for d in deps]}
                error = amb.check_plan(plan, job, self.config)
                if expected is None:
                    self.assertIsNone(error)
                else:
                    self.assertIn(expected, error)

    def run_parallel_graph(self, kind):
        self.plan(("join", [1, 2]), ("left", []), ("right", []), ("separate", []))
        barrier = self.root / "barrier"
        barrier.mkdir()
        executor = self.script("exec_graph.sh", f"""barrier={shlex.quote(str(barrier))}
title=$(sed -n '1s/^# //p' "$2")
case "$title" in
    left|right)
        touch "$barrier/$title"
        for i in $(seq 1 100); do
            if test -f "$barrier/left" && test -f "$barrier/right"; then break; fi
            sleep 0.05
        done
        test -f "$barrier/left" && test -f "$barrier/right" || exit 9
        ;;
    join)
        test -f left && test -f right && test ! -f separate || exit 8
        ;;
esac
printf '%s\n' "$title" > "$title"
printf '%s\n' '{{"summary":"created artifact"}}' > "$3"
""")
        self.set_role("exec.sol", executor)
        parent = self.submit(
            kind=kind, tier="L3", land="merge" if kind == "code" else "none",
            accept=[{"id": "A1", "cmd": "test -f left && test -f right && test -f join && test -f separate"}],
            checklist=["all artifacts"],
        )
        final = self.run_to_end(timeout=20)
        self.assertEqual(final["state"], "done", final)
        join, left, right, separate = [self.state(child) for child in final["subs"]]
        self.assertEqual([s["state"] for s in (join, left, right, separate)], ["done"] * 4)
        self.assertTrue((barrier / "left").is_file() and (barrier / "right").is_file())
        wt = Path(final["wt"])
        for source in (join, left, right, separate):
            self.git("merge-base", "--is-ancestor", source["sha"], final["sha"], cwd=wt)
        self.assertEqual(len(self.history_to(final, "review")), 1)
        prompt = (self.jobs / parent["id"] / "attempts/1/review/prompt.md").read_text()
        self.assertIn("check left", prompt)
        self.assertIn("check right", prompt)
        return final, [join, left, right, separate]

    def test_independent_code_workers_overlap_and_integrate_all_leaves(self):
        final, _ = self.run_parallel_graph("code")
        self.assertTrue(final["merged"])
        self.assertEqual(self.git("rev-parse", "main").strip(), final["sha"])

    def test_doc_workers_overlap_and_use_a_single_integration_review(self):
        final, children = self.run_parallel_graph("doc")
        self.assertEqual(final["review"]["verdict"], "PASS")
        self.assertTrue(all(not self.history_to(child, "review") for child in children))

    def test_conflicting_independent_outputs_block_integration(self):
        self.plan(("left", []), ("right", []))
        executor = self.script("exec_conflict.sh", """sed -n '1s/^# //p' "$2" > conflict.txt
printf '%s\n' '{"summary":"changed conflict.txt"}' > "$3"
""")
        self.set_role("exec.sol", executor)
        self.submit(tier="L3", land="merge", accept=[{"id": "A1", "cmd": "test -f conflict.txt"}])
        final = self.run_to_end(timeout=20)
        self.assertEqual((final["state"], final["attempt"]), ("blocked", 1))
        self.assertIn("dag:merge", final["needs_human"])
        self.assertIn("conflict.txt", final["needs_human"])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base_sha)
