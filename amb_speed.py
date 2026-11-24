"""Background speed review. The model returns proposals; amb owns task mutations."""
import fcntl
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from stat import S_ISREG

import amb_metrics

TASK_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
HOUR = 3600
PERIOD = 900
DIRECT_IDLE_SECONDS = 600
DIRECT_IDLE_KEY = "无写入"
LIMIT_BYTES = 8192
CLAUDE = "/opt/amb/claude-full"
SPEED_HINT = """## 速度
- 目标是最快把可验证的结果交到用户手上。
- 互不依赖、各自要等的步骤（远程命令、下载、测试、多机检查）放到后台同时跑，不串行等待。
- 只有在至少两块工作互不依赖、各需数分钟以上、且不改同一文件时，才开子代理；否则自己做。
- 不做 brief 没要求的探索、复核和润色；结果可交付就停。"""


def speed_hint_enabled():
    return os.environ.get("AMB_SPEED_HINT", "1") != "0"


def age_minutes(now, start):
    return round(max(0, now - start) / 60, 1)


def stage_minutes(amb, history, now):
    stages = []
    for entry, successor in zip(history, [*history[1:], {"ts": None}]):
        try:
            begin = amb.parse_time(entry["ts"]).timestamp()
            end = amb.parse_time(successor["ts"]).timestamp() if successor["ts"] else now
            stages.append((entry["to"], age_minutes(end, begin), begin, bool(successor["ts"])))
        except (KeyError, ValueError, TypeError):
            continue
    return stages


def amb_metadata(amb, cfg, now):
    jobs = {}
    for jid in amb.list_jobs():
        try:
            jobs[jid] = amb.load(jid)
        except amb.AmbError:
            continue
    states = {jid: st.get("state") for jid, (_, st) in jobs.items()}
    delegated = {}
    for child, (job, state) in jobs.items():
        if job.get("lead_parent"):
            delegated.setdefault(job["lead_parent"], []).append((job, state))
    current, completed = [], {}
    day = int(now // 86400) * 86400
    for jid, (job, st) in jobs.items():
        stages = stage_minutes(amb, st.get("history") or [], now)
        for stage, minutes, begin, ended in stages:
            if begin >= day and ended:
                completed.setdefault(stage, []).append(minutes)
        if states[jid] in amb.TERMINAL:
            continue
        attempt = st.get("attempt", 0)
        models = (st.get("attempts") or {}).get(str(attempt)) or {}
        state = states[jid]
        role = ((models.get("plan") or cfg.get("planner")) if state == "planning"
                else (cfg.get("reviewer") or {}).get(st.get("exec"), "review.opus") if state == "review"
                else ("ask" if job.get("kind") == "ask" else "exec." + (st.get("exec") or "sol")))
        run = st.get("run") or {}
        signature = st.get("last_sig") or ""
        matching_failures = sum(
            (amb.read_json(path, {}) or {}).get("sig") == signature
            for path in Path(amb.jobs_dir(jid, "attempts")).glob("*/failure.json")
        ) if signature else 0
        exit_path = Path(run["local"], "exit") if run.get("local") else None
        ready = [child for child in st.get("subs") or []
                 if states.get(child) == "queued" and not jobs[child][1].get("run")
                 and all(states.get(dep) == "done" for dep in jobs[child][0].get("depends_on") or [])]
        lead = {}
        if amb.is_leader(job, st, cfg):
            children = delegated.get(jid, [])
            started = amb_metrics.timestamp(st.get("started_at"))
            dispatches = [at for child, child_st in children
                          if (at := amb_metrics.timestamp(amb.job_timing(child, child_st)["dispatched_at"])) is not None]
            lead = {
                "lead_first_dispatch_seconds": round(max(0, min(dispatches) - started), 1)
                if started is not None and dispatches else None,
                "lead_started_seconds_ago": round(max(0, now - started), 1) if started is not None else None,
                "lead_children_active": sum(child_st.get("state") not in amb.TERMINAL for _, child_st in children),
            }
        current.append({
            **lead,
            "id": jid, "tier": job.get("tier"), "kind": job.get("kind"), "role": role,
            "model": (models.get("review_model") if state == "review" else models.get("plan_model")
                      if state == "planning" else models.get("exec_model")) or (amb.role_model(cfg, role) if role else None),
            "stage": state, "stage_minutes": stages[-1][1] if stages else None,
            "history_minutes": [[phase, minutes] for phase, minutes, _, _ in stages[-12:]],
            "attempt": attempt, "rung": st.get("rung"), "parent": job.get("parent"),
            "depends_on": job.get("depends_on") or [], "ready_children": ready,
            "last_signature": signature if re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", signature) else None,
            "matching_failures": matching_failures, "reopened_from": job.get("reopened_from"),
            "run_has_exit": exit_path.is_file() if exit_path else None,
        })
    medians = {stage: statistics.median(durations) for stage, durations in completed.items()}
    return current, medians


def file_stat(path):
    try:
        return path.stat()
    except OSError:
        return None


def direct_last_write(directory):
    """Use task file mtimes, excluding inbox traffic and runner internals."""
    latest = 0
    for root, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if name != ".git" and not name.startswith(".agent-message-")]
        if root == os.fspath(directory):
            files = set(files) - {"inbox", "inbox.ack", ".inbox.lock", ".inbox.ack.tmp"}
        for name in files:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if S_ISREG(info.st_mode):
                latest = max(latest, info.st_mtime)
    return latest


