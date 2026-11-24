"""Sol 复查修复项的回归测试（编号对应 FINAL_REPORT“Sol 复查与修复”）。"""
import json
import os
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    from test_t2 import FAKES, JOB_ID, TERMINAL, T2Base, descendants, kill_quietly, pid_alive, write_json
    from test_t4 import Recorder
except ImportError:
    from tests.test_t2 import FAKES, JOB_ID, TERMINAL, T2Base, descendants, kill_quietly, pid_alive, write_json
    from tests.test_t4 import Recorder

ROOT = Path(__file__).resolve().parents[1]


class FixBase(T2Base):
    def setUp(self):
        super().setUp()
        self.config["repos"][f"local:{self.repo}"]["auto_merge"] = True
        self.save_config()

    def run_to_end(self, timeout=90):
        return self.tick_until(lambda s: s["state"] in TERMINAL, timeout=timeout, what="terminal")

    def mover(self):
        repo = shlex.quote(str(self.repo))
        return self.script("exec_move_base.sh",
                           f"{shlex.quote(str(FAKES / 'exec_ok.sh'))} \"$@\"\n"
                           f"echo other > {repo}/OTHER.txt\ngit -C {repo} add OTHER.txt\n"
                           f"git -C {repo} commit -q -m moved\n")

    def draft_submit(self, expected, **overrides):
        job = {"id": JOB_ID, "kind": "code", "title": "t", "user_words": "x", "tier": "L1", "host": "local",
               "repo": str(self.repo), "base": "main", "exec": "sol",
               "accept": [{"id": "A1", "cmd": "true"}], "land": "none", "deadline": "2099-01-01T00:00:00+00:00"}
        job.update(overrides)
        d = self.jobs / "_draft" / JOB_ID
        d.mkdir(parents=True)
        write_json(d / "job.json", job)
        (d / "brief.md").write_text("# t\n", encoding="utf-8")
        return self.amb("submit", JOB_ID, expected=expected)


class TestFix1And2BaseMoved(FixBase):
    def test_1_reaccept_really_runs_in_fresh_dir(self):
        counter = self.root / "accept_runs.log"
        self.set_role("exec.sol", self.mover())
        self.submit(land="merge", accept=[{"id": "A1", "cmd": f"echo run >> {shlex.quote(str(counter))} && test -f DONE.txt"}])
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(len(counter.read_text().splitlines()), 2, "合并后的重验收必须真实执行")
        acc = self.jobs / JOB_ID / "attempts" / "1" / "accept"
        self.assertTrue((acc / "01-A1" / "exit").is_file() and (acc / "02-A1" / "exit").is_file())
        self.assertEqual(st["accept_results"][0]["sha"], self.git("rev-parse", "main").strip())

    def test_1_duplicate_accept_ids_rejected(self):
        self.draft_submit(2, accept=[{"id": "A1", "cmd": "true"}, {"id": "A1", "cmd": "false"}])

    def test_2_merge_commit_is_rereviewed(self):
        self.set_role("exec.sol", self.mover())
        self.submit(tier="L2", land="merge")
        st = self.run_to_end()
        self.assertEqual(st["state"], "done", st)
        main = self.git("rev-parse", "main").strip()
        self.assertEqual(st["review"]["sha"], main)
        self.assertNotEqual(main, st["sha"])
        self.assertEqual(len([h for h in st["history"] if h["to"] == "review"]), 2, st["history"])


class TestFix3Protect(FixBase):
    def test_3_non_ascii_and_space_paths_match_protect(self):
        ex = self.script("exec_odd.sh", "mkdir -p \"$1/测试\"\necho x > \"$1/测试/a b.txt\"\n"
                         f"exec {shlex.quote(str(FAKES / 'exec_ok.sh'))} \"$@\"\n")
        self.set_role("exec.sol", ex)
        self.submit(protect=["测试/**"])
        st = self.run_to_end()
        self.assertIn("review", [h["to"] for h in st["history"]])


class TestFix4AcceptTamper(FixBase):
    def test_4_accept_that_modifies_tracked_files_fails(self):
        self.submit(accept=[{"id": "A1", "cmd": "echo hacked >> README"}],
                    limits={"step_minutes": 45, "max_attempts": 1})
        st = self.run_to_end()
        self.assertEqual(st["state"], "blocked", st)
        fail = json.loads((self.jobs / JOB_ID / "attempts" / "1" / "failure.json").read_text(encoding="utf-8"))
        self.assertEqual(fail["sig"], "accept:A1:worktree")


class TestFix7AcceptId(FixBase):
    def test_7_path_like_accept_id_rejected(self):
        for bad in ("../x", "a/b", "x" * 33, ""):
            with self.subTest(bad=bad):
                import shutil
                shutil.rmtree(self.jobs / "_draft", ignore_errors=True)
                if bad == "":
                    continue  # 空 ID 视为缺省（A1），允许
                self.draft_submit(2, accept=[{"id": bad, "cmd": "true"}])

    def test_7_plan_with_bad_accept_id_is_bad_output(self):
        plan = self.script("plan_bad_id.sh", "cat > \"$3\" <<'J'\n"
                           '{"subs":[{"title":"s","accept":[{"id":"../../x","cmd":"true"}],"depends_on":[]}]}\nJ\n')
        self.config["roles"]["plan.opus"] = {"cmd": f"{plan} {{workdir}} {{prompt_file}} {{out_file}}"}
        self.save_config()
        self.submit(tier="L3")
        st = self.run_to_end()
        fail = (self.jobs / JOB_ID / "attempts" / "1" / "failure.json").read_text(encoding="utf-8")
        self.assertIn("bad_output", fail)
        self.assertIn("验收 ID", fail)
        self.assertEqual(st["state"], "blocked")


