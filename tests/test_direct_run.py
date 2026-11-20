"""Exercise handoff and inbox against a controllable command-line agent."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path

DIRECT_RUN = Path(__file__).resolve().parents[1] / "amb-direct-run"
SESSION = "9a14cc41-f180-47bf-87d8-41a1f5ed76db"
FAKE_CODEX = """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

session = "9a14cc41-f180-47bf-87d8-41a1f5ed76db"
prompt = sys.stdin.read()
argv = sys.argv[1:]
if "resume" in argv:
    assert argv[-2:] == [session, "-"], argv
    Path("resumed.md").write_text(prompt)
else:
    if Path.cwd().name == "prepare":
        Path("ready").touch()
        while not Path("release").exists():
            time.sleep(0.02)
    elif Path.cwd().name == "publish":
        assert Path("../prepare/exit").read_text().strip() == "0"
        assert str(Path.cwd().parent / "prepare") in prompt
        Path(".first-pid").write_text(str(os.getpid()))
        Path(".first-pid").replace("first-pid")
        while not Path("release").exists():
            time.sleep(0.02)
        assert Path("first-pid").read_text() == str(os.getpid())
        assert json.loads(Path("inbox").read_text())["message"] == "追加：检查结果"
        Path("first-finished").touch()
    print(json.dumps({"type": "thread.started", "thread_id": session}), flush=True)
Path("out.md").write_text("done")
"""


class TestDirectRun(unittest.TestCase):
    def setUp(self):
        resources = self.enterContext(ExitStack())
        self.workdir = Path(resources.enter_context(tempfile.TemporaryDirectory()))
        self.base = self.workdir / "jobs"
        self.bin = self.workdir / "bin"
        self.bin.mkdir()
        fake = self.bin / "codex"
        fake.write_text(FAKE_CODEX)
        fake.chmod(0o755)
        self.prompt = self.workdir / "prompt.md"
        self.prompt.write_text("Execute the direct task.")
        self.env = {**os.environ, "AMB_DIRECT_ROOT": str(self.base),
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}

    def invoke(self, *args):
        return subprocess.run([DIRECT_RUN, *args], text=True, capture_output=True,
                              env=self.env, timeout=10)

    def begin(self, name, *args):
        process = subprocess.Popen([DIRECT_RUN, "run", name, "--prompt", self.prompt, *args],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=self.env)
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)

    def wait_for(self, path, seconds=8):
        deadline = time.monotonic() + seconds
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.is_file(), f"{path} was not written")

    def test_handoff_and_late_inbox_use_same_session(self):
        prepare = self.begin("prepare")
        previous = self.base / "prepare"
        self.wait_for(previous / "ready")
        publish = self.begin("publish", "--after", "prepare")
        current = self.base / "publish"
        self.wait_for(current / "status.json")
        self.assertEqual(json.loads((current / "status.json").read_text())["phase"], "waiting")
        self.assertFalse((current / "first-pid").exists())

        (previous / "release").touch()
        prepare.communicate(timeout=8)
        self.assertEqual(prepare.returncode, 0)
        self.wait_for(current / "first-pid")
        pid = int((current / "first-pid").read_text())
        sent = self.invoke("send", "publish", "追加：检查结果")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        os.kill(pid, 0)
        self.assertFalse((current / "exit").exists())
        (current / "release").touch()

        _, stderr = publish.communicate(timeout=8)
        self.assertEqual(publish.returncode, 0, stderr)
        self.assertTrue((current / "first-finished").is_file())
        self.assertEqual((current / "exit").read_text().strip(), "0")
        self.assertIn("1. 追加：检查结果", (current / "resumed.md").read_text())
        status = json.loads((current / "status.json").read_text())
        self.assertEqual((status["phase"], status["turn"], status["session_id"]), ("done", 2, SESSION))
        self.assertNotEqual(self.invoke("send", "publish", "too late").returncode, 0)
        self.assertNotEqual(self.invoke("run", "publish", "--prompt", self.prompt).returncode, 0)
        self.assertEqual((current / "exit").read_text().strip(), "0")

    def test_failed_prerequisite_writes_exit_without_starting_agent(self):
        previous = self.base / "legacy"
        previous.mkdir(parents=True)
        (previous / "exit").write_text("7\n")
        result = self.invoke("run", "after-failure", "--prompt", self.prompt, "--after", "legacy")
        current = self.base / "after-failure"
        self.assertEqual(result.returncode, 1)
        self.assertEqual((current / "exit").read_text().strip(), "1")
        self.assertFalse((current / "out.md").exists())
        self.assertIn("legacy", json.loads((current / "status.json").read_text())["error"])