def direct_metadata(amb, direct_root, work, now, sample=False):
    previous = amb.read_json(work / "direct-samples.json", {}) or {}
    current, samples = [], {}
    for directory in sorted(direct_root.glob("*/")):
        if not TASK_ID.fullmatch(directory.name):
            continue
        prompt = file_stat(directory / "prompt.md")
        if not prompt or (directory / "exit").exists():
            continue
        out, err = file_stat(directory / "out.md"), file_stat(directory / "err.log")
        log = file_stat(directory / "log.txt")
        updated = direct_last_write(directory)
        if now - updated >= 24 * HOUR:
            continue
        meta = amb.read_json(directory / "status.json", {}) or {}
        if not isinstance(meta, dict):
            meta = {}
        deps = meta.get("dependencies") or []
        deps = [dep for dep in deps if isinstance(dep, str) and TASK_ID.fullmatch(dep)]
        started = meta.get("started_at") or meta.get("created_at") or prompt.st_mtime
        try:
            if isinstance(started, str):
                started = amb.parse_time(started).timestamp()
            minutes = age_minutes(now, started)
        except (ValueError, TypeError):
            minutes = age_minutes(now, prompt.st_mtime)
        sizes = [s.st_size if s else 0 for s in (out, err, log)]
        phase = meta.get("phase")
        history = previous.get(directory.name, [])
        if not history or now - history[-1][0] >= 55:
            history.append([now, *sizes])
        # Keep one sample at/before the start of the window to compute its delta.
        old = next((s for s in reversed(history) if s[0] <= now - 600), None)
        samples[directory.name] = ([old] if old else []) + [s for s in history if s[0] > now - 600]
        current.append({
            "dir": directory.name, "minutes": minutes,
            "stage": phase if phase in ("waiting", "running", "done", "failed") else "unknown",
            "waiting_on": [dep for dep in deps if not (direct_root / dep / "exit").is_file()],
            "exit": False, "bytes": dict(zip(("out.md", "err.log", "log.txt"), sizes)),
            "growth_10m": dict(zip(("out.md", "err.log", "log.txt"),
                                   (latest - prior for latest, prior in zip(sizes, old[1:])))) if old else None,
            "last_write_minutes": age_minutes(now, updated),
            "idle_10m": now - updated >= DIRECT_IDLE_SECONDS,
        })
    if sample and samples != previous:
        amb.write_json(work / "direct-samples.json", samples, mode=0o600)
    return current


