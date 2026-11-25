"""Claude subscription exhaustion defers the current step."""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import amb


QUOTA = "You've hit your 5-hour limit · resets 1pm (UTC)"
OBSERVED = datetime(2026, 11, 22, 10, 54, tzinfo=timezone.utc)
RESET = OBSERVED.replace(hour=13, minute=0)


@pytest.mark.parametrize("step", ["exec", "repair"])
def test_quota_wait_and_resume_same_step(tmp_path, step):
    directory = tmp_path / step
    directory.mkdir()
    prompt = directory / "prompt.md"
    prompt.write_text(f"Read {directory}/inbox")
    (directory / "stdout").write_text(QUOTA + "\n")
    (directory / "exit").write_text("1")
    (directory / "inbox").write_text('{"message":"keep going"}\n')
    (directory / "inbox.ack").write_text("1\n")
    run = {"step": step, "dir": str(directory), "local": str(directory), "host": "local",
           "argv": ["claude", "-p"], "stdin": str(prompt), "cwd": "/existing/worktree",
           "timeout": 3600, "sha": "candidate", "inbox": True, "collected": True,
           "collected_at": OBSERVED.timestamp()}
    state = {"state": "exec", "attempt": 2, "exec": "opus-high", "history": [], "attempts": {}, "run": run}
    host = Mock(ssh=False)
    host.get.side_effect = lambda path: Path(path).read_text()
    cfg, job = {}, {}
    amb.finish_collected_step("job", job, state, cfg, host)
    assert run["quota_reset"] == RESET.timestamp()
    assert not run.get("collected")
    assert state["attempt"] == 2 and state["exec"] == "opus-high"
    assert state["state"] == "exec"

    with patch.object(amb, "Host", return_value=host), patch.object(amb, "start_step") as start:
        with patch.object(amb.time, "time", return_value=RESET.timestamp() - 1):
            amb.poll_step("job", job, state, cfg)
        start.assert_not_called()
        host.poll.assert_not_called()
        with patch.object(amb.time, "time", return_value=RESET.timestamp()):
            amb.poll_step("job", job, state, cfg)

    new_dir = str(directory) + "-quota-1"
    start.assert_called_once_with("job", job, state, cfg, host, step, ["claude", "-p"],
                                  "/existing/worktree", 3600, stdin=new_dir + "/prompt.md",
                                  stepdir=new_dir, localdir=new_dir, retry=0, quota_retries=1,
                                  sha="candidate", inbox=True)
    host.put.assert_any_call(new_dir + "/prompt.md", f"Read {new_dir}/inbox")
    host.put.assert_any_call(new_dir + "/inbox.ack", "1\n")


def test_quota_detection_ignores_normal_failures_and_handles_midnight():
    run = {"step": "exec", "argv": ["claude"]}
    texts = {"exit": "1", "stdout": "", "stderr": QUOTA.replace("1pm", "12:30am")}
    assert amb.claude_quota_reset(run, texts, OBSERVED).isoformat() == "2026-11-23T00:30:00+00:00"
    assert amb.claude_quota_reset(run, {**texts, "stderr": "network error"}, OBSERVED) is None
    assert amb.claude_quota_reset(run, {**texts, "exit": "0"}, OBSERVED) is None
    assert amb.claude_quota_reset({**run, "argv": ["codex"]}, texts, OBSERVED) is None
