#!/usr/bin/env python3
"""Send an amb job's result back to the Feishu conversation that asked for it.

  ambition-job-deliver deliver <job_id> [--dry-run]   queue text (and files) into the original thread
  ambition-job-deliver check [--heal] [--json]        healthcheck: every finished job that carries
                                                      feishu_request_id must have reached Feishu

Destination is fixed by the bridge from job.json's feishu_request_id; repeated calls are
deduplicated by the bridge per (request_id, job_id). Never prints credentials.
Optional deliverable manifest in the job worktree: deliver.json
  {"text_file": "out.md", "files": ["videos/*.mp4", "table.png"]}
Without a manifest: text = <wt>/out.md if present, else the job summary; no files.
"""
import base64, glob, json, os, re, subprocess, sys, time
from datetime import datetime, timezone

AMB = os.environ.get("AMB_ROOT") or os.path.expanduser("~/amb")
HELPER = os.environ.get("AMB_JOB_DELIVER_HELPER", "sudo -n /usr/local/sbin/ambition-job-deliver").split()
TERMINAL = ("done", "failed", "blocked", "cancelled")
MAX_FILES, MAX_BYTES = 20, 6 * 1024 * 1024
REQ = re.compile(r"req_[a-f0-9]{32}")
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".mp4": "video/mp4",
        ".pdf": "application/pdf", ".csv": "text/csv", ".md": "text/markdown", ".txt": "text/plain",
        ".json": "application/json", ".gif": "image/gif", ".xlsx":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
