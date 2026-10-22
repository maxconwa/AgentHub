"""A stand-in for `claude -p --output-format stream-json` used by runner tests.

Behavior is chosen by FAKE_CLAUDE_MODE: success | max_turns_then_success | sleep | fail | review_approve |
limit_then_success | hold | prompt_share.
review_approve records an ``approve`` verdict on $AGENTHUB_TASK_ID through the hub at $FAKE_HUB_URL with
the agent token in $FAKE_TOKEN (as the real session would through the MCP tool), then succeeds.
limit_then_success: the first call (no --resume) ends with the subscription usage-limit message
("You've hit your session limit · resets <now+2min>", or $FAKE_LIMIT_TEXT when set); a resumed call succeeds.
A --resume'd call keeps that session id, unless --fork-session asks for a new one.
hold: waits until the file $FAKE_RELEASE exists (at most 30 s), then succeeds.
prompt_share: in a prompt session ($AGENTHUB_PROMPT_ID set) it reads the prompt, posts in its channel thread and
claims a share titled "Share for $FAKE_NAME" through the hub (as the real session would through the MCP tools),
then succeeds; any other session just succeeds.
Each invocation appends {"argv", "stdin", "cwd", "session", "env", "env_names"} as a JSON line to
FAKE_CLAUDE_LOG: ``env`` holds the AGENTHUB_* variables, ``env_names`` the names (never the values) of every
variable it got.
"""

import json
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta


def emit(obj):
    print(json.dumps(obj), flush=True)


def hub(method, path, payload=None):
    """Call the hub's agent API as $FAKE_TOKEN's agent."""
    req = urllib.request.Request(
        f"{os.environ['FAKE_HUB_URL']}/api/v1{path}", method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {os.environ['FAKE_TOKEN']}", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read() or b"null")


def main():
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
    max_turns = int(argv[argv.index("--max-turns") + 1])
    session = str(uuid.uuid4()) if resume is None or "--fork-session" in argv else resume
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a") as fh:
            env = {k: v for k, v in os.environ.items() if k.startswith("AGENTHUB_")}
            fh.write(json.dumps({"argv": argv, "stdin": prompt, "cwd": os.getcwd(), "session": session, "env": env,
                                 "env_names": sorted(os.environ)}) + "\n")
    mode = os.environ.get("FAKE_CLAUDE_MODE", "success")

    emit({"type": "system", "subtype": "init", "session_id": session, "tools": ["Bash"]})
    emit({"type": "assistant", "session_id": session, "message": {"content": [
        {"type": "text", "text": "Looking at the code now."}]}})
    emit({"type": "assistant", "session_id": session, "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la"}}]}})

    if mode == "sleep":
        time.sleep(60)
    if mode == "hold":
        deadline = time.monotonic() + 30
        while not os.path.exists(os.environ["FAKE_RELEASE"]) and time.monotonic() < deadline:
            time.sleep(0.05)
    if mode == "fail":
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": session,
              "num_turns": 1, "errors": ["boom"]})
        sys.exit(1)
    if mode == "max_turns_then_success" and resume is None:
        emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "session_id": session,
              "num_turns": max_turns, "total_cost_usd": 0.01})
        sys.exit(1)
    if mode == "limit_then_success" and resume is None:
        reset = (datetime.now() + timedelta(minutes=2)).strftime("%I:%M%p").lstrip("0").lower()
        text = os.environ.get("FAKE_LIMIT_TEXT") or f"You've hit your session limit · resets {reset}"
        emit({"type": "result", "subtype": "success", "is_error": True, "session_id": session, "num_turns": 1,
              "total_cost_usd": 0, "result": text})
        sys.exit(1)
    if mode == "review_approve":
        hub("POST", f"/tasks/{os.environ['AGENTHUB_TASK_ID']}/verdict", {"verdict": "approve", "notes": "looks good"})
    if mode == "prompt_share" and os.environ.get("AGENTHUB_PROMPT_ID"):
        pid = os.environ["AGENTHUB_PROMPT_ID"]
        name = os.environ.get("FAKE_NAME", "agent")
        thread = hub("GET", f"/prompts/{pid}")["thread"]
        if thread and thread["kind"] == "channel":
            hub("POST", f"/projects/{thread['project']}/messages",
                {"channel": thread["channel"], "body": f"{name} here: I'll take my part."})
        hub("POST", f"/prompts/{pid}/claim", {"title": f"Share for {name}", "body": f"{name}'s part of the work."})
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session, "num_turns": 2,
          "total_cost_usd": 0.02, "result": "Implemented the change and ran the tests."})


if __name__ == "__main__":
    main()
