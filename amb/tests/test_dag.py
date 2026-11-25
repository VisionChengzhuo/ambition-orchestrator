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
        self.set_role("plan.opus", script)

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
        if test "{kind}" = doc; then
            for report in RESULT.md out.md; do
                grep -qx 'left report' "$report" && grep -qx 'right report' "$report" || exit 7
            done
            python3 -c 'import json, sys; sys.exit(json.load(open("deliver.json"))["files"] != ["common", "left", "right"])' || exit 6
        fi
        ;;
esac
if test "{kind}" = doc && test "$title" != join; then
    for report in RESULT.md out.md; do
        printf '%s report\n' "$title" > "$report"
    done
    printf '{{"files":["common","%s"]}}\n' "$title" > deliver.json
fi
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
        for report in ("RESULT.md", "out.md"):
            lines = (Path(final["wt"]) / report).read_text().splitlines()
            self.assertCountEqual([line for line in lines if line.endswith(" report")],
                                  ["left report", "right report", "separate report"])
        manifest = json.loads((Path(final["wt"]) / "deliver.json").read_text())
        self.assertCountEqual(manifest["files"], ["common", "left", "right", "separate"])

    def test_doc_delivery_manifest_preserves_metadata(self):
        manifest = self.repo / "delivery-manifest.json"
        base = self.git("rev-parse", "HEAD").strip()
        heads = {}
        for branch in ("left", "right"):
            self.git("switch", "-qc", branch, base)
            manifest.write_text(json.dumps({"files": ["shared/*", branch], branch: {"ready": True}}))
            self.git("add", manifest.name)
            self.git("commit", "-qm", branch)
            heads[branch] = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "-q", "--detach", base)
        amb.merge_plan_heads(amb.Host(self.config, "local"), str(self.repo), heads, doc=True)
        self.assertEqual(json.loads(manifest.read_text()),
                         {"files": ["shared/*", "left", "right"],
                          "left": {"ready": True}, "right": {"ready": True}})

    def test_doc_lowercase_result_add_add_keeps_both_versions(self):
        base = self.git("rev-parse", "HEAD").strip()
        heads = {}
        for branch in ("left", "right"):
            self.git("switch", "-qc", branch, base)
            for name in ("result.md", "reply.md"):
                (self.repo / name).write_text(f"{branch} {name}\n")
            self.git("add", "result.md", "reply.md")
            self.git("commit", "-qm", branch)
            heads[branch] = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "-q", "--detach", base)
        amb.merge_plan_heads(amb.Host(self.config, "local"), str(self.repo), heads, doc=True)
        for name in ("result.md", "reply.md"):
            self.assertEqual((self.repo / name).read_text(), f"left {name}\n\n\n---\n\nright {name}\n")

    def test_doc_report_edits_keep_both_versions_and_business_conflicts_block(self):
        report = self.repo / "RESULT.md"
        business = self.repo / "business.md"
        report.write_text("common report\n")
        business.write_text("common content\n")
        self.git("add", "RESULT.md", "business.md")
        self.git("commit", "-qm", "common")
        base = self.git("rev-parse", "HEAD").strip()
        heads = {}
        for branch in ("left", "right"):
            self.git("switch", "-qc", branch, base)
            report.write_text(f"{branch} report\n")
            self.git("commit", "-qam", branch)
            heads[branch] = self.git("rev-parse", "HEAD").strip()
        host = amb.Host(self.config, "local")
        self.git("switch", "-q", "--detach", base)
        amb.merge_plan_heads(host, str(self.repo), heads, doc=True)
        self.assertEqual(report.read_text(), "left report\n\n\n---\n\nright report\n")
        for branch in heads:
            self.git("switch", "-q", branch)
            business.write_text(f"{branch} content\n")
            self.git("commit", "-qam", "update business")
            heads[branch] = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "-q", "--detach", base)
        with self.assertRaises(amb.StepError) as raised:
            amb.merge_plan_heads(host, str(self.repo), heads, doc=True)
        self.assertEqual(raised.exception.sig, "dag:merge")
        self.assertIn("business.md", raised.exception.detail)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), heads["left"])
        self.assertEqual(self.git("status", "--porcelain"), "")

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
