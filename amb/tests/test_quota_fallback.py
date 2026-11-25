"""Claude quota refusals use the configured Codex role in the same attempt."""
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import amb


QUOTA = "You've hit your 5-hour limit · resets 1pm (UTC)\n"


@pytest.mark.parametrize("step", ["exec", "repair"])
def test_quota_fallback_preserves_current_attempt(step):
    with tempfile.TemporaryDirectory() as folder:
        d = Path(folder)
        (d / "exit").write_text("1\n")
        (d / "stdout").write_text(QUOTA)
        (d / "prompt.md").write_text(f"Read {d}/inbox\n")
        run = {"step": step, "local": folder, "dir": folder, "argv": ["claude", "-p"],
               "stdin": str(d / "prompt.md"), "cwd": "/work/current", "timeout": 90,
               "inbox": True, "sha": "candidate", "collected": True}
        state = {"state": "exec", "run": run, "attempt": 2, "attempts": {}, "history": []}
        cfg = {"hosts": {"local": {}},
               "roles": {"exec.astra": {"cmd": "codex exec -C {workdir} -o {out_file}"}}}
        host = Mock(ssh=False)
        host.name = "local"
        host.get.side_effect = lambda path: "2\n" if path.endswith(".ack") else "message\n"
        with patch.object(amb, "start_step") as launch:
            amb.finish_collected_step("job", {}, state, cfg, host)
        args, kwargs = launch.call_args
        assert args[5:9] == (step, ["codex", "exec", "-C", "/work/current", "-o",
                                    f"{folder}-quota-fallback/out"], "/work/current", 90)
        assert kwargs["quota_fallback"] and kwargs["sha"] == "candidate"
        assert state["attempt"] == 2
        host.put.assert_any_call(f"{folder}-quota-fallback/inbox.ack", "2\n")


@pytest.mark.parametrize("launcher,status,output", [
    ("claude", "0", QUOTA),
    ("codex", "1", QUOTA),
    ("claude", "1", "other error"),
])
def test_other_results_do_not_trigger_fallback(launcher, status, output):
    assert not amb.claude_quota_refusal({"argv": [launcher]}, {"exit": status, "stdout": output})
