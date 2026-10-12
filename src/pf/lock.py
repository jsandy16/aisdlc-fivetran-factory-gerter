"""Resolve a valid bundle into spec.lock.json — the single canonical document builds and the runtime read.

The lock contains only what the connector needs at runtime (no access principals, no audit buckets),
plus the compiled form of every English rule taken from rules.lock.yaml.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import VERSION
from .bundle import Bundle, collect_secrets
from .envelope import Issue, PfError
from .rulecompiler import RulesLock, english_rules, load_rules_lock, text_sha


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def spec_sha(b: Bundle) -> str:
    return sha256({k: v for k, v in sorted(b.raw.items())})


def english_status(b: Bundle, rl: RulesLock | None) -> list[Issue]:
    """Every English rule must be compiled for its current text and accepted by a person."""
    issues: list[Issue] = []
    for ref in english_rules(b):
        entry = rl.rules.get(ref.rule_id) if rl else None
        where = f"{ref.file}:{ref.loc}"
        if entry is None:
            issues.append(Issue(file="rules.lock.yaml", loc=ref.rule_id, code="not_compiled",
                                msg=f"English rule '{ref.rule_id}' ({where}) is not compiled: run `pf rules compile {b.name}`"))
        elif entry.text_sha256 != text_sha(ref.text, ref.examples):
            issues.append(Issue(file="rules.lock.yaml", loc=ref.rule_id, code="stale",
                                msg=f"English rule '{ref.rule_id}' changed since it was compiled: run `pf rules compile {b.name}` and accept again"))
        elif entry.status != "accepted":
            issues.append(Issue(file="rules.lock.yaml", loc=ref.rule_id, code="not_accepted",
                                msg=f"English rule '{ref.rule_id}' is compiled but not accepted: its owner reviews it and runs `pf rules accept {b.name} {ref.rule_id} --by <@handle>`"))
        elif entry.acceptance_sha256 != entry.acceptance_digest():
            issues.append(Issue(file="rules.lock.yaml", loc=ref.rule_id, code="acceptance_invalid",
                                msg=f"English rule '{ref.rule_id}' was changed after it was accepted (expression, cases or status edited): "
                                    f"compile and accept it again"))
        else:
            from .rulecompiler import reverify

            problems = reverify(ref, entry)
            if problems:
                issues.append(Issue(file="rules.lock.yaml", loc=ref.rule_id, code="acceptance_invalid",
                                    msg=f"English rule '{ref.rule_id}' no longer verifies with this runtime: {'; '.join(problems)}"))
    return issues


def build_lock(b: Bundle) -> dict:
    if not b.ok:
        raise PfError("the spec bundle has errors; run `pf validate` first", "invalid", [i for i in b.issues if i.severity == "error"])
    rl = load_rules_lock(b.path)
    issues = english_status(b, rl)
    if issues:
        raise PfError("English rules are not ready to build", "rules_not_ready", issues)
    p, s, t, pol = b.pipeline, b.source, b.sttm, b.policy
    assert p and s and t and pol
    compiled = {rid: e.expression for rid, e in (rl.rules.items() if rl else [])}

    entities = []
    for e in t.entities:
        ed = e.model_dump(mode="json")
        for a in ed["attributes"]:
            for c in a["checks"]:
                if c["kind"] == "english":
                    c["compiled_expr"] = compiled[c["id"]]
        for c in ed["row_checks"]:
            if c["kind"] == "english":
                c["compiled_expr"] = compiled[c["id"]]
        entities.append(ed)
    source = s.source.model_dump(mode="json")
    if source["type"] in ("sftp", "ftp"):
        for c in source["package"]["checks"]:
            if c["kind"] == "english":
                c["compiled_expr"] = compiled[c["id"]]

    shared = collect_secrets(b, include_originals=False)
    originals = {}
    secrets: dict[str, list[str]] = {}
    for env in p.environments:
        ep = pol.environments[env]
        keys = list(shared)
        if pol.archive_originals and b.is_file_source and ep.originals:
            originals[env] = ep.originals.model_dump(mode="json")
            keys += [ep.originals.access_key_id.secret, ep.originals.secret_access_key.secret]
        secrets[env] = sorted(set(keys))
    lock = {
        "lock_version": 1,
        "pipeline": p.name,
        "version": p.version,
        "title": p.title,
        "gxp_tier": p.gxp_tier,
        "late_data_policy": p.requirement.late_data_policy,
        "classification": {k: v for k, v in sorted(pol.classification.items()) if v != pol.default_classification},
        "default_classification": pol.default_classification,
        "environments": list(p.environments),
        "runtime": p.runtime.model_dump(mode="json"),
        "source": source,
        "entities": entities,
        "archive_originals": bool(pol.archive_originals and b.is_file_source),
        "originals": originals,
        "secrets": secrets,
        "spec_sha256": spec_sha(b),
        "rules_lock_sha256": rl.sha256() if rl else None,
        "framework_version": VERSION,
    }
    from pf_runtime import VERSION as RT

    lock["runtime_version"] = RT
    return lock
