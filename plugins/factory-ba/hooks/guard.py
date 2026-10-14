#!/usr/bin/env python3
"""PreToolUse guard for Pipeline Factory sessions (fail-closed; stdlib only).

Denies: running `factory accept` (a person's sign-off, never an agent's), editing generated or recorded files
(build/, DAG and Terraform output, locks, acceptance and review records, the agent audit trail), and reading or
fetching secrets (.env, keys, cloud credentials, secret-store reads). Anything it cannot parse is denied.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import PurePosixPath

ACCEPT = re.compile(r"(\bfactory\b.*\baccept\b|factory_agents\.accept|\baccept_rule\b)", re.S)
SECRET_COMMANDS = re.compile(
    r"(secretsmanager\s+get-secret-value|\bvault\s+(kv\s+get|read)\b|ssm\s+get-parameter.*--with-decryption"
    r"|\b(cat|less|more|head|tail|grep|base64|cp|scp)\b[^|;&]*(\.env\b|\.pem\b|\.key\b|id_rsa|id_ed25519"
    r"|\.aws/credentials|\.netrc))",
    re.S,
)
GENERATED_NAMES = {
    "impl.lock.json",
    "review.json",
    "spec.lock.json",
    "agent-decisions.jsonl",
    "BUILD-MANIFEST.json",
    "edge_cases.json",
}
SECRET_FILES = re.compile(r"(^|/)(\.env(\..*)?|.*\.pem|.*\.key|id_rsa.*|id_ed25519.*|credentials|\.netrc)$")


def deny(reason: str) -> None:
    decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}
    print(json.dumps({"hookSpecificOutput": decision}))
    sys.exit(0)


def generated(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    return "build" in p.parts or p.name in GENERATED_NAMES or p.name.endswith(".tf.json")


def main() -> None:
    try:
        event = json.load(sys.stdin)
        tool = str(event["tool_name"])
        args = event.get("tool_input") or {}
    except (ValueError, KeyError, TypeError):
        deny("the guard could not read the tool call; refusing (fail-closed)")
        return
    if tool == "Bash":
        cmd = str(args.get("command", ""))
        if ACCEPT.search(cmd):
            deny("`factory accept` is a person's sign-off after reading the review package; agents never run it")
        if SECRET_COMMANDS.search(cmd):
            deny("reading secret values is not allowed in this session; specs hold secret references only")
    elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = str(args.get("file_path") or args.get("notebook_path") or "")
        if generated(path):
            deny(f"{path} is generated or recorded by the factory; change the spec and rebuild instead")
    elif tool == "Read":
        path = str(args.get("file_path", ""))
        if SECRET_FILES.search(path.replace("\\", "/")):
            deny(f"{path} may hold secrets; it is not read in this session")
    sys.exit(0)


if __name__ == "__main__":
    main()