def today_stats(amb, now, medians):
    day = int(now // 86400) * 86400
    cancelled, reopened = 0, 0
    try:
        with open(Path(amb.root(), "stats.jsonl"), encoding="utf-8") as stats:
            for line in stats:
                try:
                    entry = json.loads(line)
                    if amb.parse_time(entry["ts"]).timestamp() < day:
                        continue
                    cancelled += entry.get("event") == "cancelled"
                    reopened += entry.get("event") in ("reopen", "reopened")
                except (KeyError, ValueError, TypeError):
                    continue
    except FileNotFoundError:
        pass
    return {"cancelled": cancelled, "reopened": reopened, "stage_median_minutes": medians}


def incident_key(event):
    return str(event.get("task") or event.get("id") or event.get("jid")) + ":" + str(event.get("phase"))


def encode_digest(sections):
    """Stay within the model's byte budget and give each populated section space."""
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    digest = {key: [] for key in sections}
    digest["today"] = sections["today"]
    limits = {"amb_status": 4800, "direct": 1500, "sentinel": 1100, "seen_keys": 440}
    for key, limit in limits.items():
        size = 2
        for entry in sections[key]:
            cost = len(encode(entry).encode("utf-8")) + 1
            if size + cost <= limit:
                digest[key].append(entry)
                size += cost
    body = encode(digest)
    if len(body.encode("utf-8")) > LIMIT_BYTES:
        raise ValueError("speed digest exceeds limit")
    return body, digest


def work_dir(amb):
    return Path(amb.jobs_dir("_speed"))


def eligible(state, events, now):
    runs = [at for at in state.get("runs", []) if at > now - HOUR]
    fresh = {incident_key(event) for event in events} - set(state.get("incidents") or [])
    return len(runs) < 4 and (bool(fresh) or not runs or now - runs[-1] >= PERIOD)


def advice_events(events, state):
    """A blocked task can still require a one-time sentinel decision."""
    handled = set(state.get("incidents") or [])
    return [event for event in events if event.get("action") == "needs_decision"
            and incident_key(event) not in handled]


def schedule(amb, cfg):
    if not cfg.get("speed_supervisor"):
        return
    import amb_sentinel
    work = work_dir(amb)
    now = time.time()
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    direct_root = Path(os.environ.get("AMB_DIRECT_ROOT", str(Path.home() / "amb-direct")))
    direct = direct_metadata(amb, direct_root, work, now, sample=True)
    active = direct or any(
        (amb.read_json(amb.jobs_dir(jid, "state.json"), {}) or {}).get("state") not in (*amb.TERMINAL, None)
        for jid in amb.list_jobs()
    )
    state = amb.read_json(work / "run.json", {}) or {}
    events = amb_sentinel.recent_events(amb.root(), since=now - 1800)
    seen = amb.read_json(work / "seen.json", {}) or {}
    if not (((active or advice_events(events, state)) and eligible(state, events, now))
            or any(value.get("pending") for value in seen.values())):
        return
    subprocess.Popen(amb.amb_argv("_speed-review"), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)


def parse_reply(output):
    line = output.strip().splitlines()[-1]
    reply = json.loads(line)
    if not isinstance(reply, dict) or not all(isinstance(reply.get(key), list) for key in ("actions", "reports")):
        raise ValueError("invalid speed-review reply")
    return reply


def record_reports(amb, path, seen, reports, tasks):
    for report in reports[:3]:
        if not isinstance(report, dict) or not all(isinstance(report.get(k), str) for k in ("key", "task", "text")):
            continue
        key, task = report["key"], report["task"]
        reason = " ".join(report["text"].split())
        if (task not in tasks or not TASK_ID.fullmatch(task) or key in seen or
                not key.startswith(task + ":") or len(key) > 128 or not reason or len(reason) > 500 or
                not key.isprintable()):
            continue
        entry = {"ts": amb.now(), "task": task, "action": "needs_decision", "reason": reason,
                 "needs_human": True, "src": "speed", "key": key, "event_id": "speed:" + key}
        seen[key] = {"pending": True, "entry": entry}
    amb.write_json(path, seen, mode=0o600)


def forward_pending(amb, cfg, path, seen):
    import amb_sentinel
    for record in seen.values():
        if not record.get("pending"):
            continue
        try:
            entry = record["entry"]
            amb_sentinel.append_outbox(amb, cfg, entry)
        except (amb.HostDown, amb.StepError, OSError, ValueError):
            break  # durable queue, retry on next tick without repeating the model
        record["pending"] = False
        amb.write_json(path, seen, mode=0o600)


def filter_direct_reports(reports, direct, direct_root):
    running = {job["dir"]: job for job in direct if job["stage"] in ("running", "unknown")}
    allowed = []
    for report in reports:
        task = report.get("task") if isinstance(report, dict) else None
        if isinstance(task, str) and task in running:
            directory = direct_root / task
            if (report.get("key") != task + ":" + DIRECT_IDLE_KEY or not running[task]["idle_10m"]
                    or (directory / "exit").exists()
                    or time.time() - direct_last_write(directory) < DIRECT_IDLE_SECONDS):
                continue
        allowed.append(report)
    return allowed


def apply_actions(amb, cfg, actions, tasks):
    tick = False
    with open(Path(amb.root(), ".tick.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = amb.load_config()
        for action in actions[:3]:
            if not isinstance(action, dict) or not isinstance(action.get("id"), str) or action["id"] not in tasks:
                continue
            jid = action["id"]
            if action.get("do") == "tick":
                state = amb.read_json(amb.jobs_dir(jid, "state.json"), {}) or {}
                tick |= bool(state and state.get("state") not in amb.TERMINAL)
            elif action.get("do") == "start_ready":
                try:
                    child, state = amb.load(jid)
                except amb.AmbError:
                    continue
                parent = child.get("parent")
                if not parent or state.get("state") != "queued" or state.get("run"):
                    continue
                parent_state = amb.read_json(amb.jobs_dir(parent, "state.json"), {}) or {}
                if parent_state.get("state") == "subs" and jid in (parent_state.get("subs") or []) and all(
                        (amb.read_json(amb.jobs_dir(dep, "state.json"), {}) or {}).get("state") == "done"
                        for dep in child.get("depends_on") or []):
                    amb.tick_job(jid, cfg)
        if tick:
            amb.tick_all(cfg)


def review(amb, cfg):
    import amb_sentinel
    if not cfg.get("speed_supervisor"):
        return
    work = work_dir(amb)
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(work / ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        now = time.time()
        seen_path, state_path = work / "seen.json", work / "run.json"
        seen = amb.read_json(seen_path, {}) or {}
        forward_pending(amb, cfg, seen_path, seen)
        direct_root = Path(os.environ.get("AMB_DIRECT_ROOT", str(Path.home() / "amb-direct")))
        direct = direct_metadata(amb, direct_root, work, now)
        active, medians = amb_metadata(amb, cfg, now)
        state = amb.read_json(state_path, {}) or {}
        events = amb_sentinel.recent_events(amb.root(), since=now - 1800)
        if not (direct or active):
            events = advice_events(events, state)
        if not (direct or active or events) or not eligible(state, events, now):
            return
        event_keys = set(state.get("incidents") or [])
        coverage = state.get("coverage") or {}
        events.sort(key=lambda event: (incident_key(event) in event_keys, event.get("ts", "")))
        fresh_tasks = {incident_key(event).split(":", 1)[0] for event in events if incident_key(event) not in event_keys}
        active.sort(key=lambda job: (job["id"] not in fresh_tasks, coverage.get("amb:" + job["id"], 0),
                                     -(job["stage_minutes"] or 0), job["id"]))
        direct.sort(key=lambda job: (coverage.get("direct:" + job["dir"], 0),
                                     -job["last_write_minutes"], job["dir"]))
        body, digest = encode_digest({"amb_status": active, "direct": direct, "sentinel": events,
                                      "today": today_stats(amb, now, medians), "seen_keys": list(reversed(seen))})
        sha = hashlib.sha256(body.encode()).hexdigest()
        if state.get("digest_sha256") == sha:
            return
        state.update(digest_sha256=sha, runs=[at for at in state.get("runs", []) if at > now - HOUR] + [now],
                     incidents=sorted({incident_key(event) for event in events if incident_key(event) in event_keys}
                                      | {incident_key(event) for event in digest["sentinel"]}))
        coverage = {key: at for key, at in coverage.items() if at > now - 24 * HOUR}
        coverage.update(("amb:" + job["id"], now) for job in digest["amb_status"])
        coverage.update(("direct:" + job["dir"], now) for job in digest["direct"])
        state["coverage"] = coverage
        amb.write_json(state_path, state, mode=0o600)
        prompt = Path(__file__).with_name("speed-supervisor-prompt.md").read_text(encoding="utf-8")
        host = amb.Host(cfg, "kml-dev")
        cmd = ["sh", "-c", 'cd /tmp && exec "$@"', "sh", "timeout", "300", "bash", CLAUDE,
               "-p", "--model", "claude-opus-5-5", "--effort", "medium", "--permission-mode", "plan",
               "--no-session-persistence", "--tools", ""]
        try:
            schema = ('输出字段示例（id、task 必须用 digest 中的原始编号）'
                      '\n{"actions":[{"do":"start_ready","id":"J-0000-01.a1s01"}],'
                      '"reports":[{"key":"J-0000-01:问题类型","task":"J-0000-01","text":"一两句中文"}]}\n')
            result = host.run(cmd, input=prompt + "\n\n" + schema + "\n## Digest (JSON metadata)\n" + body + "\n",
                              timeout=305 + host.timeout, check=True)
            reply = parse_reply(result.stdout)
        except (amb.HostDown, amb.StepError, OSError, ValueError, IndexError) as exc:
            amb.append_jsonl(Path(amb.root(), "stats.jsonl"),
                             {"ts": amb.now(), "event": "speed_review_failed", "error": type(exc).__name__})
            return
        tasks = {job["id"] for job in digest["amb_status"]}
        report_tasks = tasks | {job["dir"] for job in digest["direct"]} | {
            event["task"] for event in digest["sentinel"]
        }
        reports = filter_direct_reports(reply["reports"][:3], digest["direct"], direct_root)
        record_reports(amb, seen_path, seen, reports, report_tasks)
        forward_pending(amb, cfg, seen_path, seen)
        apply_actions(amb, cfg, reply["actions"], tasks)
