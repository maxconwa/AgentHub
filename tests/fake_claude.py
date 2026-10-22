"""A stand-in for `claude -p --output-format stream-json` used by runner tests.

Behavior is chosen by FAKE_CLAUDE_MODE: success | max_turns_then_success | sleep | fail | review_approve.
review_approve records an ``approve`` verdict on $AGENTHUB_TASK_ID through the hub at $FAKE_HUB_URL with
the agent token in $FAKE_TOKEN (as the real session would through the MCP tool), then succeeds.
Each invocation appends {"argv", "stdin", "env"} as a JSON line to FAKE_CLAUDE_LOG.
"""

import json
import os
import sys
import time
import urllib.request
import uuid


def emit(obj):
    print(json.dumps(obj), flush=True)


def main():
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a") as fh:
            env = {k: v for k, v in os.environ.items() if k.startswith("AGENTHUB_")}
            fh.write(json.dumps({"argv": argv, "stdin": prompt, "env": env}) + "\n")
    resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
    max_turns = int(argv[argv.index("--max-turns") + 1])
    session = resume or str(uuid.uuid4())
    mode = os.environ.get("FAKE_CLAUDE_MODE", "success")

    emit({"type": "system", "subtype": "init", "session_id": session, "tools": ["Bash"]})
    emit({"type": "assistant", "session_id": session, "message": {"content": [
        {"type": "text", "text": "Looking at the code now."}]}})
    emit({"type": "assistant", "session_id": session, "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la"}}]}})

    if mode == "sleep":
        time.sleep(60)
    if mode == "fail":
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": session,
              "num_turns": 1, "errors": ["boom"]})
        sys.exit(1)
    if mode == "max_turns_then_success" and resume is None:
        emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "session_id": session,
              "num_turns": max_turns, "total_cost_usd": 0.01})
        sys.exit(1)
    if mode == "review_approve":
        req = urllib.request.Request(
            f"{os.environ['FAKE_HUB_URL']}/api/v1/tasks/{os.environ['AGENTHUB_TASK_ID']}/verdict",
            data=json.dumps({"verdict": "approve", "notes": "looks good"}).encode(), method="POST",
            headers={"Authorization": f"Bearer {os.environ['FAKE_TOKEN']}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session, "num_turns": 2,
          "total_cost_usd": 0.02, "result": "Implemented the change and ran the tests."})


if __name__ == "__main__":
    main()
