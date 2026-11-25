"""The coordination-only Claude MCP server. Tool calls contain data, never commands."""
import json
import shlex
import subprocess
from pathlib import Path
import sys

SERVER = "amb_router"

STRING = {"type": "string", "minLength": 1, "maxLength": 24000}
NUMBER = {"type": "integer", "minimum": 0}
TOOLS = {
    "dispatch": ("Create and submit one child job. Assignment becomes the child's brief and user instruction.",
                 {"title": STRING, "assignment": STRING, "goal": STRING, "thinking": STRING,
                  "host": STRING, "repo": STRING, "kind": {"enum": ["doc", "code", "ask"]},
                  "executor": {"enum": ["sol", "opus", "fable", "astra"]}, "model": STRING, "effort": STRING},
                 ("title", "assignment", "goal", "thinking")),
    "children": ("Read your delegated jobs and their result summaries.", {}, ()),
    "inbox": ("Read your current inbox; each entry includes its sequence number.", {}, ()),
    "ack": ("Acknowledge inbox entries through sequence n after processing them.", {"n": NUMBER}, ("n",)),
    "wait": ("Wait up to 20 seconds for an inbox message or a child completion.", {}, ()),
    "direction": ("Record a change of direction and its reasoning.", {"thinking": STRING}, ("thinking",)),
    "child_action": ("Send clarification, redo or cancel one of your own children.",
                     {"id": STRING, "action": {"enum": ["send", "redo", "cancel"]}, "reason": STRING},
                     ("id", "action", "reason")),
}


def claude_args(command):
    args = ["-I", "-c", Path(__file__).read_text(encoding="utf-8"), *command]
    mcp = json.dumps({"mcpServers": {SERVER: {"type": "stdio", "command": "python3", "args": args}}})
    return ["--tools", "", "--strict-mcp-config", "--mcp-config", mcp, "--setting-sources", "",
            "--disable-slash-commands", "--disallowedTools", "mcp__kml-public-search",
            "--allowedTools", *[f"mcp__{SERVER}__{name}" for name in TOOLS]]


def command(jid, attempt, step, host, local_command):
    identity = [jid, str(attempt), step]
    if host == "<REDACTED_HOST_A>":
        return ["sudo", "-n", "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "<REDACTED_HOST_B>", '"$HOME/bin/amb" _router ' + shlex.join(identity)]
    return [*local_command, *identity]


def tool_list():
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": properties, "required": list(required),
                             "additionalProperties": False}}
            for name, (desc, properties, required) in TOOLS.items()]


def validate_call(name, args):
    if not isinstance(name, str) or name not in TOOLS or not isinstance(args, dict):
        raise ValueError("Unknown Router tool")
    _, properties, required = TOOLS[name]
    if not set(required) <= args.keys() <= properties.keys():
        raise ValueError("Invalid Router tool arguments")
    for key, value in args.items():
        schema = properties[key]
        if "enum" in schema:
            valid = value in schema["enum"]
        elif schema["type"] == "integer":
            valid = type(value) is int and value >= 0
        else:
            valid = isinstance(value, str) and value.strip() and len(value) <= schema["maxLength"]
        if not valid:
            raise ValueError(f"Invalid argument: {key}")


def serve(call, expected_errors):
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if not isinstance(req, dict) or "id" not in req:
            continue
        response = {"jsonrpc": "2.0", "id": req["id"]}
        method = req.get("method")
        if method == "initialize":
            response["result"] = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                                  "serverInfo": {"name": SERVER, "version": "1"}}
        elif method == "tools/list":
            response["result"] = {"tools": tool_list()}
        elif method == "ping":
            response["result"] = {}
        elif method == "tools/call":
            params = req.get("params")
            try:
                if not isinstance(params, dict):
                    raise ValueError("Missing Router tool arguments")
                name, args = params.get("name"), params.get("arguments", {})
                validate_call(name, args)
                value, error = call(name, args), False
            except (ValueError, *expected_errors) as exc:
                value, error = str(exc), True
            response["result"] = {"isError": error,
                                  "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        print(json.dumps(response, ensure_ascii=False), flush=True)


def relay(command, name, args):
    response = subprocess.run(command, input=json.dumps({"name": name, "args": args}),
                              capture_output=True, text=True, timeout=45)
    if response.returncode:
        raise ValueError("Router coordination service is unavailable")
    result = json.loads(response.stdout)
    if not result.get("ok"):
        raise ValueError(result.get("error") or "Router coordination request failed")
    return result["value"]


if __name__ == "__main__":
    serve(lambda name, args: relay(sys.argv[1:], name, args), (OSError, subprocess.TimeoutExpired))
