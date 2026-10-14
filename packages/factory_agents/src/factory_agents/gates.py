"""Gates for a custom rule (ADR-0020): deterministic checks that must all pass before review and acceptance.

hash · import allow-list · plugin contract · owner test cases · agent edge cases · test_impl.py (pytest) ·
mypy --strict · bandit. mutmut is opt-in (not a gate in Phase 4).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory_spec.imports import check_plugin_source

EDGE_CASES = "edge_cases.json"
REVIEW = "review.json"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class GateReport:
    rule_id: str
    code_sha256: str
    tests_sha256: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def issues(self) -> list[str]:
        return [f"gate {c.name}: {c.detail}" for c in self.checks if not c.ok]

    def as_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "ok": self.ok, "code_sha256": self.code_sha256,
                "tests_sha256": self.tests_sha256, "checks": [c.__dict__ for c in self.checks]}  # fmt: skip


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _tool(args: list[str], cwd: Path, timeout: int = 300) -> tuple[bool, str]:
    proc = subprocess.run(  # noqa: S603 - fixed tool invocations on the rule's own files
        [sys.executable, "-m", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    return proc.returncode == 0, " | ".join(tail)[:400]


def edge_cases(rule_dir: Path) -> list[dict[str, Any]]:
    f = rule_dir / EDGE_CASES
    data = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []
    return data if isinstance(data, list) else []


def run_gates(pipeline_dir: Path, rule: Any, *, tools: bool = True) -> GateReport:
    from factory_core.errors import FactoryError
    from factory_runtime.plugins import load_plugin, run_case

    d = pipeline_dir / "rules" / rule.rule_id
    code, tests = d / "impl.py", d / "test_impl.py"
    rep = GateReport(rule.rule_id, _sha(code), _sha(tests))
    add = rep.checks.append
    if not code.is_file():
        add(Check("code_present", False, "rules/<rule_id>/impl.py is missing"))
        return rep
    add(Check("code_hash", rep.code_sha256 == rule.implementation.code_sha256,
              "impl.py does not match code_sha256 in rules.yaml"))  # fmt: skip
    findings = check_plugin_source(code.read_text(encoding="utf-8"))
    add(Check("import_allow_list", not findings, "; ".join(f"line {f.line}: {f.msg}" for f in findings[:5])))
    if not rep.ok:
        return rep  # never execute code that failed the hash or the allow-list
    try:
        load_plugin(pipeline_dir, rule.rule_id, rep.code_sha256)
        add(Check("contract", True))
    except FactoryError as exc:
        add(Check("contract", False, f"{exc.code}: {exc}"))
        return rep
    with tempfile.TemporaryDirectory(prefix="factory-gates-") as tmp:
        for label, cases in (("owner_cases", [c.model_dump(mode="json") for c in rule.test_cases]),
                             ("edge_cases", edge_cases(d))):  # fmt: skip
            bad = []
            for c in cases:
                try:
                    ok, why = run_case(
                        pipeline_dir, rule, c.get("given", {}), c.get("expect", {}), Path(tmp) / c["name"]
                    )
                except Exception as exc:  # a crash in the rule is a failed case
                    ok, why = False, f"raised {type(exc).__name__}"
                if not ok:
                    bad.append(f"{c['name']}: {why}")
            add(Check(label, not bad, "; ".join(bad[:5])))
    if tools:
        if tests.is_file():
            add(Check("test_impl", *_tool(["pytest", "-q", "-p", "no:cacheprovider", "test_impl.py"], d)))
        else:
            add(Check("test_impl", False, "rules/<rule_id>/test_impl.py is missing"))
        add(Check("mypy_strict", *_tool(["mypy", "--strict", "--no-incremental", "--cache-dir", "/dev/null",
                                         "impl.py"], d)))  # fmt: skip
        add(Check("bandit", *_tool(["bandit", "-q", "-ll", "impl.py"], d)))
    return rep
