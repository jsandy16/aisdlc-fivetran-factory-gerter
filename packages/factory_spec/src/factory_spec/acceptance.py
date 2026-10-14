"""A person's acceptance of a custom rule (ADR-0020): a digest over what was reviewed, kept in impl.lock.json.

digest = sha256 of the canonical {contract, plain_english, code_sha256, tests_sha256}. Any change to the rule's
contract, its plain English, its code or its tests invalidates the acceptance (the validator reports it).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from factory_core.canonical import canonical_bytes

LOCK = "impl.lock.json"


def contract_of(rule: Any) -> dict[str, Any]:
    impl = rule.implementation
    return {
        "rule_id": rule.rule_id,
        "inputs": list(rule.inputs),
        "outputs": list(rule.outputs),
        "output_kinds": dict(sorted(impl.output_kinds.items())),
        "on_failure": rule.on_failure.model_dump(mode="json") if rule.on_failure else None,
    }


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def acceptance_digest(rule: Any, rule_dir: Path) -> dict[str, Any]:
    body = {
        "contract": contract_of(rule),
        "plain_english": rule.plain_english,
        "code_sha256": file_sha(rule_dir / "impl.py"),
        "tests_sha256": file_sha(rule_dir / "test_impl.py"),
    }
    return {**body, "digest": hashlib.sha256(canonical_bytes(body)).hexdigest()}


def read_lock(rule_dir: Path) -> dict[str, Any] | None:
    f = rule_dir / LOCK
    if not f.is_file():
        return None
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def acceptance_problem(rule: Any, rule_dir: Path) -> str | None:
    """None when a person accepted exactly this rule; otherwise why not."""
    lock = read_lock(rule_dir)
    if lock is None:
        return "no person has accepted this custom rule (factory accept)"
    if lock.get("digest") != acceptance_digest(rule, rule_dir)["digest"]:
        return "the rule's contract, plain English, code or tests changed since it was accepted"
    if lock.get("review", {}).get("verdict") != "approve":
        return "the acceptance has no approving review"
    return None