class TestFix9RunnerKilled(FixBase):
    def test_9_live_child_after_runner_death_is_not_restarted(self):
        self.set_role("exec.sol", FAKES / "exec_hang.sh")
        self.submit()
        _, pid = self.wait_for_pid()
        children = descendants(pid)
        self.assertTrue(children)
        kill_quietly(pid)  # 只杀 runner，执行子进程仍活着
        for _ in range(5):
            self.tick()
        st = self.state()
        self.assertEqual(st["infra_restarts"], 0, st)
        self.assertEqual(st["state"], "exec")
        sleeps = [p for e in os.listdir("/proc") if e.isdigit() and pid_alive(p := int(e))
                  and "sleep" in Path(f"/proc/{e}/comm").read_text(errors="ignore")
                  and str(self.root) in os.readlink(f"/proc/{e}/cwd")]
        self.assertEqual(len(sleeps), 1, "执行者被重复启动")
        for c in children:
            kill_quietly(c)
        st = self.tick_until(lambda s: s.get("infra_restarts", 0) >= 1, timeout=30, what="infra restart")
        self.assertEqual(st["attempt"], 1)

    def test_9_launch_claim_is_exclusive(self):
        d = self.root / "step"
        d.mkdir()
        src = subprocess.run(["python3", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import amb; print(amb.RUNNER)",
                              str(ROOT)], capture_output=True, text=True, check=True).stdout
        (d / "runner.py").write_text(src)
        write_json(d / "spec.json", {"argv": ["sleep", "30"], "cwd": str(d), "stdin": None})
        procs = [subprocess.Popen(["python3", str(d / "runner.py"), "launch", str(d)], stdout=subprocess.PIPE, text=True)
                 for _ in range(4)]
        outs = {p.communicate(timeout=20)[0].strip() for p in procs}
        pid = int((d / "pid").read_text().split()[0])
        self.addCleanup(lambda: os.killpg(pid, signal.SIGKILL))
        sleeps = [e for e in os.listdir("/proc") if e.isdigit() and pid_alive(int(e))
                  and Path(f"/proc/{e}/comm").read_text(errors="ignore").strip() == "sleep"
                  and os.readlink(f"/proc/{e}/cwd") == str(d)]
        self.assertEqual(len(sleeps), 1)
        self.assertLessEqual(outs - {""}, {str(pid)})


class TestFix12And14Notify(FixBase):
    def test_12_redirect_not_followed(self):
        target = Recorder([])
        self.addCleanup(target.close)

        class Redir(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(307)
                self.send_header("Location", target.url)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Redir)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.config["webhook"] = {"url": f"http://127.0.0.1:{srv.server_port}/", "secret": "s", "header": "X-T"}
        self.save_config()
        self.submit()
        st = self.run_to_end()
        self.tick()
        self.assertEqual(target.requests, [], "重定向不得跟随（会泄漏密钥头）")
        self.assertIs(self.state()["notified"]["webhook"], False)

    def test_12_plain_http_remote_url_refused(self):
        self.config["webhook"] = {"url": "http://192.0.2.1/hook", "secret": "", "header": ""}
        self.save_config()
        self.submit()
        self.run_to_end()
        t0 = time.monotonic()
        self.tick()
        self.assertLess(time.monotonic() - t0, 5, "非 https 远端地址应直接拒绝，不发请求")
        self.assertIs(self.state()["notified"]["webhook"], False)

    def test_14_slow_endpoint_bounded_by_budget(self):
        class Slow(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                for _ in range(60):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.5)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.config["webhook"] = {"url": f"http://127.0.0.1:{srv.server_port}/", "secret": "", "header": ""}
        self.config["defaults"]["notify_budget_s"] = 2
        self.save_config()
        self.submit()
        self.run_to_end(timeout=120)
        t0 = time.monotonic()
        self.tick()
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIs(self.state()["notified"]["webhook"], False)


class TestFix13InstallCron(unittest.TestCase):
    def run_install(self, list_script, initial):
        d = tempfile.mkdtemp()
        store = Path(d) / "tab"
        store.write_text(initial, encoding="utf-8")
        fake = Path(d) / "crontab"
        fake.write_text(f"#!/usr/bin/env bash\nif [ \"$1\" = -l ]; then {list_script}; "
                        f"else cat > {shlex.quote(str(store))}; fi\n", encoding="utf-8")
        fake.chmod(0o755)
        r = subprocess.run(["bash", str(ROOT / "install-cron.sh")], env={**os.environ, "CRONTAB_BIN": str(fake)},
                           capture_output=True, text=True)
        return r, store.read_text(encoding="utf-8")

    def test_13_read_failure_aborts_without_writing(self):
        r, tab = self.run_install("echo 'crontab: permission denied' >&2; exit 1", "0 1 * * * /keep.sh\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(tab, "0 1 * * * /keep.sh\n")

    def test_13_no_crontab_means_empty(self):
        r, tab = self.run_install("echo 'no crontab for u' >&2; exit 1", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(tab.splitlines(), ["* * * * * flock -n ~/amb/.lock python3 ~/amb/amb.py tick"])


if __name__ == "__main__":
    unittest.main()
