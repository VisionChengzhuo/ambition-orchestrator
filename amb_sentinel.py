"""Token-free checks run by amb tick. Events are kept outside job state."""
import contextlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import time


LIMITS = {"queue": 120, "plan": 480, "exec": 600, "review": 300, "human": 600}

# The oldest deployed runners do not include progress in their poll response.
# Use this read-only probe only for legacy execution steps old enough to alert.
PROGRESS = '''
import json, os, sys
def output_progress(directory):
    result = {}
    for name in ("stdout", "stderr", "out"):
        try:
            info = os.stat(os.path.join(directory, name))
        except FileNotFoundError:
            continue
        except OSError:
            return None
        result[name] = [info.st_size, info.st_mtime]
    return result
'''
PROBE_PROGRESS = PROGRESS + "print(json.dumps(output_progress(sys.argv[1])))"

# One append per stable event ID, including retries after a lost SSH response.
WRITE_OUTBOX = '''
import fcntl, json, os, sys
event = json.load(sys.stdin)
with open(sys.argv[1], "a+", encoding="utf-8") as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    stream.seek(0)
    last = ""
    for last in stream:
        try:
            row = json.loads(last)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event_id") == event["event_id"]:
            if not last.endswith("\\n"):
                stream.write("\\n")
            break
    else:
        if last and not last.endswith("\\n"):
            stream.write("\\n")
        stream.write(json.dumps(event, ensure_ascii=False) + "\\n")
    stream.flush()
    os.fsync(stream.fileno())
'''


def append_outbox(amb, cfg, entry):
    """Append to the existing supervisor outbox, after validating its routing."""
    sc = cfg.get("supervisor") or {}
    if not sc.get("enabled") or not sc.get("host") or not sc.get("outbox"):
        raise ValueError("supervisor outbox is not configured")
    host = amb.Host(cfg, sc["host"])
    host.run(["python3", "-c", WRITE_OUTBOX, sc["outbox"]],
             input=json.dumps(entry, ensure_ascii=False), timeout=host.timeout + 5, check=True)


def event_path(root, jid, phase):
    return Path(root, "jobs", "_sentinel", "events", jid, phase + ".json")


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def phase_start(state):
    history = state.get("history") or []
    return timestamp(history[-1].get("ts")) if history else None


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path, value):
    temporary = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                       encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    except OSError:
        # Observation failure must not stop a live task's state machine.
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def record(root, jid, job, phase, clock, since, reason, action="needs_decision"):
    limit = LIMITS[phase]
    if since is None or clock - since <= limit:
        return
    path = event_path(root, jid, phase)
    if path.exists():
        return
    event = {
        "ts": datetime.fromtimestamp(clock, timezone.utc).isoformat(timespec="seconds"),
        "event_id": f"sentinel:{jid}:{phase}",
        "task": jid,
        "phase": phase,
        "age_s": int(clock - since),
        "limit_s": limit,
        "action": action,
        "reason": reason,
        "src": "sentinel",
    }
    if job.get("test") is True:
        event["test"] = True
    if job.get("campaign"):
        event["campaign"] = job["campaign"]
    write_json(path, event)


def observe_state(root, jid, job, state, clock=None):
    phase = state.get("state")
    if phase not in ("queued", "blocked", "waiting_human"):
        return
    clock = time.time() if clock is None else clock
    deadline = timestamp(job.get("deadline"))
    if deadline is not None and deadline <= clock:
        return
    started = phase_start(state)
    if phase == "queued":
        if started is None or clock - started <= LIMITS["queue"]:
            return
        # A child waiting for prerequisites is not a ready task stuck in the queue.
        for dependency in job.get("depends_on") or []:
            dependency_state = read_json(Path(root, "jobs", dependency, "state.json")) or {}
            if dependency_state.get("state") != "done":
                return
            started = max(started, phase_start(dependency_state) or clock)
        record(root, jid, job, "queue", clock, started, "就绪但排队超过 2 分钟，继续正常调度",
               "start_ready" if job.get("parent") else "tick")
    elif state.get("needs_human") or phase == "waiting_human":
        record(root, jid, job, "human", clock, started, "等待人工处理超过 10 分钟")


def output_growth(root, jid, run, progress, clock, waiting_on_children=False):
    path = Path(root, "jobs", "_sentinel", "progress", jid + ".json")
    previous = read_json(path) or {}
    sizes = {name: size for name, (size, _) in progress.items() if size}
    started = run["started"]
    if previous.get("run") != [run.get("dir"), started]:
        last = max([started, *(min(clock, mtime) for size, mtime in progress.values() if size)])
    else:
        last = previous["last_growth"]
        if sizes == previous["sizes"] and not waiting_on_children:
            return last
        increases = [min(clock, mtime) for name, (size, mtime) in progress.items()
                     if size > previous["sizes"].get(name, 0)]
        last = max([last, *increases])
    if waiting_on_children:
        last = clock
    write_json(path, {"run": [run.get("dir"), started], "sizes": sizes, "last_growth": last})
    return last


def observe_run(root, jid, job, run, poll, legacy_progress, clock=None, *, waiting_on_children=False):
    phase = "exec" if run["step"] == "repair" else run["step"]
    if phase not in ("plan", "exec", "review") or event_path(root, jid, phase).exists():
        return
    clock = time.time() if clock is None else clock
    started = run["started"]
    if phase == "exec":
        progress = poll.get("progress")
        if progress is None and clock - started > LIMITS[phase]:
            progress = legacy_progress()
        if progress is not None:
            started = output_growth(root, jid, run, progress, clock, waiting_on_children)
            if not waiting_on_children:
                record(root, jid, job, phase, clock, started, "执行进程仍在运行，输出超过 10 分钟未增长")
    else:
        reason = {"plan": "规划仍在运行，已超过 8 分钟", "review": "复核仍在运行，已超过 5 分钟"}[phase]
        record(root, jid, job, phase, clock, started, reason)


def recent_events(root, since=0):
    events = []
    for path in Path(root, "jobs", "_sentinel", "events").glob("*/*.json"):
        event = read_json(path)
        if isinstance(event, dict) and not event.get("campaign") and (timestamp(event.get("ts")) or 0) >= since:
            events.append(event)
    return sorted(events, key=lambda event: (event["ts"], event["event_id"]))
