import ast
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


AMB = Path(__file__).resolve().parents[1] / "amb.py"
FAKES = Path(__file__).resolve().parent / "fakes"
JOB_ID = "J-1120-01"


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestT1CLI(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        home = self.root / "home"
        home.mkdir()
        self.env = {
            "AMB_ROOT": str(self.root),
            "HOME": str(home),
            "PATH": os.defpath,
            "PYTHONUTF8": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.jobs = self.root / "jobs"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (
            ("init", "--quiet", "--initial-branch=main"),
            (
                "-c", "user.name=Amb Test", "-c", "user.email=amb@example.invalid",
                "commit", "--quiet", "--allow-empty", "-m", "Initial commit",
            ),
        ):
            subprocess.run(
                ["git", *args], cwd=self.repo, env=self.env, check=True,
                capture_output=True, timeout=10,
            )
        self.repo_config = {"auto_merge": False, "push": False}
        self.config = {
            "hosts": {"local": {"ssh": None, "work_root": str(self.root / "work")}},
            "roles": {
                "exec.sol": {
                    "cmd": f"{shlex.quote(str(FAKES / 'exec_ok.sh'))} "
                    "{workdir} {prompt_file} {out_file}",
                },
            },
            "ladder": {"sol": ["sol", "opus", "replan"]},
            "reviewer": {"sol": "review.opus"},
            "repos": {f"local:{self.repo}": self.repo_config},
            "defaults": {"step_minutes": 45, "max_attempts": 3, "ssh_timeout_s": 20},
            "webhook": {"url": "", "secret": "", "header": ""},
            "feishu": {"cmd": "", "enabled": False},
            "linear": {"api_key_file": "", "dry_run": True},
            "heartbeat": str(self.root / "heartbeat"),
        }
        write_json(self.root / "config.json", self.config)

    def amb(self, *args, expected=0):
        result = subprocess.run(
            ["python3", str(AMB), *args], cwd=self.root, env=self.env,
            capture_output=True, encoding="utf-8", timeout=15,
        )
        self.assertTrue(AMB.is_file(), f"CLI script missing: {AMB}")
        self.assertEqual(
            result.returncode, expected,
            f"amb {args!r}\nstdout: {result.stdout}\nstderr: {result.stderr}",
        )
        return result

    def draft(self, *, omit=(), **overrides):
        job = {
            "id": JOB_ID,
            "kind": "code",
            "title": "T1 fixture",
            "user_words": "运行本地验收",
            "tier": "L1",
            "host": "local",
            "repo": str(self.repo),
            "base": "main",
            "exec": "sol",
            "accept": [{"id": "A1", "cmd": "true", "timeout_s": 10}],
            "checklist": [],
            "protect": [],
            "land": "none",
            "linear": "",
            "limits": {"step_minutes": 45, "max_attempts": 3},
            "deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "parent": None,
            "depends_on": [],
        }
        job.update(overrides)
        for field in omit:
            del job[field]
        path = self.jobs / "_draft" / JOB_ID
        path.mkdir(parents=True)
        write_json(path / "job.json", job)
        (path / "brief.md").write_text("# T1 fixture\n", encoding="utf-8")
        return path

    def assert_submit_rejected(self, **draft_options):
        draft = self.draft(**draft_options)
        result = self.amb("submit", JOB_ID, expected=2)
        self.assertTrue(result.stderr.strip(), "expected a validation reason on stderr")
        self.assertTrue((draft / "job.json").is_file())
        self.assertFalse((self.jobs / JOB_ID).exists())

    def test_new_prints_id_and_creates_draft(self):
        result = self.amb(
            "new", "--once", "--kind", "code", "--tier", "L1", "--host", "local",
            "--repo", str(self.repo),
        )
        job_id = result.stdout.strip()
        self.assertRegex(job_id, r"\AJ-[0-9]{4}-[0-9]{2}\Z")
        draft = self.jobs / "_draft" / job_id
        job = json.loads((draft / "job.json").read_text(encoding="utf-8"))
        for field, value in {
            "id": job_id, "kind": "code", "tier": "L1", "host": "local",
            "repo": str(self.repo),
        }.items():
            with self.subTest(field=field):
                self.assertEqual(job[field], value)
        self.assertTrue((draft / "brief.md").is_file())

    def test_submit_rejects_missing_user_words(self):
        self.assert_submit_rejected(omit=("user_words",))

    def test_submit_rejects_empty_user_words(self):
        self.assert_submit_rejected(user_words="")

    def test_submit_accepts_code_without_acceptance(self):
        self.draft(omit=("accept",))
        self.amb("submit", JOB_ID)
        self.assertTrue((self.jobs / JOB_ID / "state.json").is_file())

    def test_submit_rejects_merge_without_acceptance(self):
        self.repo_config["auto_merge"] = True
        write_json(self.root / "config.json", self.config)
        self.assert_submit_rejected(accept=[], land="merge")

    def test_new_accepts_explicit_executor(self):
        jid = self.amb("new", "--once", "--kind", "doc", "--tier", "L1", "--exec", "opus").stdout.strip()
        job = json.loads((self.jobs / "_draft" / jid / "job.json").read_text())
        self.assertEqual(job["exec"], "opus")
        # autoresearch_design A1：AutoResearch 总指挥要能显式派 Fable 单（默认仍不是 Fable）
        jid = self.amb("new", "--once", "--kind", "doc", "--exec", "fable").stdout.strip()
        self.assertEqual(json.loads((self.jobs / "_draft" / jid / "job.json").read_text())["exec"], "fable")
        self.amb("new", "--exec", "astra", expected=2)

    def test_submit_rejects_code_without_repo(self):
        self.assert_submit_rejected(repo="")

    def test_submit_rejects_unknown_host(self):
        self.assert_submit_rejected(host="unconfigured-host")

    def test_submit_rejects_past_deadline(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        self.assert_submit_rejected(deadline=past.isoformat())

    def test_submit_rejects_doc_without_checklist(self):
        self.assert_submit_rejected(kind="doc", tier="L2", repo="", accept=[])

    def test_submit_accepts_doc_l1(self):
        self.draft(kind="doc", tier="L1", repo="", accept=[], checklist=["Proofread"])
        self.amb("submit", JOB_ID)

    def test_submit_rejects_doc_merge_even_with_auto_merge_enabled(self):
        self.repo_config["auto_merge"] = True
        write_json(self.root / "config.json", self.config)
        self.assert_submit_rejected(
            kind="doc", tier="L2", accept=[], checklist=["Proofread"], land="merge",
        )

    def test_submit_rejects_merge_without_auto_merge(self):
        self.assert_submit_rejected(land="merge")

    def test_submit_moves_valid_task_to_queued_and_records_history(self):
        draft = self.draft()
        brief = (draft / "brief.md").read_bytes()
        self.amb("submit", JOB_ID)

        self.assertFalse(draft.exists())
        job = self.jobs / JOB_ID
        self.assertTrue((job / "job.json").is_file())
        self.assertEqual((job / "brief.md").read_bytes(), brief)
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        queued = [entry for entry in state["history"] if entry["to"] == "queued"]
        self.assertEqual(len(queued), 1)
        self.assertTrue({"ts", "from", "to", "why"} <= queued[0].keys())

    def stored_job(self, job_id):
        job = self.jobs / job_id
        job.mkdir(parents=True)
        write_json(job / "job.json", {"id": job_id, "kind": "code", "exec": "sol"})
        now = datetime.now(timezone.utc).isoformat()
        write_json(job / "state.json", {
            "state": "done",
            "attempt": 2,
            "exec": "sol",
            "history": [{"ts": now, "from": "accept", "to": "done", "why": "pass"}],
        })
        return job

    def test_status_lists_tasks_and_filters_by_id(self):
        other_id = "J-1120-02"
        self.stored_job(JOB_ID)
        self.stored_job(other_id)

        lines = self.amb("status").stdout.splitlines()
        self.assertCountEqual(
            [line.split()[0] for line in lines if line.strip()], [JOB_ID, other_id],
        )
        for job_id in (JOB_ID, other_id):
            with self.subTest(job_id=job_id):
                output = self.amb("status", job_id).stdout.strip()
                self.assertEqual(len(output.splitlines()), 1)
                self.assertEqual(output.split(maxsplit=4)[:4], [job_id, "done", "2", "sol"])
                self.assertEqual(len(output.split(maxsplit=4)), 5)

    def test_result_prints_stored_markdown(self):
        job = self.stored_job(JOB_ID)
        markdown = "# J-1120-01\n结论：本地验收通过。\n"
        (job / "result.md").write_text(markdown, encoding="utf-8")
        self.assertEqual(self.amb("result", JOB_ID).stdout.rstrip("\n"), markdown.rstrip("\n"))

    def test_result_returns_three_for_missing_job(self):
        self.amb("result", JOB_ID, expected=3)


class TestT1Imports(unittest.TestCase):
    def test_amb_has_no_third_party_python_dependencies(self):
        local_modules = {
            "amb", "amb_events", "amb_metrics", "amb_router", "amb_sentinel", "amb_speed",
            "ambition_job_deliver", "card_text",
        }
        allowed = sys.stdlib_module_names | local_modules
        for module in local_modules:
            script = AMB.with_name(module + ".py")
            with self.subTest(module=module):
                tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
                invalid = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        imports = ["." * node.level + (node.module or "")]
                    else:
                        continue
                    for name in imports:
                        if name.partition(".")[0] not in allowed:
                            invalid.append(f"line {node.lineno}: {name}")
                self.assertEqual(invalid, [], "third-party imports")
