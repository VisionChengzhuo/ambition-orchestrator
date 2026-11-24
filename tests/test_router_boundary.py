import json
from argparse import Namespace
from unittest.mock import patch

import amb
import amb_router
import pytest


def test_tool_surface_is_coordination_only():
    argv = amb_router.claude_args(["/usr/bin/python3", "/srv/amb.py", "_router"])
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "mcp__kml-public-search" in argv
    assert {item["name"] for item in amb_router.tool_list()} == {
        "dispatch", "children", "inbox", "ack", "wait", "direction", "child_action"
    }
    with pytest.raises(ValueError):
        amb_router.validate_call("Bash", {"command": "printf canary"})
    with pytest.raises(ValueError):
        amb_router.validate_call("dispatch", {
            "title": "child", "assignment": "calculate", "goal": "37", "thinking": "delegate",
            "command": "printf canary",
        })


def test_stale_broker_and_unrelated_child_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("AMB_ROOT", str(tmp_path))
    jid = "J-TEST"
    amb.write_json(amb.jobs_dir(jid, "job.json"), {"id": jid, "exec": "fable"})
    amb.write_json(amb.jobs_dir(jid, "state.json"), {
        "attempt": 2, "state": "exec", "run": {"step": "repair", "router_session": "current"},
    })
    with pytest.raises(amb.AmbError, match="no longer active"):
        amb.router_context(Namespace(id=jid, attempt=2, session="stale"), {})
    with pytest.raises(amb.AmbError, match="只能操作自己派出的子单"):
        amb.router_call(Namespace(id=jid, attempt=2, session="current"), {}, "child_action",
                        {"id": "J-UNRELATED", "action": "cancel", "reason": "test"})


def test_dispatch_scopes_child_prompt_and_inherits_test_transitively(tmp_path, monkeypatch):
    monkeypatch.setenv("AMB_ROOT", str(tmp_path))
    parent = amb.skeleton("J-ROOT", "ask", words="派单后自己汇总")
    parent["test"] = True
    amb.write_json(amb.jobs_dir(parent["id"], "job.json"), parent)
    # Model old validation data: this previously saved intermediate missed the test marker.
    legacy = amb.skeleton("J-LEGACY", "doc", words="旧子单")
    legacy["lead_parent"] = parent["id"]
    amb.write_json(amb.jobs_dir(legacy["id"], "job.json"), legacy)
    amb.save(legacy["id"], amb.initial_state(legacy))
    cfg = amb.load_config()
    cfg["linear"].update(auto_create=True, dry_run=False)
    with patch.object(amb, "linear_app") as linear, patch.object(amb.subprocess, "Popen"):
        result = amb.router_dispatch(legacy, cfg, {
            "title": "验证数字", "assignment": "只计算 7、11 和 19 的和。",
            "goal": "返回求和结果", "thinking": "把算术交给 Sol",
        })
    linear.assert_not_called()
    child, state = amb.load(result["id"])
    assert child["test"] is True
    assert not child.get("linear")
    assert child["user_words"] == "只计算 7、11 和 19 的和。"
    prompt = amb.exec_prompt(child, state, cfg, amb.Host(cfg, "local"), "", str(tmp_path))
    assert parent["user_words"] not in prompt


def test_stream_result_uses_final_result_only():
    stdout = "\n".join(map(json.dumps, [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": '{"summary":"partial"}'}]}},
        {"type": "result", "result": '{"summary":"finished"}'},
    ]))
    assert json.loads(amb.router_final_text(stdout)) == {"summary": "finished"}


def test_router_only_append_stays_with_router_until_explicitly_broadcast(tmp_path, monkeypatch):
    monkeypatch.setenv("AMB_ROOT", str(tmp_path))
    root, child = {"id": "J-ROOT"}, {"id": "J-WORK", "lead_parent": "J-ROOT"}
    amb.write_json(amb.jobs_dir("J-ROOT", "job.json"), root)
    amb.write_json(amb.jobs_dir("J-WORK", "job.json"), child)
    amb.append_jsonl(amb.jobs_dir("J-ROOT", "append.jsonl"), {"message": "你再派一个 Sol"})
    amb.append_jsonl(amb.jobs_dir("J-ROOT", "append.jsonl"), {"message": "共享需求", "broadcast": True})
    amb.append_jsonl(amb.jobs_dir("J-WORK", "append.jsonl"), {"message": "单独给执行者"})
    assert amb.appended_words(root) == ["你再派一个 Sol", "共享需求"]
    assert amb.appended_words(child) == ["单独给执行者", "共享需求"]


@pytest.mark.parametrize("step", ["exec", "repair"])
def test_router_prompt_omits_worker_speed_hint(tmp_path, monkeypatch, step):
    monkeypatch.setenv("AMB_ROOT", str(tmp_path))
    cfg = amb.load_config()
    cfg["roles"]["exec.fable"] = {"cmd": "claude -p"}
    host = amb.Host(cfg, "local")
    job = amb.skeleton("J-ROOT", "doc", words="把计算交给子单")
    job.update(mode="lead", exec="fable")
    state = amb.initial_state(job)
    state.update(wt=str(tmp_path), sha="abc", speed_hint=True)
    if step == "repair":
        with patch.object(host, "put") as put, patch.object(amb, "start_step"), \
                patch.object(amb, "leader_snapshot", return_value={}):
            amb.start_repair(job["id"], job, state, cfg, host, ["再核实一次"])
        prompt = put.call_args_list[0].args[1]
    else:
        prompt = amb.exec_prompt(job, state, cfg, host, "", str(tmp_path))
    assert "实质工作、测试和修复都由子单完成" in prompt
    assert amb.amb_speed.SPEED_HINT not in prompt

    worker = {**job, "mode": "", "exec": "sol"}
    assert amb.amb_speed.SPEED_HINT in amb.exec_prompt(
        worker, {**state, "exec": "sol"}, cfg, host, "", str(tmp_path)
    )


@pytest.mark.parametrize("step", ["exec", "repair"])
@pytest.mark.parametrize("session", ["current", None])
def test_lost_router_process_never_restarts_with_worker_tools(tmp_path, monkeypatch, step, session):
    monkeypatch.setenv("AMB_ROOT", str(tmp_path))
    cfg = amb.load_config()
    host = amb.Host(cfg, "local")
    job = amb.skeleton("J-ROOT", "doc", words="派单")
    job.update(mode="lead", exec="fable")
    state = amb.initial_state(job)
    clock = amb.time.time()
    run = state["run"] = {"step": step, "host": "local", "dir": str(tmp_path),
                          "local": str(tmp_path), "cwd": str(tmp_path), "stdin": "prompt.md",
                          "argv": ["claude", "-p"], "started": clock, "began": clock,
                          "timeout": 600, "router_session": session}
    state["started_at"] = amb.now()
    with patch.object(amb, "Host", return_value=host), patch.object(host, "run"), \
            patch.object(host, "poll", return_value={"alive": False, "exit": None}), \
            patch.object(host, "launch", return_value="123") as launch:
        if session is None:
            with pytest.raises(amb.StepError) as exc:
                amb.poll_step(job["id"], job, state, cfg)
            assert exc.value.sig == "lead:launcher"
            launch.assert_not_called()
        else:
            amb.poll_step(job["id"], job, state, cfg)
            argv = launch.call_args.args[1]
            assert argv[:2] == run["argv"]
            assert argv[argv.index("--tools") + 1] == ""
            assert "--strict-mcp-config" in argv
            assert session in argv[argv.index("--mcp-config") + 1]
