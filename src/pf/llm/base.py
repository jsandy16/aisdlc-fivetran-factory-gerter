"""Provider-agnostic structured LLM calls with strict pydantic validation.

Providers (PF_LLM_PROVIDER):
  agent-sdk  Claude Agent SDK; runs the named plugin skill; structured output (default)
  replay     recorded outputs from fixtures (tests, CI) — no network, fully deterministic
  record     agent-sdk, then saves the output as a replay fixture
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from ..envelope import PfError

T = TypeVar("T", bound=BaseModel)
PLUGIN_NAME = "pipeline-factory-steps"  # headless-only plugin: interactive sessions never see these skills
PROMPT_VERSION = "1"


class LLMMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    task: str
    skill: str
    skill_sha256: str
    prompt_sha256: str
    schema_sha256: str
    model: str
    prompt_version: str = PROMPT_VERSION
    cost_usd: float | None = None
    turns: int | None = None
    duration_s: float


def plugin_root() -> Path:
    env = os.environ.get("PF_PLUGIN_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "steps-plugin" / ".claude-plugin" / "plugin.json").exists():
            return p / "steps-plugin"
    raise PfError("pipeline-factory plugin not found; set PF_PLUGIN_ROOT", "no_plugin")


def skill_sha(skill: str) -> str:
    f = plugin_root() / "skills" / skill / "SKILL.md"
    if not f.exists():
        raise PfError(f"skill '{skill}' not found at {f}", "no_skill")
    return hashlib.sha256(f.read_bytes()).hexdigest()


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON Schema for structured output: draft-07 compatible, additionalProperties false everywhere,
    no numeric/length constraints (enforced by pydantic after the call instead)."""
    schema = model.model_json_schema()

    constraint_keys = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "pattern",
                       "multipleOf", "title", "default", "maxItems")

    def clean(node):
        """Walks schema nodes only: keys inside a `properties` map are field names, never keywords to strip."""
        if isinstance(node, list):
            for v in node:
                clean(v)
            return
        if not isinstance(node, dict):
            return
        for k in constraint_keys:
            node.pop(k, None)
        if node.get("type") == "array" and node.get("minItems", 0) > 1:
            node["minItems"] = 1
        if "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
            for sub in node["properties"].values():
                clean(sub)
        for k in ("items", "anyOf", "oneOf", "allOf", "not", "additionalItems", "prefixItems"):
            if k in node:
                clean(node[k])

    defs = schema.pop("$defs", {})

    def inline(node):  # $ref/$defs are not reliably honoured by every structured-output path: inline them
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(dict(defs[node["$ref"].rsplit("/", 1)[-1]]))
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    schema = inline(schema)
    clean(schema)
    return schema


def _key(task: str, skill: str, prompt: str, schema: dict) -> str:
    """Everything that can change the answer: task, skill name AND content, prompt, schema, prompt version, model.
    Editing a SKILL.md or switching model therefore needs a new recording, never a stale replay."""
    from .agent_sdk import settings

    return hashlib.sha256(json.dumps([task, skill, skill_sha(skill), prompt, schema, PROMPT_VERSION, settings()["model"]],
                                     sort_keys=True).encode()).hexdigest()


def fence(tag: str, text: str) -> str:
    """Wrap untrusted text in <tag> delimiters it cannot close or reopen."""
    import re

    safe = re.sub(rf"<\s*/?\s*{re.escape(tag)}\b[^>]*>", f"[{tag}]", text, flags=re.IGNORECASE)
    return f"<{tag}>\n{safe}\n</{tag}>"


SECRET_PATTERNS = [
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"\bAKIA[0-9A-Z]{16}\b", r"\bASIA[0-9A-Z]{16}\b",
    r"(?i)\b(password|passwd|pwd|secret|api[_ -]?key|token|access[_ -]?key)\b(\s*(is|:|=)\s*)([^\s,;]{4,})",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b",
]


def redact_secrets(text: str) -> tuple[str, int]:
    """Deterministic scrub before any text leaves for the model: keys, AWS key ids, 'password: x' style values."""
    import re

    n = 0
    for pat in SECRET_PATTERNS:
        def repl(m):
            nonlocal n
            n += 1
            return (m.group(1) + m.group(2) + "[REDACTED]") if m.lastindex and m.lastindex >= 4 else "[REDACTED]"
        text = re.sub(pat, repl, text)
    return text, n


def _canon_sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fixtures_dir() -> Path:
    return Path(os.environ.get("PF_LLM_FIXTURES", "tests/llm-fixtures"))


def run_structured(task: str, skill: str, prompt: str, output: type[T], *, log_dir: Path | None = None) -> tuple[T, LLMMeta]:
    provider = os.environ.get("PF_LLM_PROVIDER", "agent-sdk")
    schema = strict_schema(output)
    key = _key(task, skill, prompt, schema)
    started = time.time()
    meta_extra: dict = {}
    recorded: dict = {}
    if provider == "replay":
        f = fixtures_dir() / task / f"{key}.json"
        if not f.exists():
            raise PfError(f"no recorded LLM output for this request ({f}); record it with PF_LLM_PROVIDER=record", "no_fixture")
        rec = json.loads(f.read_text(encoding="utf-8"))
        raw = rec["output"]
        if rec.get("output_sha256") != _canon_sha(raw):
            raise PfError(f"recorded LLM output {f} was edited after recording", "fixture_tampered")
        recorded = rec.get("provenance", {})  # replay reports how the answer was really produced
        model_name = recorded.get("model", "unknown")
        meta_extra = {k: recorded.get(k) for k in ("cost_usd", "turns")}
    elif provider in ("agent-sdk", "record"):
        from .agent_sdk import call

        raw, model_name, meta_extra = call(skill, prompt, schema)
        if provider == "record":
            f = fixtures_dir() / task / f"{key}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            prov = {"model": model_name, "skill_sha256": skill_sha(skill), "prompt_version": PROMPT_VERSION,
                    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **meta_extra}
            f.write_text(json.dumps({"task": task, "skill": skill, "provenance": prov, "output": raw,
                                     "output_sha256": _canon_sha(raw)}, indent=2, sort_keys=True), encoding="utf-8")
    else:
        raise PfError(f"unknown PF_LLM_PROVIDER '{provider}'", "invalid")
    try:
        obj = output.model_validate(raw)
    except ValidationError as exc:
        raise PfError(f"{task}: the model's output did not match the {output.__name__} schema: {exc.errors()[:3]}", "llm_schema") from None
    meta = LLMMeta(provider=provider if provider != "replay" else f"replay:{recorded.get('recorded_at', '?')}", task=task, skill=skill,
                   skill_sha256=recorded.get("skill_sha256", skill_sha(skill)),
                   prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), schema_sha256=hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
                   model=model_name, duration_s=round(time.time() - started, 2), **meta_extra)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "llm-calls.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"meta": meta.model_dump(), "output": obj.model_dump(mode="json")}, sort_keys=True) + "\n")
    return obj, meta
