"""Shared lifecycle ledger for orchestrated and direct jobs."""
import fcntl
import json
import os
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

TIMES = ("asked_at", "dispatched_at", "started_at", "finished_at", "delivered_at")
STAGES = ("ask_to_dispatch", "dispatch_to_start", "start_to_finish", "finish_to_delivery")
TERMINAL = {"done", "failed", "blocked", "cancelled"}
LEGACY_CHILD = re.compile(r"J-[0-9]+-[0-9]+(?:r[0-9]+)?\.a[0-9]+s[0-9]+\Z")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo else None
    except (AttributeError, ValueError, OverflowError):
        return None


def normalize_asked(value):
    epoch = timestamp(value)
    if epoch is None:
        raise ValueError("--asked-at 需要包含时区的 ISO 8601 时间")
    if epoch > datetime.now(timezone.utc).timestamp():
        raise ValueError("--asked-at 不能晚于当前时间")
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def records(stream):
    for line in stream:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                yield row
        except ValueError:
            continue


def append(root, row):
    with Path(root, "stats.jsonl").open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def mark_delivered(root, row):
    """Keep the original receipt time if Ambition retries the command."""
    path = Path(root, "stats.jsonl")
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        for previous in records(stream):
            if (previous.get("source", "amb"), previous.get("id")) == (row["source"], row["id"]):
                if previous.get("delivered_at"):
                    if not row.get("feishu_message_ids") or previous.get("feishu_message_ids"):
                        return previous["delivered_at"]
                    row["supersedes_unverified_delivery"] = True
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row["delivered_at"]


def correction_count(inputs, now):
    inputs = Path(inputs)
    if not inputs.exists():
        return None
    seen = set()
    files = [inputs / "corrections.jsonl", *sorted(inputs.glob("*/corrections.jsonl"))]
    exported = False
    for path in files:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                exported = True
                for entry in records(stream):
                    at = timestamp(entry.get("at") or entry.get("ts"))
                    if at is not None and now - 86400 < at <= now:
                        identity = entry.get("id")
                        seen.add(("id", str(identity)) if identity else
                                 ("event", at, str(entry.get("text") or entry.get("message") or ""),
                                  str(entry.get("category") or "")))
        except OSError:
            continue
    return len(seen) if exported else None


def minutes(begin, end):
    if begin is None or end is None or end < begin:
        return None
    return (end - begin) / 60


def median(values):
    return round(statistics.median(values), 2) if values else None


def snapshot(root, inputs, now=None, since=None):
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    path = Path(root, "stats.jsonl")
    jobs = {}
    reopens = {}
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            for row in records(stream):
                jid, event = row.get("id"), row.get("event")
                if not isinstance(jid, str):
                    continue
                source = row.get("source", "amb")
                if event == "reopen" and isinstance(row.get("new_id"), str):
                    reopens[row["new_id"]] = jid
                elif event in TERMINAL or any(row.get(key) for key in TIMES):
                    job = jobs.setdefault((source, jid), {})
                    job.update({key: row[key] for key in TIMES if row.get(key)})
                    if event in TERMINAL:
                        job.setdefault("finished_at", row.get("ts"))
                        job["state"] = event
                        if isinstance(row.get("rework"), list):
                            job["rework"] = row["rework"]
                    for key in ("state", "parent", "last_sig", "execution_breakdown"):
                        if row.get(key):
                            job[key] = row[key]
                    if isinstance(row.get("attempt"), int):
                        job["attempt"] = max(job.get("attempt", 1), row["attempt"])
    stages = {key: [] for key in STAGES}
    deliveries, slow, execution_long_tail, failures = [], [], [], {}
    finalized = set()
    reworked = set()

    for (source, jid), job in jobs.items():
        if job.get("parent") or (source == "amb" and LEGACY_CHILD.fullmatch(jid)):
            continue
        points = [timestamp(job.get(key)) for key in TIMES]
        lengths = {key: minutes(begin, end)
                   for key, begin, end in zip(STAGES, points, points[1:])}
        for key, duration, ended in zip(STAGES, lengths.values(), points[1:]):
            if duration is not None and (since is None or ended >= since):
                stages[key].append(duration)
        delivery = minutes(points[0], points[-1])
        if delivery is not None and (since is None or points[-1] >= since):
            deliveries.append(delivery)
        reported = {key: round(value, 2) for key, value in lengths.items() if value is not None}
        if reported and (since is None or any(point is not None and point >= since for point in points)):
            slow.append({"id": jid, "source": source, "state": job.get("state"),
                         "minutes": round(delivery if delivery is not None else sum(reported.values()), 2),
                         "stages_minutes": reported})
        if (breakdown := job.get("execution_breakdown")) and (since is None or (points[3] or now) >= since):
            execution_long_tail.append({"id": jid, "state": job.get("state"), **breakdown})
        if job.get("state") in TERMINAL and (since is None or points[3] is not None and points[3] >= since):
            origin = jid
            # Orchestrator reopen IDs share the original job's metric denominator.
            visited = {origin}
            while source == "amb" and origin in reopens and reopens[origin] not in visited:
                origin = reopens[origin]
                visited.add(origin)
            identity = source, origin
            finalized.add(identity)
            unresolved_review = any(
                entry.get("reason") == "review_failed" and (not entry.get("in_place") or job["state"] != "done")
                for entry in job.get("rework", []) if isinstance(entry, dict)
            )
            if (job.get("attempt", 1) > 1 or job["state"] == "cancelled" or unresolved_review
                    or (source == "amb" and jid in reopens)):
                reworked.add(identity)
            if job.get("state") != "done":
                label = str(job.get("last_sig") or job["state"])
                failures[label] = failures.get(label, 0) + 1

    for jid in reopens.values():
        if ("amb", jid) in finalized:
            reworked.add(("amb", jid))
    corrections = correction_count(inputs, now)
    return {
        "delivery_median_minutes": median(deliveries),
        "delivery_max_minutes": round(max(deliveries), 2) if deliveries else None,
        "post_completion_median_minutes": median(stages["finish_to_delivery"]),
        "rework_rate_pct": round(100 * len(reworked) / len(finalized), 2) if finalized else None,
        "corrections_24h": corrections,
        "samples": {"delivery": len(deliveries), "post_completion": len(stages["finish_to_delivery"]),
                    "finalized": len(finalized), "reworked": len(reworked)},
        "stage_medians_minutes": {name: median(values) for name, values in stages.items()},
        "stage_max_minutes": {name: round(max(values), 2) if values else None for name, values in stages.items()},
        "stage_samples": {name: len(values) for name, values in stages.items()},
        "execution_long_tail": sorted(execution_long_tail, key=lambda row: row["elapsed_s"], reverse=True)[:10],
        "slow_jobs": sorted(slow, key=lambda row: row["minutes"], reverse=True)[:10],
        "failures": failures,
    }
