"""T2 黑盒测试：步骤运行器与 L1。

全部通过 subprocess 调用 ``python3 amb.py``；AMB_ROOT 指向临时目录，主机用 local，
仓库是临时 git 仓库，模型用 tests/fakes/*.sh（或测试运行时在临时目录生成的脚本）。

测试中用到、但 PLAN.md 未写死数值的约定：
- ``limits.step_minutes`` 允许小数（例如 0.05 分钟 = 3 秒），用于快速触发超时；
- 失败记录文件名为 ``failure.json``，位于任务目录（一般为 ``attempts/<n>/``）之内；
- 执行步骤的进程号写在任务目录下名为 ``pid`` 的文件里（PLAN E3：``attempts/<n>/exec/pid``）。
"""

import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


AMB = Path(__file__).resolve().parents[1] / "amb.py"
FAKES = Path(__file__).resolve().parent / "fakes"
JOB_ID = "J-0101-01"
TERMINAL = {"done", "failed", "blocked"}


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def role_cmd(script):
    return f"{shlex.quote(str(script))} {{workdir}} {{prompt_file}} {{out_file}}"


def pid_alive(pid):
    """进程存在且不是僵尸。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            stat = fh.read()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


def descendants(pid):
    """返回 pid 的全部子孙进程号（读 /proc）。"""
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as fh:
                ppid = int(fh.read().rpartition(")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


def kill_quietly(pid, sig=signal.SIGKILL):
    try:
        os.kill(pid, sig)
    except (OSError, ProcessLookupError):
        pass


class T2Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.root = Path(temporary.name)
        self.addCleanup(temporary.cleanup)
        self.addCleanup(self.kill_leftovers)
        home = self.root / "home"
        home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {
            "AMB_ROOT": str(self.root),
            "HOME": str(home),
            "PATH": f"{self.bin}:{os.defpath}",
            "PYTHONUTF8": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Amb Test",
            "GIT_AUTHOR_EMAIL": "amb@example.invalid",
            "GIT_COMMITTER_NAME": "Amb Test",
            "GIT_COMMITTER_EMAIL": "amb@example.invalid",
        }
        self.jobs = self.root / "jobs"
        self.work = self.root / "work"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet", "--initial-branch=main")
        (self.repo / "README").write_text("fixture\n", encoding="utf-8")
        self.git("add", "README")
        self.git("commit", "--quiet", "-m", "Initial commit")
        self.base_sha = self.git("rev-parse", "HEAD").strip()
        self.config = {
            "hosts": {"local": {"ssh": None, "work_root": str(self.work)}},
            "roles": {
                "exec.sol": {"cmd": role_cmd(FAKES / "exec_ok.sh")},
                "exec.opus": {"cmd": role_cmd(FAKES / "exec_ok.sh")},
                "exec.opus-high": {"cmd": role_cmd(FAKES / "exec_ok.sh")},
                "review.opus": {"cmd": role_cmd(FAKES / "review_pass.sh")},
                "review.codex": {"cmd": role_cmd(FAKES / "review_pass.sh")},
                "ask": {"cmd": role_cmd(FAKES / "exec_ok.sh")},
            },
            "ladder": {"sol": ["sol", "opus", "replan"], "opus": ["opus", "opus-high", "replan"]},
            "reviewer": {"sol": "review.opus", "opus": "review.codex", "opus-high": "review.codex"},
            "repos": {f"local:{self.repo}": {"auto_merge": False, "push": False}},
            "defaults": {"step_minutes": 45, "max_attempts": 3, "ssh_timeout_s": 2},
            "webhook": {"url": "", "secret": "", "header": ""},
            "feishu": {"cmd": "", "enabled": False},
            "linear": {"api_key_file": "", "dry_run": True},
            "heartbeat": str(self.root / "heartbeat"),
        }
        self.save_config()

    # ------------------------------------------------------------ 工具
    def save_config(self):
        write_json(self.root / "config.json", self.config)

    def set_role(self, role, script):
        self.config["roles"][role] = {"cmd": role_cmd(script)}
        self.save_config()

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
            capture_output=True, encoding="utf-8", timeout=20,
        ).stdout

    def script(self, name, body):
        """在临时目录生成一个可执行 bash 脚本（不放进 tests/fakes）。"""
        path = self.root / "scripts" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def amb(self, *args, expected=0, timeout=60):
        self.assertTrue(AMB.is_file(), f"CLI script missing: {AMB}")
        result = subprocess.run(
            ["python3", str(AMB), *args], cwd=self.root, env=self.env,
            capture_output=True, encoding="utf-8", timeout=timeout,
        )
        if expected is not None:
            self.assertEqual(
                result.returncode, expected,
                f"amb {args!r}\nstdout: {result.stdout}\nstderr: {result.stderr}",
            )
        return result

    def tick(self, expected=0):
        return self.amb("tick", expected=expected)

    def submit(self, job_id=JOB_ID, brief="# T2 fixture\n", **overrides):
        job = {
            "id": job_id,
            "kind": "code",
            "title": "T2 fixture",
            "user_words": "在仓库里追加 DONE.txt",
            "tier": "L1",
            "host": "local",
            "repo": str(self.repo),
            "base": "main",
            "exec": "sol",
            "accept": [{"id": "A1", "cmd": "test -f DONE.txt", "timeout_s": 30}],
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
        draft = self.jobs / "_draft" / job_id
        draft.mkdir(parents=True)
        write_json(draft / "job.json", job)
        (draft / "brief.md").write_text(brief, encoding="utf-8")
        self.amb("submit", job_id)
        self.assertTrue((self.jobs / job_id / "state.json").is_file())
        return job

    def state(self, job_id=JOB_ID):
        return json.loads((self.jobs / job_id / "state.json").read_text(encoding="utf-8"))

    def history_to(self, state, to):
        return [h for h in state.get("history", []) if h.get("to") == to]

    def failures(self, job_id=JOB_ID):
        return sorted((self.jobs / job_id).rglob("failure.json"))

    def pid_files(self, job_id=JOB_ID):
        return sorted(p for p in (self.jobs / job_id).rglob("pid") if p.is_file())

    def read_pid(self, path):
        return int(path.read_text(encoding="utf-8").split()[0])

    def tick_until(self, predicate, timeout=60, interval=0.3, job_id=JOB_ID, what="condition"):
        deadline = time.monotonic() + timeout
        while True:
            self.tick()
            st = self.state(job_id)
            if predicate(st):
                return st
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}; last state:\n"
                          f"{json.dumps(st, ensure_ascii=False, indent=1)}")
            time.sleep(interval)

    def wait_for_pid(self, job_id=JOB_ID, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for path in self.pid_files(job_id):
                try:
                    pid = self.read_pid(path)
                except (OSError, ValueError, IndexError):
                    continue
                if pid_alive(pid):
                    return path, pid
            self.tick()
            time.sleep(0.3)
        self.fail(f"no live pid file under {self.jobs / job_id}; state: {self.state(job_id)}")

    def kill_leftovers(self):
        """清理测试遗留进程（exec_hang 的 sleep 3600 等）。"""
        if not self.jobs.exists():
            return
        for path in self.jobs.rglob("pid"):
            try:
                pid = self.read_pid(path)
            except (OSError, ValueError, IndexError):
                continue
            for child in descendants(pid):
                kill_quietly(child)
            kill_quietly(pid)
        # 兜底：工作目录在本测试临时目录下的残留进程。
        marker = str(self.root)
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == os.getpid():
                continue
            try:
                cwd = os.readlink(f"/proc/{entry}/cwd")
            except OSError:
                continue
            if cwd.startswith(marker):
                kill_quietly(int(entry))


class TestT2Lifecycle(T2Base):
    def test_exec_ok_with_passing_accept_reaches_done(self):
        self.submit()
        st = self.tick_until(lambda s: s["state"] in TERMINAL, what="terminal state")
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["attempt"], 1)
        states = [h["to"] for h in st["history"]]
        self.assertIn("exec", states)
        self.assertIn("accept", states)
        self.assertLess(states.index("exec"), states.index("accept"))
        self.assertEqual(states[-1], "done")
        self.assertEqual(self.failures(), [])

        # 执行者在独立 worktree 里工作，编排器统一提交到 amb/<id>/a1 分支。
        wt = self.work / JOB_ID / "a1" / "wt"
        self.assertTrue((wt / "DONE.txt").is_file(), f"worktree missing: {wt}")
        branch_sha = self.git("rev-parse", f"refs/heads/amb/{JOB_ID}/a1").strip()
        self.assertEqual(st["sha"], branch_sha)
        self.assertNotEqual(branch_sha, self.base_sha)
        self.assertIn("DONE.txt", self.git("show", "--name-only", "--format=", branch_sha))
        self.assertEqual(st.get("base_sha"), self.base_sha)
        self.assertEqual(self.git("status", "--porcelain", "--", "DONE.txt", cwd=wt).strip(), "")
        # 主工作区和 base 不受影响（land=none）。
        self.assertFalse((self.repo / "DONE.txt").exists())
        self.assertEqual(self.git("rev-parse", "refs/heads/main").strip(), self.base_sha)
        self.assertEqual(self.git("status", "--porcelain").strip(), "")

    def test_failing_accept_never_done_and_writes_failure_json(self):
        self.submit(accept=[{"id": "A1", "cmd": "exit 7", "timeout_s": 30}],
                    limits={"step_minutes": 45, "max_attempts": 3})
        seen = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            self.tick()
            st = self.state()
            seen.append(st["state"])
            self.assertNotEqual(st["state"], "done", st)
            if self.failures() and (st["state"] in TERMINAL or st["attempt"] > 1):
                break
            time.sleep(0.3)
        st = self.state()
        self.assertNotEqual(st["state"], "done", st)
        self.assertNotIn("done", [h["to"] for h in st["history"]])
        failures = self.failures()
        self.assertTrue(failures, f"no failure.json; states seen: {seen}")
        first = self.jobs / JOB_ID / "attempts" / "1" / "failure.json"
        self.assertIn(first, failures)
        json.loads(first.read_text(encoding="utf-8"))  # 必须是合法 JSON
        self.assertIn("A1", first.read_text(encoding="utf-8"))
        self.assertTrue(st["attempt"] > 1 or st["state"] in {"blocked", "failed"}, st)

    def test_exec_hang_times_out_is_killed_and_counts_one_failure(self):
        self.set_role("exec.sol", FAKES / "exec_hang.sh")
        self.set_role("exec.opus", FAKES / "exec_hang.sh")
        self.submit(limits={"step_minutes": 0.05, "max_attempts": 3})
        _, pid = self.wait_for_pid()
        procs = [pid, *descendants(pid)]
        st = self.tick_until(
            lambda s: bool(self.failures()) or s["attempt"] > 1 or s["state"] in TERMINAL,
            timeout=90, what="timeout failure",
        )
        self.assertNotEqual(st["state"], "done")
        self.assertEqual(len(self.failures()), 1, self.failures())
        self.assertTrue((self.jobs / JOB_ID / "attempts" / "1" / "failure.json").is_file())
        self.assertTrue(st["attempt"] == 2 or st["state"] in {"blocked", "failed"}, st)
        time.sleep(0.5)
        alive = [p for p in procs if pid_alive(p)]
        self.assertEqual(alive, [], "hung exec processes were not killed")

    def test_repeated_ticks_during_exec_start_exec_only_once(self):
        self.set_role("exec.sol", FAKES / "exec_hang.sh")
        self.submit()
        path, pid = self.wait_for_pid()
        for _ in range(5):
            self.tick()
        st = self.state()
        self.assertEqual(st["state"], "exec", st)
        self.assertEqual(st["attempt"], 1)
        self.assertEqual(len(self.history_to(st, "exec")), 1, st["history"])
        pairs = [(h["from"], h["to"]) for h in st["history"]]
        self.assertEqual(len(pairs), len(set(pairs)), f"duplicate history: {st['history']}")
        self.assertEqual(len(self.pid_files()), 1, self.pid_files())
        self.assertEqual(self.read_pid(path), pid)
        self.assertTrue(pid_alive(pid))
        hang_procs = [p for p in [pid, *descendants(pid)]
                      if "sleep" in Path(f"/proc/{p}/comm").read_text(errors="ignore")]
        self.assertEqual(len(hang_procs), 1, "exec started more than once")

    def test_unreachable_host_waits_without_consuming_attempt(self):
        # 假 ssh：模拟连接超时（ssh 的连接失败退出码为 255）；同时地址取 TEST-NET 黑洞。
        fake_ssh = self.bin / "ssh"
        fake_ssh.write_text(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {shlex.quote(str(self.root / 'ssh_calls.log'))}\n"
            "echo 'ssh: connect to host 192.0.2.1 port 22: Connection timed out' >&2\n"
            "exit 255\n",
            encoding="utf-8",
        )
        fake_ssh.chmod(0o755)
        for tool in ("scp", "rsync"):
            (self.bin / tool).write_text(fake_ssh.read_text(encoding="utf-8"), encoding="utf-8")
            (self.bin / tool).chmod(0o755)
        self.config["hosts"]["blackhole"] = {"ssh": "192.0.2.1", "work_root": str(self.work)}
        self.save_config()
        self.submit(host="blackhole")
        for _ in range(3):
            self.tick()
        st = self.state()
        self.assertEqual(st["state"], "waiting_host", st)
        self.assertEqual(st["attempt"], 1)
        self.assertTrue(st.get("unreachable_since"), st)
        self.assertEqual(self.history_to(st, "exec"), [])
        self.assertEqual(len(self.history_to(st, "waiting_host")), 1, st["history"])
        self.assertEqual(self.failures(), [])
        self.assertEqual(list(self.work.rglob("DONE.txt")) if self.work.exists() else [], [])

        # 主机恢复可达（映射改为本机）后自动回到 queued 并开工。
        self.config["hosts"]["blackhole"] = {"ssh": None, "work_root": str(self.work)}
        self.save_config()
        st = self.tick_until(lambda s: s["state"] in TERMINAL, what="recovery")
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["attempt"], 1)
        states = [h["to"] for h in st["history"]]
        self.assertIn("queued", states[states.index("waiting_host"):])

    def test_killed_step_process_triggers_infra_restart_without_attempt(self):
        self.set_role("exec.sol", FAKES / "exec_hang.sh")
        self.submit()
        _, pid = self.wait_for_pid()
        before = self.state()
        self.assertEqual(before["attempt"], 1)
        children = descendants(pid)
        kill_quietly(pid)          # 先杀记录的进程，exit 文件就不会被写出
        for child in children:
            kill_quietly(child)
        deadline = time.monotonic() + 10
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(pid_alive(pid))

        st = self.tick_until(lambda s: s.get("infra_restarts", 0) >= 1, timeout=30,
                             what="infra restart")
        self.assertEqual(st["attempt"], 1, st)
        self.assertEqual(st["infra_restarts"], 1, st)
        self.assertNotIn(st["state"], {"done", "failed", "blocked"}, st)
        self.assertEqual(self.failures(), [], "infra restart must not record a failure")
        # 重启后会重新启动执行步骤。
        _, new_pid = self.wait_for_pid()
        self.assertNotEqual(new_pid, pid)
        self.assertEqual(self.state()["attempt"], 1)

    def test_brief_with_shell_metacharacters_is_not_executed(self):
        pwn = self.root / "pwned"
        payloads = [
            f"$(touch {pwn}-dollar)",
            f"`touch {pwn}-backtick`",
            f"'; touch {pwn}-single; '",
            f"\"; touch {pwn}-double; \"",
            f"EOF\ntouch {pwn}-eof\nEOF",
            f"PROMPT\ntouch {pwn}-prompt\nPROMPT",
            f"AMB_EOF\ntouch {pwn}-amb\nAMB_EOF",
            f"END\ntouch {pwn}-end\nEND",
            f"'EOF'\n$(touch {pwn}-quoted)\n",
            f"\\\"; touch {pwn}-escaped #",
        ]
        text = "\n".join(payloads)
        copy = self.root / "prompt_copy.txt"
        capture = self.script(
            "exec_capture.sh",
            f"cp -- \"$2\" {shlex.quote(str(copy))}\n"
            f"exec {shlex.quote(str(FAKES / 'exec_ok.sh'))} \"$@\"\n",
        )
        self.set_role("exec.sol", capture)
        self.submit(
            title=f"标题 {payloads[0]} {payloads[2]}",
            user_words=f"用户原话：{text}",
            brief=f"# 说明\n{text}\n",
        )
        st = self.tick_until(lambda s: s["state"] in TERMINAL, what="terminal state")
        self.assertEqual(st["state"], "done", st)
        leaked = sorted(p.name for p in self.root.glob("pwned*"))
        self.assertEqual(leaked, [], "user text was executed by a shell")
        self.assertTrue(copy.is_file(), "exec role was not invoked")
        prompt = copy.read_text(encoding="utf-8")
        self.assertIn(payloads[0], prompt)
        self.assertIn(f"touch {pwn}-eof", prompt)


class TestT2Robustness(T2Base):
    def test_deadline_passed_marks_failed(self):
        self.set_role("exec.sol", FAKES / "exec_hang.sh")
        deadline = datetime.now(timezone.utc) + timedelta(seconds=4)
        self.submit(deadline=deadline.isoformat())
        while datetime.now(timezone.utc) < deadline + timedelta(seconds=0.5):
            self.tick()
            time.sleep(0.3)
        st = self.tick_until(lambda s: s["state"] in TERMINAL, timeout=20, what="deadline")
        self.assertEqual(st["state"], "failed", st)

    def test_one_broken_job_does_not_stop_others(self):
        broken = "J-0101-09"
        bdir = self.jobs / broken
        bdir.mkdir(parents=True)
        write_json(bdir / "job.json", {"id": broken, "kind": "code", "host": "local",
                                       "repo": str(self.root / "no-such-repo"), "exec": "sol",
                                       "accept": [{"id": "A1", "cmd": "true"}],
                                       "user_words": "x"})
        (bdir / "state.json").write_text("{not json", encoding="utf-8")
        self.submit()
        deadline = time.monotonic() + 60
        while True:
            self.amb("tick", expected=None)
            st = self.state()
            if st["state"] in TERMINAL:
                break
            self.assertLess(time.monotonic(), deadline, st)
            time.sleep(0.3)
        self.assertEqual(st["state"], "done", st)


if __name__ == "__main__":
    unittest.main()
