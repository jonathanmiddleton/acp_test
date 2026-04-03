
"""
Validate ACP integration with copilot-language-server.

Runs through the full lifecycle: init, session creation, prompt, response.
Prints structured results for each step.

Usage:
    python3 acp_validate.py [/path/to/copilot-language-server]

If no path is given, attempts to find the binary via 'ps'.
"""

import json
import subprocess
import sys
import threading
import time
import os

# Collect all streamed updates here
UPDATES = []


def find_binary():
    """Try to locate a running copilot-language-server process."""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "command"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if "copilot-language-server" in line and "grep" not in line:
                # Extract the binary path (everything before ' --')
                parts = line.split(" --")
                return parts[0].strip()
    except Exception:
        pass
    return None


def read_ndjson(stream, collected, label="out"):
    """Read NDJSON lines from a stream into collected list."""
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            collected.append((label, msg))
        except json.JSONDecodeError:
            collected.append((label, {"_raw": line}))


def send(proc, msg):
    """Send a JSON-RPC message."""
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def drain(collected, timeout=3.0):
    """Wait for messages to arrive, return them, and clear the buffer."""
    time.sleep(timeout)
    result = list(collected)
    collected.clear()
    return result


def find_response(messages, req_id):
    """Find the JSON-RPC response matching a request id."""
    for _, msg in messages:
        if isinstance(msg, dict) and msg.get("id") == req_id:
            return msg
    return None


def find_notifications(messages, method=None):
    """Find all JSON-RPC notifications (no 'id' field)."""
    results = []
    for _, msg in messages:
        if isinstance(msg, dict) and "id" not in msg:
            if method is None or msg.get("method") == method:
                results.append(msg)
    return results


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else find_binary()
    if not binary:
        print("ERROR: Could not find copilot-language-server.")
        print("Pass the path as an argument: python3 acp_validate.py /path/to/binary")
        sys.exit(1)

    print(f"Binary: {binary}")
    print(f"CWD:    {os.getcwd()}")
    print()

    collected = []

    proc = subprocess.Popen(
        [binary, "--acp", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    threading.Thread(
        target=read_ndjson, args=(proc.stdout, collected, "out"), daemon=True
    ).start()
    threading.Thread(
        target=read_ndjson, args=(proc.stderr, collected, "err"), daemon=True
    ).start()

    time.sleep(1)
    results = {}

    # --- Step 1: Initialize ---
    print("=" * 50)
    print("STEP 1: initialize")
    print("=" * 50)
    send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientInfo": {"name": "meadow-validate", "version": "0.1.0"},
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
            },
        },
    )
    msgs = drain(collected, 2)
    resp = find_response(msgs, 1)
    if resp and "result" in resp:
        r = resp["result"]
        info = r.get("agentInfo", {})
        caps = r.get("agentCapabilities", {})
        methods = r.get("authMethods", [])
        print(f"  Agent:       {info.get('name')} v{info.get('version')}")
        print(f"  Protocol:    {r.get('protocolVersion')}")
        print(
            f"  Capabilities: loadSession={caps.get('loadSession')}, "
            f"image={caps.get('promptCapabilities', {}).get('image')}, "
            f"embeddedContext={caps.get('promptCapabilities', {}).get('embeddedContext')}"
        )
        print(f"  Signin methods: {[m.get('id') for m in methods]}")
        results["init"] = "OK"
    elif resp and "error" in resp:
        print(f"  ERROR: {resp['error']}")
        results["init"] = "FAIL"
    else:
        print(f"  No response. Raw: {msgs}")
        results["init"] = "FAIL"
    print()

    if results.get("init") != "OK":
        print("Cannot continue without init. Exiting.")
        proc.terminate()
        sys.exit(1)

    # --- Step 2: session/new ---
    print("=" * 50)
    print("STEP 2: session/new")
    print("=" * 50)
    send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {"cwd": os.getcwd(), "mcpServers": []},
        },
    )
    msgs = drain(collected, 3)
    resp = find_response(msgs, 2)
    session_id = None
    if resp and "result" in resp:
        r = resp["result"]
        session_id = r.get("sessionId")
        models = r.get("models", {}).get("availableModels", [])
        current = r.get("models", {}).get("currentModelId")
        modes = r.get("modes", {}).get("availableModes", [])
        print(f"  Session ID:  {session_id}")
        print(
            f"  Models ({len(models)}): {[m['modelId'] for m in models[:5]]}{'...' if len(models) > 5 else ''}"
        )
        print(f"  Default model: {current}")
        print(f"  Modes:       {[m['id'].split('#')[-1] for m in modes]}")
        results["session"] = "OK"
    elif resp and "error" in resp:
        print(f"  ERROR: {resp['error']}")
        results["session"] = "FAIL"
    else:
        print(f"  No response. Raw: {msgs}")
        results["session"] = "FAIL"
    print()

    if not session_id:
        print("No session. Exiting.")
        proc.terminate()
        sys.exit(1)

    # --- Step 3: session/prompt ---
    print("=" * 50)
    print("STEP 3: session/prompt")
    print("=" * 50)
    send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [
                    {"type": "text", "text": "Reply with exactly: MEADOW_ACP_OK"}
                ],
            },
        },
    )
    # Streaming — give it more time
    msgs = drain(collected, 15)
    resp = find_response(msgs, 3)
    notifs = find_notifications(msgs, "session/update")

    agent_text = ""
    thought_text = ""
    update_types = set()
    for n in notifs:
        u = n.get("params", {}).get("update", {})
        kind = u.get("sessionUpdate", "")
        update_types.add(kind)
        if kind == "agent_message_chunk":
            content = u.get("content", {})
            if content.get("type") == "text":
                agent_text += content.get("text", "")
        elif kind == "agent_thought_chunk":
            content = u.get("content", {})
            if content.get("type") == "text":
                thought_text += content.get("text", "")

    print(f"  Updates received: {len(notifs)}")
    print(f"  Update types:     {sorted(update_types)}")
    if thought_text:
        preview = thought_text[:200].replace("\n", " ")
        print(
            f"  Thought preview:  {preview}{'...' if len(thought_text) > 200 else ''}"
        )
    print(f"  Agent response:   {agent_text[:500]}")
    if resp and "result" in resp:
        print(f"  Stop reason:      {resp['result'].get('stopReason')}")
        results["prompt"] = "OK"
    elif resp and "error" in resp:
        print(f"  ERROR: {resp['error']}")
        results["prompt"] = "FAIL"
    else:
        print(f"  No final response yet (may need more time)")
        results["prompt"] = "PARTIAL"
    print()

    # --- Summary ---
    print("=" * 50)
    print("SUMMARY")
    print("=" * 50)
    for step, status in results.items():
        print(f"  {step:10s} {status}")
    print()

    all_ok = all(v == "OK" for v in results.values())
    if all_ok:
        print("All steps passed. ACP integration is viable.")
    else:
        print("Some steps failed. See details above.")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


if __name__ == "__main__":
    main()
