"""Claude Code's permission prompt for the Show Claude window. Owner: chat.

A minimal MCP server over stdio (newline-delimited JSON-RPC 2.0, standard library only) with one tool,
`approve`. Claude Code calls it (`--permission-prompt-tool mcp__uireport__approve`) before any tool that
isn't pre-allowed. The request is written as <id>.request.json into $UIREPORT_APPROVAL_DIR; the chat
window shows a card and writes <id>.answer.json. No answer within 10 minutes, or anything going wrong,
means deny. Run as a script: it must not import the uireport package.
"""
import json
import os
import sys
import time
import uuid

TIMEOUT_S = 600
FOLDER = os.environ.get("UIREPORT_APPROVAL_DIR", "")
TOOL = {
    "name": "approve",
    "description": "Ask the user to allow or deny a tool call.",
    "inputSchema": {"type": "object",
                    "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"},
                                   "tool_use_id": {"type": "string"}},
                    "required": ["tool_name", "input"]},
}


def deny(message):
    return {"behavior": "deny", "message": message}


def ask(tool_name, tool_input):
    if not FOLDER or not os.path.isdir(FOLDER):
        return deny("The Show Claude window isn't available to ask the user.")
    rid = uuid.uuid4().hex
    req = os.path.join(FOLDER, rid + ".request.json")
    ans = os.path.join(FOLDER, rid + ".answer.json")
    with open(req + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"id": rid, "tool_name": tool_name, "input": tool_input, "time": time.time()}, fh)
    os.replace(req + ".tmp", req)
    end = time.time() + TIMEOUT_S
    try:
        while time.time() < end:
            if os.path.exists(ans):
                try:
                    with open(ans, encoding="utf-8") as fh:
                        answer = json.load(fh)
                except ValueError:  # still being written
                    time.sleep(0.05)
                    continue
                if answer.get("behavior") == "allow":
                    return {"behavior": "allow", "updatedInput": tool_input}
                return deny(answer.get("message") or "The user denied this.")
            time.sleep(0.1)
        return deny("No answer from the user within 10 minutes.")
    finally:
        for path in (req, ans):
            try:
                os.remove(path)
            except OSError:
                pass


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "uireport", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        args = params.get("arguments") or {}
        if params.get("name") != "approve":
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        try:
            decision = ask(str(args.get("tool_name", "")), args.get("input") or {})
        except Exception as exc:  # noqa: BLE001 - never let Claude act on a broken approval
            decision = deny(f"The approval step failed: {exc}")
        result = {"content": [{"type": "text", "text": json.dumps(decision)}]}
    elif method == "ping":
        result = {}
    elif mid is None:
        return None  # a notification (e.g. notifications/initialized)
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    # Claude Code speaks UTF-8; Python's pipe default here is the ANSI code page (cp1252), which
    # would garble every non-ASCII character in an approved edit or command.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
