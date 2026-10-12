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
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from ..envelope import PfError

T = TypeVar("T", bound=BaseModel)
PLUGIN_NAME = "pipeline-factory"
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
        if (p / "plugin" / ".claude-plugin" / "plugin.json").exists():
            return p / "plugin"
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
    return hashlib.sha256(json.dumps([task, skill, prompt, schema, PROMPT_VERSION], sort_keys=True).encode()).hexdigest()


def fixtures_dir() -> Path:
    return Path(os.environ.get("PF_LLM_FIXTURES", "tests/llm-fixtures"))


def run_structured(task: str, skill: str, prompt: str, output: type[T], *, log_dir: Path | None = None) -> tuple[T, LLMMeta]:
    provider = os.environ.get("PF_LLM_PROVIDER", "agent-sdk")
    schema = strict_schema(output)
    key = _key(task, skill, prompt, schema)
    started = time.time()
    meta_extra: dict = {}
    if provider == "replay":
        f = fixtures_dir() / task / f"{key}.json"
        if not f.exists():
            raise PfError(f"no recorded LLM output for this request ({f}); record it with PF_LLM_PROVIDER=record", "no_fixture")
        raw = json.loads(f.read_text(encoding="utf-8"))["output"]
        model_name = "replay"
    elif provider in ("agent-sdk", "record"):
        from .agent_sdk import call

        raw, model_name, meta_extra = call(skill, prompt, schema)
        if provider == "record":
            f = fixtures_dir() / task / f"{key}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"task": task, "skill": skill, "output": raw}, indent=2, sort_keys=True), encoding="utf-8")
    else:
        raise PfError(f"unknown PF_LLM_PROVIDER '{provider}'", "invalid")
    try:
        obj = output.model_validate(raw)
    except ValidationError as exc:
        raise PfError(f"{task}: the model's output did not match the {output.__name__} schema: {exc.errors()[:3]}", "llm_schema") from None
    meta = LLMMeta(provider=provider, task=task, skill=skill, skill_sha256=skill_sha(skill),
                   prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), schema_sha256=hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
                   model=model_name, duration_s=round(time.time() - started, 2), **meta_extra)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "llm-calls.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"meta": meta.model_dump(), "output": obj.model_dump(mode="json")}, sort_keys=True) + "\n")
    return obj, meta