STATE_TEXT = {"failed": "没有做成", "blocked": "卡住了，需要负责人处理", "cancelled": "已取消"}


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def parse_time(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def helper(method, params):
    try:
        res = subprocess.run(HELPER, input=json.dumps({"method": method, "params": params}),
                             capture_output=True, text=True, timeout=60)
        data = json.loads(res.stdout)
        return data if isinstance(data, dict) else {"ok": False, "stage": "helper"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"ok": False, "stage": "helper"}


def scrub(text):
    # Feishu askers should not see host paths of the operator's machines.
    return re.sub(r"(?<![\w.])/(?:home|root|[\w.-]+_hdd|opt|tmp)/[^\s)）」\]】，。；`'\"]+", "[内部路径]", text)


def plan(jid):
    jd = os.path.join(AMB, "jobs", jid)
    job, st, res = read_json(os.path.join(jd, "job.json"), {}), read_json(os.path.join(jd, "state.json"), {}), \
        read_json(os.path.join(jd, "result.json"), {})
    rid = job.get("feishu_request_id")
    if not isinstance(rid, str) or not REQ.fullmatch(rid):
        raise SystemExit(f"{jid}: 没有有效的 feishu_request_id")
    state = st.get("state")
    if state not in TERMINAL:
        raise SystemExit(f"{jid}: 状态 {state} 还没结束")
    summary = (res.get("summary") or st.get("summary") or "").strip()
    wt = st.get("wt") or ""
    files, text = [], None
    if state == "done":
        manifest = read_json(os.path.join(wt, "deliver.json"), None) if wt else None
        manifest = manifest if isinstance(manifest, dict) else (job.get("deliver") or {})
        text_file = manifest.get("text_file") or "out.md"
        if wt and os.path.isfile(os.path.join(wt, text_file)):
            with open(os.path.join(wt, text_file), encoding="utf-8", errors="replace") as f:
                text = f.read()
        text = (text or "").strip() or summary or "任务已完成。"
        real_wt = os.path.realpath(wt) if wt else ""
        for pattern in manifest.get("files") or []:
            for path in sorted(glob.glob(os.path.join(wt, pattern))):
                real = os.path.realpath(path)
                if not real.startswith(real_wt + os.sep) or not os.path.isfile(real):
                    continue
                if os.path.getsize(real) > MAX_BYTES:
                    text += f"\n（附件 {os.path.basename(real)} 超过 6MB，未能直接发送）"
                    continue
                if real not in files:
                    files.append(real)
        if len(files) > MAX_FILES:
            text += f"\n（共 {len(files)} 个附件，这里只发前 {MAX_FILES} 个）"
            files = files[:MAX_FILES]
    else:
        text = f"这个请求的任务{STATE_TEXT.get(state, state)}。" + \
               ("\n" + summary[:600] if summary and state == "failed" else "") + \
               "\n已记录，负责人会跟进；有新结果会在这里继续回复。"
    return {"job_id": jid, "request_id": rid, "state": state, "text": scrub(text), "files": files}


def deliver(jid, dry=False):
    p = plan(jid)
    if dry:
        return {"ok": True, "dry_run": True, **{k: v for k, v in p.items() if k != "text"},
                "text_preview": p["text"][:300]}
    receipt_path = os.path.join(AMB, "jobs", jid, "requester_delivery.json")
    outputs, errors = [], []
    r = helper("deliver", {"request_id": p["request_id"], "job_id": jid, "part": "text", "text": p["text"]})
    (outputs.extend(r["result"]["outputs"]) if r.get("ok") else errors.append(r.get("error") or r.get("stage")))
    for path in p["files"] if not errors else []:
        name = os.path.basename(path)
        mime = MIME.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        r = helper("deliver", {"request_id": p["request_id"], "job_id": jid, "part": "file", "base64": b64,
                               "filename": name, "mime": mime, "image": mime in ("image/png", "image/jpeg")})
        if r.get("ok"):
            outputs.append({"output_id": r["result"]["output_id"], "state": r["result"]["state"]})
        else:
            errors.append(f"{name}: {r.get('error') or r.get('stage')}")
    receipt = {"request_id": p["request_id"], "job_state": p["state"], "queued": not errors,
               "outputs": outputs, "errors": errors, "files": [os.path.basename(f) for f in p["files"]],
               "at": datetime.now(timezone.utc).isoformat()}
    write_json(receipt_path, receipt)
    return {"ok": not errors, "job_id": jid, **receipt}


def confirmed(amb_root, jid, request_id):
    receipt = read_json(os.path.join(amb_root, "jobs", jid, "requester_delivery.json"), {})
    if receipt.get("request_id") != request_id or not receipt.get("queued"):
        return False
    expected = {output["output_id"] for output in receipt.get("outputs", [])}
    reply = helper("status", {"request_id": request_id, "job_id": jid})
    result = reply.get("result") or {}
    sent = {output["id"] for output in result.get("outputs", [])
            if output["state"] == "sent" and output.get("remote")}
    if reply.get("ok") is True and expected and expected <= sent and result.get("all_sent") is True:
        return [output["remote"] for output in result["outputs"]]
    return []


def candidates():
    cfg = read_json(os.path.join(AMB, "config.json"), {})
    rr = cfg.get("requester_reply") or {}
    since = parse_time(rr["since"]) if rr.get("since") else 0
    for jid in sorted(os.listdir(os.path.join(AMB, "jobs"))):
        if jid.startswith("_"):
            continue
        jd = os.path.join(AMB, "jobs", jid)
        job, st = read_json(os.path.join(jd, "job.json"), {}), read_json(os.path.join(jd, "state.json"), {})
        if not job.get("feishu_request_id") or job.get("parent") or st.get("state") not in TERMINAL:
            continue
        hist = st.get("history") or []
        if not hist or parse_time(hist[-1]["ts"]) < since:
            continue
        yield jid, hist[-1]["ts"]


def check(heal=False):
    problems, healed, ok = [], [], 0
    for jid, ts in candidates():
        receipt = read_json(os.path.join(AMB, "jobs", jid, "requester_delivery.json"))
        age = time.time() - parse_time(ts)
        if not receipt or not receipt.get("queued"):
            if age < 180:  # amb's own notify channel normally does this within a tick
                continue
            if heal:
                r = deliver(jid)
                (healed if r.get("ok") else problems).append(f"{jid}:未排队" + ("" if r.get("ok") else ":" + ";".join(r.get("errors") or [])[:80]))
            else:
                problems.append(f"{jid}:未排队")
            continue
        st = helper("status", {"request_id": receipt["request_id"], "job_id": jid})
        if not st.get("ok"):
            problems.append(f"{jid}:状态不可读")
        elif st["result"]["all_sent"]:
            ok += 1
        elif age > 600:
            bad = [o["state"] for o in st["result"]["outputs"] if o["state"] != "sent"]
            problems.append(f"{jid}:未送达({','.join(sorted(set(bad))) or '无输出'})")
    stuck = helper("stuck", {"older_than_ms": 600000, "rearm_failed": bool(heal)})
    if stuck.get("ok"):
        healed += [f"重试:{x}" for x in stuck["result"]["rearmed"]]
    else:
        problems.append("桥接不可达")
    return {"ok": not problems, "delivered": ok, "problems": problems, "healed": healed}


def main(argv):
    if len(argv) >= 2 and argv[0] == "deliver":
        r = deliver(argv[1], "--dry-run" in argv)
    elif argv and argv[0] == "check":
        r = check("--heal" in argv)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
