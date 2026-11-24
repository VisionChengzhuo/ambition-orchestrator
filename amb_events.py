"""Durable webhook delivery for job completions and progress."""

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path


TERMINAL = frozenset({"done", "failed", "blocked", "cancelled"})
MAX_BODY = 1 << 20
SENSITIVE = re.compile(
    r"(?i)\b(?:bearer\s+|(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*)[^\s,;]+"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:sk-ant-|sk-proj-|gh[pousr]_|github_pat_|glpat-|xox[baprs]-|xai-|lin_api_)[A-Za-z0-9_-]{10,}",
    re.DOTALL,
)


def read_json(path):
    try:
        value = Path(path).read_text(encoding="utf-8")
        return json.loads(value)
    except (OSError, ValueError):
        return None


def write_event(path, entry, *, replace):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            json.dump(entry, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            if replace:
                tmp.replace(path)
            else:
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    pass
        finally:
            tmp.unlink(missing_ok=True)


def event_dir(root):
    return Path(root) / "jobs" / "_webhook" / "events"


def clean_text(value):
    return SENSITIVE.sub("[已隐去]", str(value or ""))


def emit(root, jid, title, state, summary, result_path):
    if state not in TERMINAL:
        return
    lines = (line.strip()[:200] for line in clean_text(summary).splitlines() if line.strip())
    body = {
        "type": "amb_job_event",
        "id": jid,
        "title": " ".join(clean_text(title).splitlines())[:160],
        "state": state,
        "summary": "\n".join(islice(lines, 3)),
        "result_path": str(result_path),
        "reply_guidance": (
            "用任务名称指代事项，先说结论。正文、列表和链接文字默认不展示 TCZ 卡号；"
            "要附卡片链接时，用任务名称作链接文字。只有用户明确要卡号时才给。再写「执行模型：<模型名或 ID>｜推理档位：<实际档位>」。"
            "按实际完成工作的执行记录填写，无法核实的字段写「未确认」。省略 Codex 等执行工具名。"
        ),
    }
    key = hashlib.sha256(json.dumps([jid, state, str(result_path)]).encode()).hexdigest()
    enqueue(root, body, key)


def emit_progress(root, jid, title, report, result_path, report_key):
    key = hashlib.sha256(json.dumps([jid, "progress", report_key]).encode()).hexdigest()
    body = {
        "type": "amb_job_event",
        "id": jid,
        "event_id": f"progress:{key}",
        "title": " ".join(clean_text(title).splitlines())[:160],
        "state": "progress",
        "summary": clean_text(report),
        "result_path": str(result_path),
        "reply_guidance": "这是进行中的进度，按 event_id 去重。转达实际进度、下一步和需要用户决定的事。",
    }
    enqueue(root, body, key)


def enqueue(root, body, key):
    directory = event_dir(root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (key + ".json")
    if path.exists() or (directory / "sent" / path.name).exists():
        return
    write_event(path, {"body": body, "queued": time.time()}, replace=False)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_post(url, data, headers, deadline):
    """POST without redirects; plaintext is available for loopback tests only."""
    parsed = urllib.parse.urlsplit(url)
    if not (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost"))):
        raise ValueError("通知 URL 必须使用 HTTPS 或回环 HTTP")
    remaining = deadline - time.time()
    if remaining <= 0:
        raise TimeoutError("通知时间预算已用完")
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.build_opener(NoRedirect).open(request, timeout=remaining) as response:
        body = b""
        while chunk := response.read1(65536):
            body += chunk
            if len(body) > MAX_BODY or time.time() >= deadline:
                raise TimeoutError("响应过大或超时")
        return response.status, body


def send(wh, body, deadline):
    try:
        source = wh.get("config_file")
        if source:
            wh = {**wh, **(read_json(Path(source).expanduser()) or {})}
        url = wh.get("url")
        if not url:
            return 0
        headers = {"Content-Type": "application/json"}
        secret = wh.get("secret")
        if secret:
            headers[wh.get("header") or "X-Amb-Secret"] = secret
        body = dict(body)
        for private in (url, secret, secret.split(" ", 1)[-1] if secret else None):
            if private:
                for field, value in body.items():
                    if isinstance(value, str) and (len(private) >= 8 or value == private):
                        body[field] = value.replace(private, "[已隐去]")
        status, _ = http_post(url, json.dumps(body, ensure_ascii=False).encode(), headers, deadline)
        return status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code
    except (OSError, TypeError, ValueError):
        return 0


def flush(root, wh, budget=15):
    directory = event_dir(root)
    if not directory.is_dir() or not (wh.get("url") or wh.get("config_file")):
        return
    with (directory / ".lock").open("a") as lock:
        wait_until = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= wait_until:
                    return
                time.sleep(0.1)
        deadline = time.time() + budget
        sent = directory / "sent"
        sent.mkdir(mode=0o700, exist_ok=True)
        ready = []
        for path in directory.glob("*.json"):
            entry = read_json(path)
            if (sent / path.name).exists():
                path.unlink(missing_ok=True)
            elif entry and entry.get("retry_at", 0) <= time.time():
                ready.append((path, entry))
        # A slow or rejected older event should leave time for new completions.
        ready.sort(key=lambda item: (bool(item[1].get("attempted_at")), item[1]["queued"]))
        for path, entry in ready:
            if time.time() >= deadline:
                break
            status = send(wh, entry["body"], min(deadline, time.time() + 4))
            attempted = datetime.now(timezone.utc).isoformat(timespec="seconds")
            entry.update(http_status=status, attempted_at=attempted)
            if 200 <= status < 300:
                entry["sent_at"] = attempted
                entry.pop("retry_at", None)
                write_event(sent / path.name, entry, replace=True)
                path.unlink(missing_ok=True)
            else:
                wait = (wh.get("client_error_backoff_s") or 45) if 400 <= status < 500 else 5
                entry["retry_at"] = time.time() + wait
                write_event(path, entry, replace=True)
