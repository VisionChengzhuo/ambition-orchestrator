"""amb send: append user words to running jobs and their subtasks."""
import json

try:
    from test_t2 import FAKES, JOB_ID
    from test_t5 import T5Base
except ImportError:
    from tests.test_t2 import FAKES, JOB_ID
    from tests.test_t5 import T5Base


class TestSend(T5Base):
    def test_send_reaches_running_child_inbox_and_queued_child_prompt(self):
        self.set_role("exec.sol", FAKES / "exec_hang.sh")
        self.submit(tier="L3")
        parent = self.tick_until(lambda s: s["state"] == "subs")
        first, second = parent["subs"]
        self.wait_for_pid(first)
        run = self.state(first)["run"]
        self.assertTrue(run.get("inbox"))
        self.assertIn("inbox.ack", (self.jobs / first / "attempts/1/exec/prompt.md").read_text())
        out = self.amb("send", JOB_ID, "追加：顺便写 NOTE.md").stdout
        self.assertIn(first, out)
        self.assertIn(second, out)
        with open(f"{run['dir']}/inbox", encoding="utf-8") as f:
            inbox = [json.loads(l) for l in f]
        self.assertEqual([m["message"] for m in inbox], ["追加：顺便写 NOTE.md"])
        self.amb("send", "unknown", "x", expected=3)
        self.amb("send", JOB_ID, "  ", expected=2)
        check = ("import amb, json; job = json.load(open(amb.jobs_dir(%r, 'job.json'))); "
                 "print('\\n'.join(amb.appended_section(job)))" % second)
        import subprocess, sys
        from tests.test_t2 import AMB
        text = subprocess.run([sys.executable, "-c", check], cwd=AMB.parent, env=self.env,
                              capture_output=True, encoding="utf-8").stdout
        self.assertIn("1. 追加：顺便写 NOTE.md", text)
        self.amb("cancel", JOB_ID)
        self.amb("send", JOB_ID, "late", expected=2)


def test_unconfirmed_reminders_route_and_deduplicate(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import patch
    import amb
    monkeypatch.setenv("AMB_ROOT", str(tmp_path))
    pending = [
        {"at": "1970-01-01T00:00:00Z", "message": "已确认"},
        {"at": "1970-01-01T00:00:00Z", "message": "带队者发", "sender": "J-lead"},
        {"at": "1970-01-01T00:01:40Z", "message": "Ambition 发，刚满五分钟"},
        {"at": "1970-01-01T00:01:41Z", "message": "尚未满五分钟"},
        {"at": "1970-01-01T00:00:00Z", "message": "编排器通知", "from": "amb"},
    ]
    paths = {"/run/inbox": "\n".join(map(json.dumps, pending)), "/run/inbox.ack": "1"}
    fake_host = SimpleNamespace(get=paths.__getitem__)
    job = {"id": "J-child", "lead_parent": "J-lead"}
    state = {"attempt": 2, "run": {"step": "exec", "host": "local", "dir": "/run", "started": 1, "inbox": True}}
    with patch.object(amb, "Host", return_value=fake_host), patch.object(amb.time, "time", return_value=400), \
         patch.object(amb, "write_inbox", return_value=True) as leader, patch.object(amb.amb_events, "emit") as event:
        amb.remind_unconfirmed(job["id"], job, state, {})
        amb.remind_unconfirmed(job["id"], job, state, {})
        assert state["run"]["inbox_reminded"] == [2, 3]
        leader.assert_called_once()
        event.assert_called_once()
        assert "第 2 条" in leader.call_args.args[2]["message"]
        assert "第 3 条" in event.call_args.args[4]


def test_kml_receipts_keep_legacy_runs_and_retry_paths_working(tmp_path):
    import amb

    host = amb.Host({"hosts": {"kml-dev": {}}, "defaults": {"ssh_timeout_s": 5}}, "kml-dev")
    old = tmp_path / "exec"
    old.mkdir()
    (old / "inbox").write_text('{"message":"one"}\n{"message":"two"}\n')
    (old / "inbox.ack").write_text("1\n")
    assert amb.inbox_count(host, str(old)) == (2, 1)

    new = str(tmp_path / "exec-retry-1")
    prompt = amb.move_inbox_prompt(amb.inbox_instructions(str(old)), str(old), host, new, host)
    assert f"{new}/receipts/inbox.ack" in prompt
    assert f"{new}/receipts/.inbox.ack.tmp" in prompt
    amb.copy_step_inbox(host, str(old), host, new)
    assert amb.inbox_count(host, new) == (2, 1)

    receipt = tmp_path / "exec-retry-1" / "receipts" / "inbox.ack"
    receipt.write_text("2\n")
    assert amb.inbox_count(host, new) == (2, 2)
    receipt.unlink()
    receipt.symlink_to(old / "inbox.ack")
    assert amb.inbox_count(host, new) == (2, 0)
