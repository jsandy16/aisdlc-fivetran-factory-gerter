"""The cross-file checks. Each check is a plain function `(Context) -> Iterable[Issue]` so it can report many findings
and warnings at once (pydantic validators stop at the first error and cannot see other files)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from factory_core.envelope import Issue
from factory_core.globmatch import GlobError, check_glob
from factory_core.pathmatch import may_overlap, parse_path_pattern
from factory_core.templates import TemplateError, parse_template

from factory_core import cel, units

from ..acceptance import acceptance_problem
from ..catalog import engine_table, op_catalog
from ..codes import issue
from ..derive import ArtifactGraph, derive_artifacts
from ..estimate import allowed_engines, estimate_lambda_seconds
from ..files import SPEC_DIR, SPEC_FILES
from ..imports import check_plugin_source
from ..repo import Repo
from ..sections.fields import FieldSpec
from ..sections.integrations import PortalResolve, StaticResolve
from ..sections.security import CLASSIFICATION_ORDER
from ..spec import PipelineSpec

P, R, I, S, E, D = (  # noqa: E741 - file-name constants, one letter per spec file
    "pipeline.yaml",
    "rules.yaml",
    "integrations.yaml",
    "security.yaml",
    "environments.yaml",
    "destinations.yaml",
)
RUN_FIELDS = frozenset({"run_key", "run_date", "env", "pipeline_id", "team", "attempt"})
OBJECT_FIELDS = frozenset({"bucket", "key", "size", "version_id", "sequencer"})
SCHEDULE_FIELDS = frozenset({"data_interval_start", "run_date"})
NAMING_BUILTINS = frozenset({"pipeline_id", "team", "env", "run_date", "seq", "rev"})
SECRET_PATH_TOKENS = frozenset({"env", "team", "pipeline_id"})
_SAFE_SCHEDULE = frozenset({"data_interval_start", "run_date"})


@dataclass
class Context:
    spec: PipelineSpec
    repo: Repo
    _graph: ArtifactGraph | None = field(default=None, init=False)

    @property
    def graph(self) -> ArtifactGraph:
        if self._graph is None:
            self._graph = derive_artifacts(self.spec)
        return self._graph

    @cached_property
    def variables(self) -> dict[str, str]:
        return dict(self.spec.pipeline.variables)

    @property
    def envs(self) -> list[str]:
        return self.repo.environments

    @cached_property
    def enabled_envs(self) -> list[str]:
        envs = self.spec.environments.environments
        return [e for e in self.envs if e in envs and envs[e].enabled]

    def builtin_rules(self, op: str) -> list[Any]:
        return [r for r in self.spec.rules.rules if r.implementation.kind == "builtin" and r.implementation.op == op]

    def trigger_field(self, source: str) -> FieldSpec | None:
        parts = source.split(".")
        trig = self.spec.pipeline.trigger
        if parts[0] != "trigger" or len(parts) != 3:
            return None
        if parts[1] == "message" and trig.type == "sqs":
            return trig.message.fields.get(parts[2])
        if parts[1] == "conf" and trig.type == "manual":
            return trig.conf.fields.get(parts[2])
        return None

    def variable_constrained(self, name: str) -> bool:
        src = self.variables.get(name, "")
        if src.startswith("trigger.schedule."):
            return src.rsplit(".", 1)[1] in _SAFE_SCHEDULE
        f = self.trigger_field(src)
        return f is not None and (f.pattern is not None or f.type in ("enum", "integer"))

    def trigger_sourced(self, name: str) -> bool:
        return self.variables.get(name, "").startswith("trigger.")

    @cached_property
    def portal_values(self) -> set[tuple[str, str]]:
        portal = self.spec.integrations.portal
        if portal is None:
            return set()
        return {(e.id, v) for e in portal.endpoints for v in e.mapping.values}

    def variable_resolvable(self, name: str) -> bool:
        src = self.variables.get(name)
        if src is None:
            return False
        parts = src.split(".")
        trig = self.spec.pipeline.trigger
        if parts[0] != "trigger":
            return (parts[1], parts[2]) in self.portal_values
        kind, fld = parts[1], parts[2]
        if kind == "message":
            return trig.type == "sqs" and fld in trig.message.fields
        if kind == "conf":
            return trig.type == "manual" and fld in trig.conf.fields
        if kind == "object":
            return trig.type == "s3_event" and fld in OBJECT_FIELDS
        if kind == "schedule":
            return trig.type == "schedule" and fld in SCHEDULE_FIELDS
        return False


Check = Callable[[Context], Iterable[Issue]]

# ---------------------------------------------------------------- identity


def check_meta(ctx: Context) -> Iterable[Issue]:
    md = ctx.spec.pipeline.metadata
    if ctx.spec.path.name != md.pipeline_id:
        yield issue(
            "pipeline_id_folder_mismatch",
            P,
            "metadata.pipeline_id",
            f"folder is '{ctx.spec.path.name}' but pipeline_id is '{md.pipeline_id}'",
        )
    if ctx.spec.pipeline.open_questions:
        yield issue(
            "open_questions_pending",
            P,
            "open_questions",
            f"{len(ctx.spec.pipeline.open_questions)} open question(s) block generation",
        )
    team = ctx.repo.config.team
    if team and md.team != team:
        yield issue(
            "team_mismatch", P, "metadata.team", f"metadata.team is '{md.team}' but this repository belongs to '{team}'"
        )


def _dupes(ids: list[str]) -> set[str]:
    seen: set[str] = set()
    return {i for i in ids if i in seen or seen.add(i)}  # type: ignore[func-returns-value]


def check_ids(ctx: Context) -> Iterable[Issue]:
    sp = ctx.spec
    groups: list[tuple[str, str, list[str]]] = [
        (R, "rules", [r.rule_id for r in sp.rules.rules]),
        (P, "orchestration.tasks", [t.task_id for t in sp.pipeline.orchestration.tasks]),
        (I, "sources", [s.id for s in sp.integrations.sources]),
        (I, "destinations", [d.id for d in sp.integrations.destinations]),
        (I, "apis", [a.id for a in sp.integrations.apis]),
    ]
    if sp.integrations.portal:
        groups.append((I, "portal.endpoints", [e.id for e in sp.integrations.portal.endpoints]))
    groups.extend((I, f"apis[{api.id}].endpoints", [e.id for e in api.endpoints]) for api in sp.integrations.apis)
    groups.extend(
        (D, f"destinations.{env}", [e.id for e in entries]) for env, entries in sp.destinations.destinations.items()
    )
    for f, loc, ids in groups:
        for d in sorted(_dupes(ids)):
            yield issue("duplicate_id", f, loc, f"id '{d}' is used more than once")


# ---------------------------------------------------------------- ops and tasks


def check_ops(ctx: Context) -> Iterable[Issue]:
    for r in ctx.spec.rules.rules:
        impl = r.implementation
        if impl.kind == "builtin":
            c = op_catalog().get(impl.op)
            if c is not None and c.status == "planned":
                yield issue(
                    "op_not_available",
                    R,
                    f"rules[{r.rule_id}].implementation.op",
                    f"op '{impl.op}' is planned but not available yet ({c.summary})",
                )


def check_tasks(ctx: Context) -> Iterable[Issue]:
    sp = ctx.spec
    rule_ids = {r.rule_id for r in sp.rules.rules}
    tasks = {t.task_id: t for t in sp.pipeline.orchestration.tasks}
    seen: dict[str, str] = {}
    for t in sp.pipeline.orchestration.tasks:
        loc = f"orchestration.tasks[{t.task_id}]"
        for rid in t.rules:
            if rid not in rule_ids:
                yield issue("unresolved_reference", P, f"{loc}.rules", f"rule '{rid}' does not exist in rules.yaml")
            elif rid in seen and seen[rid] != t.task_id:
                yield issue(
                    "rule_assigned_twice", P, f"{loc}.rules", f"rule '{rid}' is already run by task '{seen[rid]}'"
                )
            else:
                seen[rid] = t.task_id
        for d in t.depends_on:
            if d not in tasks:
                yield issue("unresolved_reference", P, f"{loc}.depends_on", f"task '{d}' does not exist")
    for r in sp.rules.rules:
        if r.rule_id not in seen:
            yield issue("rule_unassigned", R, f"rules[{r.rule_id}]", f"rule '{r.rule_id}' is not run by any task")
    # cycles
    state: dict[str, int] = {}

    def visit(tid: str, path: list[str]) -> list[str] | None:
        if state.get(tid) == 1:
            return [*path, tid]
        if state.get(tid) == 2 or tid not in tasks:
            return None
        state[tid] = 1
        for d in tasks[tid].depends_on:
            cyc = visit(d, [*path, tid])
            if cyc:
                return cyc
        state[tid] = 2
        return None

    for tid in sorted(tasks):
        cyc = visit(tid, [])
        if cyc:
            yield issue(
                "task_cycle", P, "orchestration.tasks", f"tasks depend on each other in a circle: {' -> '.join(cyc)}"
            )
            break


# ---------------------------------------------------------------- variables, templates, expressions


def check_variables(ctx: Context) -> Iterable[Issue]:
    trig = ctx.spec.pipeline.trigger
    for name, src in ctx.variables.items():
        if not ctx.variable_resolvable(name):
            yield issue(
                "variable_unresolvable", P, f"variables.{name}", f"'{src}' does not exist for a {trig.type} trigger"
            )


def _tokens(
    ctx: Context,
    f: str,
    loc: str,
    text: str,
    allowed_builtins: frozenset[str],
    *,
    path: bool,
    only: set[str] | None = None,
) -> Iterable[Issue]:
    try:
        t = parse_template(text)
    except TemplateError as exc:
        yield issue("template_syntax", f, loc, str(exc))
        return
    for tok in t.tokens:
        n = tok.name
        if n in allowed_builtins:
            continue
        if only is not None and n not in only:
            yield issue(
                "template_token_not_allowed",
                f,
                loc,
                f"'{{{n}}}' is not allowed here; allowed: {sorted(only | set(allowed_builtins))}",
            )
            continue
        if n not in ctx.variables:
            yield issue("template_unknown_token", f, loc, f"'{{{n}}}' is not a declared variable")
            continue
        if path and ctx.variable_resolvable(n) and not ctx.variable_constrained(n):
            yield issue(
                "template_token_unconstrained",
                f,
                loc,
                f"'{{{n}}}' is used in a path but its trigger field has no pattern",
            )


def check_templates(ctx: Context) -> Iterable[Issue]:
    sp = ctx.spec
    # run key: trigger-sourced variables only
    trigger_vars = {n for n in ctx.variables if ctx.trigger_sourced(n)}
    rk = sp.pipeline.reliability.run_key
    rk_issues = list(
        _tokens(ctx, P, "reliability.run_key", rk, frozenset({"pipeline_id"}), path=False, only=trigger_vars)
    )
    for i in rk_issues:
        yield (
            issue("run_key_invalid", P, i.loc, i.msg)
            if i.code in ("template_token_not_allowed", "template_unknown_token")
            else i
        )
    for r in sp.rules.rules:
        impl = r.implementation
        if impl.kind == "builtin" and impl.op == "naming.render":
            yield from _tokens(
                ctx,
                R,
                f"rules[{r.rule_id}].implementation.params.template",
                impl.params.template,
                NAMING_BUILTINS,
                path=True,
            )
    for api_loc, api in _apis(ctx):
        for e in api.endpoints:
            eloc = f"{api_loc}.endpoints[{e.id}]"
            yield from _tokens(ctx, I, f"{eloc}.path", e.path, frozenset({"env", "pipeline_id"}), path=True)
            for k, q in e.query.items():
                yield from _tokens(ctx, I, f"{eloc}.query.{k}", q, frozenset({"env", "pipeline_id"}), path=False)
            for j, x in enumerate(e.mapping.expect):
                yield from _tokens(ctx, I, f"{eloc}.mapping.expect[{j}].equals", x.equals, frozenset(), path=False)
    for name, ref in sp.integrations.secrets.items():
        yield from _tokens(ctx, I, f"secrets.{name}.path", ref.path, SECRET_PATH_TOKENS, path=False, only=set())
    for slot in sp.integrations.destinations:
        if isinstance(slot.resolve, StaticResolve):
            yield from _tokens(
                ctx,
                I,
                f"destinations[{slot.id}].resolve.path",
                slot.resolve.path,
                frozenset({"env", "team", "pipeline_id"}),
                path=True,
            )
    for env, entries in sp.destinations.destinations.items():
        for e in entries:
            pat = e.prefix_pattern if e.type == "s3" else e.path_pattern
            loc = f"destinations.{env}[{e.id}].{'prefix_pattern' if e.type == 's3' else 'path_pattern'}"
            for v in sorted(parse_path_pattern(pat, absolute=e.type != "s3").variables):
                if v not in ctx.variables:
                    yield issue("template_unknown_token", D, loc, f"'{{{v}}}' is not a declared variable")
                elif ctx.variable_resolvable(v) and not ctx.variable_constrained(v):
                    yield issue(
                        "template_token_unconstrained", D, loc, f"'{{{v}}}' has no pattern on its trigger field"
                    )


def _apis(ctx: Context) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if ctx.spec.integrations.portal:
        out.append(("portal", ctx.spec.integrations.portal))
    out += [(f"apis[{a.id}]", a) for a in ctx.spec.integrations.apis]
    return out


def check_cel(ctx: Context) -> Iterable[Issue]:
    for r in ctx.spec.rules.rules:
        exprs = [(f"rules[{r.rule_id}].preconditions[{i}]", e) for i, e in enumerate(r.preconditions)]
        if r.implementation.kind == "expression":
            exprs.append((f"rules[{r.rule_id}].implementation.cel", r.implementation.cel))
        for loc, expr in exprs:
            for path in sorted(cel.referenced_paths(expr)):
                root = path[0]
                name = path[1] if len(path) > 1 else ""
                if root == "vars" and name in ctx.variables:
                    continue
                if root == "artifacts" and name in r.inputs:
                    continue
                if root == "run" and name in RUN_FIELDS:
                    continue
                yield issue(
                    "cel_unknown_name",
                    R,
                    loc,
                    f"'{'.'.join(path)}' is not available (use artifacts.<input>, vars.<variable>, run.<field>)",
                )


def check_dataflow(ctx: Context) -> Iterable[Issue]:
    return list(ctx.graph.issues)


# ---------------------------------------------------------------- triggers and sources


def check_trigger_sources(ctx: Context) -> Iterable[Issue]:
    sp = ctx.spec
    trig = sp.pipeline.trigger
    envs = sp.environments.environments
    if trig.type in ("sqs", "s3_event"):
        for env in ctx.enabled_envs:
            if trig.queue not in envs[env].queues:
                yield issue(
                    "event_trigger_without_queue",
                    E,
                    f"environments.{env}.queues",
                    f"queue '{trig.queue}' is not bound in {env}",
                )
    sources = {s.id: s for s in sp.integrations.sources}
    for s in sp.integrations.sources:
        if s.type == "s3" and s.location.via == "trigger":
            for fld in (s.location.bucket_field, s.location.prefix_field):
                if trig.type == "sqs":
                    fs = trig.message.fields.get(fld)
                    if fs is None or fs.type != "string" or not fs.required:
                        yield issue(
                            "trigger_field_missing",
                            I,
                            f"sources[{s.id}].location",
                            f"'{fld}' must be a required string field of the trigger message",
                        )
                elif trig.type != "s3_event":
                    yield issue(
                        "trigger_field_missing",
                        I,
                        f"sources[{s.id}].location",
                        f"a {trig.type} trigger has no '{fld}' field",
                    )
        for env in ctx.enabled_envs:
            b = envs[env].sources.get(s.id)
            loc = f"environments.{env}.sources"
            if b is None:
                yield issue("binding_missing", E, loc, f"source '{s.id}' is not bound in {env}")
            elif s.type == "s3" and s.location.via == "trigger" and (not b.buckets or not b.prefix_roots):
                yield issue(
                    "source_unconstrained",
                    E,
                    f"{loc}.{s.id}",
                    f"list the allowed buckets and prefix_roots for '{s.id}' in {env}",
                )
            elif s.type == "s3" and s.location.via == "static" and b.bucket is None:
                yield issue("binding_missing", E, f"{loc}.{s.id}.bucket", f"source '{s.id}' needs a bucket in {env}")
            elif s.type == "database" and not b.fivetran_group:
                yield issue(
                    "binding_missing",
                    E,
                    f"{loc}.{s.id}.fivetran_group",
                    f"source '{s.id}' needs a Fivetran group in {env}",
                )
    for r in ctx.builtin_rules("s3.list"):
        p = r.implementation.params
        loc = f"rules[{r.rule_id}].implementation.params"
        if p.source not in sources:
            yield issue("unresolved_reference", R, f"{loc}.source", f"source '{p.source}' does not exist")
        if p.recursive and not p.include:
            yield issue(
                "recursive_without_include",
                R,
                f"{loc}.include",
                "a recursive listing needs at least one include pattern",
            )
        for field_name in ("include", "exclude"):
            for i, g in enumerate(getattr(p, field_name)):
                try:
                    check_glob(g)
                except GlobError as exc:
                    yield issue("glob_invalid", R, f"{loc}.{field_name}[{i}]", str(exc))


# ---------------------------------------------------------------- portal and destinations


def check_portal(ctx: Context) -> Iterable[Issue]:
    sp = ctx.spec
    portal = sp.integrations.portal
    needs = ctx.builtin_rules("portal.lookup") or any(
        isinstance(s.resolve, PortalResolve) for s in sp.integrations.destinations
    )
    if needs and portal is None:
        yield issue(
            "portal_required", I, "portal", "a rule or destination slot uses the portal, but no portal is defined"
        )
        return
    if portal is None:
        return
    endpoints = {e.id: e for e in portal.endpoints}
    for r in ctx.builtin_rules("portal.lookup"):
        ep = r.implementation.params.endpoint
        if ep not in endpoints:
            yield issue(
                "unresolved_reference",
                R,
                f"rules[{r.rule_id}].implementation.params.endpoint",
                f"portal endpoint '{ep}' does not exist",
            )
    for slot in sp.integrations.destinations:
        if isinstance(slot.resolve, PortalResolve):
            ep = endpoints.get(slot.resolve.endpoint)
            if ep is None:
                yield issue(
                    "unresolved_reference",
                    I,
                    f"destinations[{slot.id}].resolve.endpoint",
                    f"portal endpoint '{slot.resolve.endpoint}' does not exist",
                )
            elif slot.id not in ep.mapping.destinations:
                yield issue(
                    "destination_slot_unmapped",
                    I,
                    f"portal.endpoints[{ep.id}].mapping.destinations",
                    f"slot '{slot.id}' has no mapping",
                )
    for env in ctx.enabled_envs:
        if sp.environments.environments[env].portal is None:
            yield issue("binding_missing", E, f"environments.{env}.portal", f"the portal base_url is not set for {env}")


def check_destinations(ctx: Context) -> Iterable[Issue]:
    sp = ctx.spec
    slots = {s.id: s for s in sp.integrations.destinations}
    resolved: set[str] = set()
    for r in ctx.builtin_rules("destination.resolve"):
        for s in r.implementation.params.slots:
            if s not in slots:
                yield issue(
                    "unresolved_reference",
                    R,
                    f"rules[{r.rule_id}].implementation.params.slots",
                    f"destination slot '{s}' does not exist",
                )
            elif not isinstance(slots[s].resolve, PortalResolve):
                yield issue(
                    "unresolved_reference",
                    R,
                    f"rules[{r.rule_id}].implementation.params.slots",
                    f"slot '{s}' is static; only portal slots are resolved",
                )
            resolved.add(s)
    for op in ("destination.deliver", "s3.put", "s3.copy", "sftp.put", "https.put"):
        for r in ctx.builtin_rules(op):
            p = r.implementation.params
            loc = f"rules[{r.rule_id}].implementation.params"
            slot = slots.get(p.slot)
            if slot is None:
                yield issue("unresolved_reference", R, f"{loc}.slot", f"destination slot '{p.slot}' does not exist")
            elif op == "destination.deliver" and isinstance(slot.resolve, PortalResolve) and p.slot not in resolved:
                yield issue(
                    "unresolved_reference",
                    R,
                    f"{loc}.slot",
                    f"slot '{p.slot}' is never resolved by a destination.resolve rule",
                )
            elif op != "destination.deliver" and not isinstance(slot.resolve, StaticResolve):
                yield issue(
                    "unresolved_reference",
                    R,
                    f"{loc}.slot",
                    f"{op} needs a static slot; '{p.slot}' is resolved by the portal",
                )
            if p.object_name is not None and p.object_name not in r.inputs:
                yield issue(
                    "unresolved_reference",
                    R,
                    f"{loc}.object_name",
                    f"'{p.object_name}' must also be listed in the rule's inputs",
                )
    level = CLASSIFICATION_ORDER[sp.security.security_and_access.data_classification]
    for env, entries in sp.destinations.destinations.items():
        for e in entries:
            loc = f"destinations.{env}[{e.id}]"
            for s in e.slots:
                if s not in slots:
                    yield issue(
                        "unresolved_reference", D, f"{loc}.slots", f"slot '{s}' does not exist in integrations.yaml"
                    )
                elif e.type not in slots[s].allowed_types:
                    yield issue(
                        "destination_type_not_allowed",
                        D,
                        f"{loc}.type",
                        f"slot '{s}' does not allow {e.type} destinations",
                    )
            if CLASSIFICATION_ORDER[e.max_classification] < level:
                yield issue(
                    "classification_target_mismatch",
                    D,
                    f"{loc}.max_classification",
                    f"{e.id} is approved up to {e.max_classification}; "
                    f"the data is {sp.security.security_and_access.data_classification}",
                )
        for a_i, a in enumerate(entries):
            for b in entries[a_i + 1 :]:
                if a.type != b.type or not set(a.slots) & set(b.slots):
                    continue
                ka = (a.bucket,) if a.type == "s3" else (a.host, a.port)
                kb = (b.bucket,) if b.type == "s3" else (b.host, b.port)
                if ka != kb:
                    continue
                pa = parse_path_pattern(a.prefix_pattern if a.type == "s3" else a.path_pattern, absolute=a.type != "s3")
                pb = parse_path_pattern(b.prefix_pattern if b.type == "s3" else b.path_pattern, absolute=b.type != "s3")
                if may_overlap(pa, pb):
                    yield issue(
                        "destination_ambiguous",
                        D,
                        f"destinations.{env}[{b.id}]",
                        f"'{a.id}' and '{b.id}' can match the same destination",
                    )
    if slots:
        for env in ctx.enabled_envs:
            entries = sp.destinations.destinations.get(env, [])
            ids = {e.id for e in entries}
            for s in slots.values():
                if isinstance(s.resolve, StaticResolve):
                    if s.resolve.entry not in ids:
                        yield issue(
                            "destination_slot_unbound",
                            D,
                            f"destinations.{env}",
                            f"static slot '{s.id}' needs entry '{s.resolve.entry}' in {env}",
                        )
                elif not any(s.id in e.slots for e in entries):
                    yield issue(
                        "destination_slot_unbound",
                        D,
                        f"destinations.{env}",
                        f"no approved destination for slot '{s.id}' in {env}",
                    )


# ---------------------------------------------------------------- secrets

_SECRET_TEXT = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
]
# `secret: vendor_api_key` names a declared secret (a reference); any other value after such a key is flagged
_SECRET_KEY_VALUE = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"]?([^\s'\",{}\[\]]{6,})"
)


def _secret_uses(ctx: Context) -> list[tuple[str, str, str, frozenset[str]]]:
    """(secret name, file, location, accepted kinds)"""
    sp = ctx.spec
    uses: list[tuple[str, str, str, frozenset[str]]] = []
    for loc, api in _apis(ctx):
        a = api.auth
        if a.type == "oauth2_client_credentials":
            uses.append((a.client, I, f"{loc}.auth.client", frozenset({"oauth_client"})))
        elif a.type == "api_key":
            uses.append((a.secret, I, f"{loc}.auth.secret", frozenset({"api_key", "generic"})))
        else:
            uses.append((a.cert, I, f"{loc}.auth.cert", frozenset({"tls_client_cert"})))
            uses.append((a.key, I, f"{loc}.auth.key", frozenset({"tls_client_key"})))
            if a.ca:
                uses.append((a.ca, I, f"{loc}.auth.ca", frozenset({"ca_bundle"})))
    uses.extend(
        (s.credentials, I, f"sources[{s.id}].credentials", frozenset({"password", "generic"}))
        for s in sp.integrations.sources
        if s.type == "database"
    )
    for env, entries in sp.destinations.destinations.items():
        for e in entries:
            loc = f"destinations.{env}[{e.id}]"
            if e.type == "sftp":
                uses.append((e.private_key_secret, D, f"{loc}.private_key_secret", frozenset({"ssh_private_key"})))
            elif e.type == "https":
                if e.auth.type == "bearer":
                    uses.append((e.auth.token_secret, D, f"{loc}.auth.token_secret", frozenset({"api_key", "generic"})))
                else:
                    uses.append((e.auth.cert, D, f"{loc}.auth.cert", frozenset({"tls_client_cert"})))
                    uses.append((e.auth.key, D, f"{loc}.auth.key", frozenset({"tls_client_key"})))
    return uses


def check_secrets(ctx: Context) -> Iterable[Issue]:
    secrets = ctx.spec.integrations.secrets
    used: set[str] = set()
    for name, f, loc, kinds in _secret_uses(ctx):
        used.add(name)
        ref = secrets.get(name)
        if ref is None:
            yield issue(
                "unresolved_reference", f, loc, f"secret '{name}' is not declared in integrations.yaml → secrets"
            )
        elif ref.kind not in kinds:
            yield issue(
                "secret_kind_mismatch",
                I,
                f"secrets.{name}.kind",
                f"'{name}' is used as {'/'.join(sorted(kinds))} but its kind is {ref.kind}",
            )
    for name in sorted(set(secrets) - used):
        yield issue("secret_unused", I, f"secrets.{name}", f"secret '{name}' is not used")
    providers = {r.provider for r in secrets.values()}
    for env in ctx.enabled_envs:
        b = ctx.spec.environments.environments[env].secrets
        if "vault" in providers and b.vault is None:
            yield issue(
                "binding_missing",
                E,
                f"environments.{env}.secrets.vault",
                f"Vault secrets are used but {env} has no Vault binding",
            )
        if "aws_secrets_manager" in providers and b.aws_secrets_manager is None:
            yield issue(
                "binding_missing",
                E,
                f"environments.{env}.secrets.aws_secrets_manager",
                f"Secrets Manager secrets are used but {env} has no binding",
            )
    spec_dir = ctx.spec.path / SPEC_DIR
    for fname in SPEC_FILES:
        spec_file = spec_dir / fname
        if not spec_file.is_file():
            continue
        for n, line in enumerate(spec_file.read_text(encoding="utf-8").splitlines(), 1):
            values = [m.group(1) for m in _SECRET_KEY_VALUE.finditer(line)]
            if any(p.search(line) for p in _SECRET_TEXT) or any(v not in secrets for v in values):
                yield issue("secret_in_spec", fname, f"line {n}", "this line looks like a secret value")


# ---------------------------------------------------------------- environments and platform


def check_environments(ctx: Context) -> Iterable[Issue]:
    sp = ctx.spec
    envs = sp.environments.environments
    for env in ctx.envs:
        if env not in envs:
            yield issue("env_missing", E, "environments", f"environment '{env}' (factory.toml) has no bindings")
        if sp.integrations.destinations and env not in sp.destinations.destinations:
            yield issue("env_missing", D, "destinations", f"environment '{env}' has no destination allow-list")
    for env in sorted(set(envs) - set(ctx.envs)):
        yield issue("env_unknown", E, f"environments.{env}", f"'{env}' is not an environment of this repository")
    for env in sorted(set(sp.destinations.destinations) - set(ctx.envs)):
        yield issue("env_unknown", D, f"destinations.{env}", f"'{env}' is not an environment of this repository")
    owners: dict[tuple[str, str], str] = {}
    for env in sorted(envs):
        b = envs[env]
        res = [("queue", q.name) for q in b.queues.values()] + [
            ("bucket", x) for s in b.sources.values() for x in s.buckets
        ]
        for kind, name in res:
            other = owners.setdefault((kind, name), env)
            if other != env:
                yield issue("env_resource_shared", E, f"environments.{env}", f"{kind} '{name}' is also used by {other}")
    channels_needed = {c for c in sp.pipeline.observability.alerts.model_dump().values() if c != "none"}
    for env in ctx.enabled_envs:
        for c in sorted(channels_needed - set(envs[env].alert_channels)):
            yield issue(
                "binding_missing",
                E,
                f"environments.{env}.alert_channels",
                f"alert channel '{c}' is not defined in {env}",
            )
        for api in sp.integrations.apis:
            if api.id not in envs[env].apis:
                yield issue(
                    "binding_missing", E, f"environments.{env}.apis", f"API '{api.id}' has no base_url in {env}"
                )
    # platform settings
    if not ctx.repo.platform:
        yield issue(
            "platform_settings_missing",
            E,
            "",
            "platform settings were not found; placement and team checks were skipped",
        )
        return
    for msg in ctx.repo.platform_issues:
        yield issue("platform_settings_missing", E, "", msg)
    team = sp.pipeline.metadata.team
    missing_team = [e for e in ctx.enabled_envs if e in ctx.repo.platform and team not in ctx.repo.platform[e].teams]
    if missing_team:
        yield issue(
            "unknown_team",
            P,
            "metadata.team",
            f"team '{team}' is not defined in the platform settings of {', '.join(missing_team)}",
        )
    for env in ctx.enabled_envs:
        ps = ctx.repo.platform.get(env)
        if ps is None:
            continue
        b = envs[env]
        aliases = [
            (f"environments.{env}.placement.orchestration", b.placement.orchestration),
            (f"environments.{env}.placement.workload", b.placement.workload),
        ]
        aliases += [(f"environments.{env}.sources.{sid}.placement", s.placement) for sid, s in b.sources.items()]
        for loc, a in aliases:
            if a not in ps.placements:
                yield issue(
                    "unknown_placement_alias", E, loc, f"placement '{a}' is not defined in the {env} platform settings"
                )
        for e in sp.destinations.destinations.get(env, []):
            if e.type == "s3" and e.placement not in ps.placements:
                yield issue(
                    "unknown_placement_alias",
                    D,
                    f"destinations.{env}[{e.id}].placement",
                    f"placement '{e.placement}' is not defined in the {env} platform settings",
                )


# ---------------------------------------------------------------- engines and workload


def check_engines(ctx: Context) -> Iterable[Issue]:
    sp = ctx.spec
    table = engine_table()
    wl = sp.pipeline.workload
    if wl.bytes_per_run.max > table.max_package_bytes:
        yield issue(
            "workload_exceeds_limit",
            P,
            "workload.bytes_per_run.max",
            f"{units.format_bytes(wl.bytes_per_run.max)} is above the platform limit of "
            f"{units.format_bytes(table.max_package_bytes)}",
        )
    for t in sp.pipeline.orchestration.tasks:
        if t.engine == "auto":
            continue
        loc = f"orchestration.tasks[{t.task_id}].engine"
        rules = [r for r in (sp.rule(x) for x in t.rules) if r is not None]
        allowed = allowed_engines(rules)
        if t.engine not in allowed:
            yield issue(
                "engine_not_allowed",
                P,
                loc,
                f"'{t.engine}' cannot run this task; allowed: {sorted(allowed) or ['none']}",
            )
            continue
        if t.engine == "lambda":
            lim = table.lambda_.timeout_s
            if t.timeout > lim:
                yield issue(
                    "task_timeout_exceeds_engine",
                    P,
                    f"orchestration.tasks[{t.task_id}].timeout",
                    f"Lambda stops after {lim}s; timeout is {t.timeout}s",
                )
            est = estimate_lambda_seconds(ctx.spec, rules)
            safety = table.lambda_.safety_factor
            if est > lim:
                yield issue(
                    "engine_infeasible",
                    P,
                    loc,
                    f"estimated {est}s at {units.format_bytes(wl.bytes_per_run.max)}; Lambda allows {lim}s",
                )
            elif est * safety > lim:
                yield issue(
                    "engine_marginal", P, loc, f"estimated {est}s; with the {safety}x safety margin that exceeds {lim}s"
                )


# ---------------------------------------------------------------- GxP, retention, checksums


def check_gxp(ctx: Context) -> Iterable[Issue]:
    sp = ctx.spec
    sec = sp.security.security_and_access
    replay = sp.pipeline.reliability.replay_window
    if units.parse_retention_days(sec.retention.staging_after_failure) * 86400 < replay:
        yield issue(
            "retention_shorter_than_replay",
            S,
            "security_and_access.retention.staging_after_failure",
            "staging after failure is kept shorter than the replay window",
        )
    required = set(sec.checksums.delivery_sidecars)
    for r in ctx.builtin_rules("destination.deliver"):
        missing = required - set(r.implementation.params.sidecars)
        if missing:
            yield issue(
                "checksum_policy_mismatch",
                R,
                f"rules[{r.rule_id}].implementation.params.sidecars",
                f"security.yaml requires {sorted(missing)} checksum files",
            )
    if sec.gxp_tier == "non_gxp":
        return
    for t in sp.pipeline.orchestration.tasks:
        if t.engine == "eks_pod_spot":
            yield issue(
                "gxp_spot_forbidden",
                P,
                f"orchestration.tasks[{t.task_id}].engine",
                "GxP pipelines cannot run on spot capacity",
            )
    if not sec.audit.enabled or not {"delivery", "destination_resolution"} <= set(sec.audit.events):
        yield issue(
            "gxp_audit_missing",
            S,
            "security_and_access.audit",
            "GxP needs the audit trail with delivery and destination_resolution events",
        )
    if "sha256" not in sec.checksums.manifest or (
        ctx.builtin_rules("package.zip") and not ctx.builtin_rules("manifest.build")
    ):
        yield issue(
            "gxp_sha256_missing",
            S,
            "security_and_access.checksums.manifest",
            "GxP needs sha256 in a manifest of every package",
        )
    if sec.encryption_at_rest.s3_destinations != "sse_kms":
        yield issue(
            "gxp_encryption_at_rest_missing",
            S,
            "security_and_access.encryption_at_rest.s3_destinations",
            "GxP needs SSE-KMS on S3 destinations",
        )
    for env, entries in sp.destinations.destinations.items():
        for e in entries:
            if e.type in ("sftp", "https") and not e.at_rest_attestation:
                yield issue(
                    "gxp_external_destination_unattested",
                    D,
                    f"destinations.{env}[{e.id}].at_rest_attestation",
                    f"{e.id} has no at-rest attestation",
                )


# ---------------------------------------------------------------- custom rules


def check_custom(ctx: Context) -> Iterable[Issue]:
    import hashlib

    for r in ctx.spec.rules.rules:
        if r.implementation.kind != "custom":
            continue
        d = ctx.spec.path / "rules" / r.rule_id
        loc = f"rules[{r.rule_id}].implementation"
        impl = d / "impl.py"
        if not impl.is_file():
            yield issue("custom_rule_code_missing", R, loc, f"rules/{r.rule_id}/impl.py does not exist")
            continue
        if hashlib.sha256(impl.read_bytes()).hexdigest() != r.implementation.code_sha256:
            yield issue("custom_rule_code_mismatch", R, f"{loc}.code_sha256", "the code changed since it was reviewed")
        for finding in check_plugin_source(impl.read_text(encoding="utf-8", errors="replace")):
            yield issue(
                "custom_rule_import_forbidden",
                R,
                loc,
                f"rules/{r.rule_id}/impl.py line {finding.line}: {finding.msg}",
            )
        if not (d / "test_impl.py").is_file() or not r.test_cases:
            yield issue("custom_rule_untested", R, loc, "custom rules need test_impl.py and owner test_cases")
            continue
        problem = acceptance_problem(r, d)
        if problem:
            yield issue("custom_rule_unaccepted", R, loc, f"rules/{r.rule_id}: {problem}")


CHECKS: tuple[Check, ...] = (
    check_meta,
    check_ids,
    check_ops,
    check_tasks,
    check_variables,
    check_templates,
    check_cel,
    check_dataflow,
    check_trigger_sources,
    check_portal,
    check_destinations,
    check_secrets,
    check_environments,
    check_engines,
    check_gxp,
    check_custom,
)
