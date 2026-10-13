"""Run one orchestration task: its rules, in order, in this process (execution-model §1).

Error precedence (execution-model §6):
- security errors are never downgraded and transient errors are never reclassified;
- `on_failure.error_class` applies only to *check* failures (CheckFailed: validate ops, expressions, preconditions);
- `on_failure.action: warn` is honoured only for rules without outputs; `quarantine` records the rule in run-state
  and fails the run as data;
- anything unexpected is classified (factory_core.retry.classify) and reported with the exception type only, never
  its message (it may contain data values or secrets).

Checkpoints: a finished task is a no-op; within a task, a rule whose outputs are all staged or final is skipped on a
retry and its outputs are read back from staging. Streams are lazy, so skipping their consumer skips the work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from factory_core.errors import (
    ConfigError,
    DataError,
    ErrorClass,
    FactoryError,
    SecurityError,
    TransientError,
)
from factory_core.jsonlog import bind
from factory_core.retry import classify
from factory_core.runkey import run_dir, run_key_hash
from factory_spec.artifact_models import Doc, PortalSnapshot, TriggerSnapshot
from factory_spec.derive import derive_artifacts

from factory_core import cel

from .artifacts import ADOPT_KINDS, TRIGGER_SNAPSHOT, ArtifactStore, RunInfo, Vars, doc_name, run_date_of
from .audit import AuditLog
from .bundles import LoadedBundle, bundle_uri, load_bundle
from .context import RuleContext
from .ops import CheckFailed, lookup
from .pin import Pin, check_image, establish_pin
from .services import Services
from .staging import Staging

_CLASS_TO_ERROR: dict[ErrorClass, type[FactoryError]] = {
    ErrorClass.TRANSIENT: TransientError,
    ErrorClass.DATA: DataError,
    ErrorClass.CONFIG: ConfigError,
    ErrorClass.SECURITY: SecurityError,
}


@dataclass(frozen=True)
class TaskRequest:
    bundle: str
    sha256: str
    task_id: str
    run_key: str
    attempt: int = 1
    run_id: str = ""
    conf: dict[str, Any] | None = None


@dataclass
class TaskOutcome:
    task_id: str
    status: str  # "succeeded" | "already_done"
    rules: list[str] = field(default_factory=list)
    audit_key: str | None = None


def classified(exc: BaseException, where: str) -> FactoryError:
    if isinstance(exc, FactoryError):
        return exc
    cls = classify(exc)
    err_type = _CLASS_TO_ERROR.get(cls, FactoryError)
    code = "unexpected_error" if cls is ErrorClass.INTERNAL else "dependency_failed"
    err = err_type(code, f"{type(exc).__name__} in {where}")
    err.__cause__ = exc
    return err


def _check_environment(bundle: LoadedBundle, services: Services, task_id: str) -> None:
    spec, cfg = bundle.spec, services.config
    binding = spec.environments.environments.get(cfg.env)
    if binding is None or not binding.enabled:
        raise ConfigError("environment_not_bound", f"the pipeline has no enabled binding for {cfg.env}")
    regions = spec.security.security_and_access.residency.allowed_regions
    if regions and cfg.region not in regions:
        raise SecurityError("residency_region_not_allowed", f"region {cfg.region} is not allowed for this pipeline")
    if task_id not in {t.task_id for t in spec.pipeline.orchestration.tasks}:
        raise ConfigError("task_unknown", f"task '{task_id}' is not part of the pipeline")
    if spec.pipeline.metadata.team != cfg.team:
        raise ConfigError("team_mismatch", "the pipeline belongs to another team")


def _trigger_snapshot(bundle: LoadedBundle, staging: Staging, conf: dict[str, Any] | None) -> TriggerSnapshot | None:
    trig = bundle.spec.pipeline.trigger
    if staging.exists(TRIGGER_SNAPSHOT):
        return staging.get_doc(TRIGGER_SNAPSHOT, TriggerSnapshot)
    if conf is None or trig.type not in ("schedule", "manual"):
        return None
    if trig.type == "manual":
        from factory_spec.dynamic import record_model

        model = record_model("manual_conf", trig.conf)
        try:
            body = model.model_validate(conf).model_dump(mode="json", by_alias=True, exclude_none=True)
        except Exception:
            raise ConfigError("trigger_conf_invalid", "the run conf does not match the declared fields") from None
    else:
        start = conf.get("data_interval_start")
        if not isinstance(start, str):
            raise ConfigError("trigger_conf_invalid", "a scheduled run needs data_interval_start")
        body = {"data_interval_start": start, "run_date": start[:10]}
    from factory_core.canonical import canonical_bytes, sha256_hex

    snap = TriggerSnapshot(source=trig.type, body=body, body_sha256=sha256_hex(canonical_bytes(body)))
    return staging.put_doc(TRIGGER_SNAPSHOT, snap, adopt=True)


def _portal_lookup(bundle: LoadedBundle, store: ArtifactStore) -> Any:
    def find(endpoint: str) -> PortalSnapshot | None:
        for r in bundle.spec.rules.rules:
            impl = r.implementation
            if impl.kind == "builtin" and impl.op == "portal.lookup" and impl.params.endpoint == endpoint and r.outputs:
                try:
                    value = store.get(r.outputs[0]).value
                except ConfigError:
                    return None
                return value if isinstance(value, PortalSnapshot) else None
        return None

    return find


def _checkpointable(rule: Any, store: ArtifactStore) -> bool:
    return bool(rule.outputs) and all(store.graph.artifacts[o].durability in ("staged", "final") for o in rule.outputs)


def _run_rule(ctx: RuleContext) -> list[Any]:
    rule = ctx.rule
    for i, expr in enumerate(rule.preconditions):
        passed, _ = cel.evaluate(expr, ctx.cel_activation())
        if not passed:
            raise CheckFailed("precondition_failed", f"precondition {i} of {rule.rule_id} did not hold")
    impl = rule.implementation
    if impl.kind == "builtin":
        fn = lookup(impl.op)
    elif impl.kind == "expression":
        fn = lookup("expression")
    else:
        from .plugins import run_custom_rule

        fn = run_custom_rule
    outs = fn(ctx)
    if len(outs) != len(rule.outputs):
        raise FactoryError(
            "op_output_arity", f"{rule.rule_id} produced {len(outs)} outputs, expected {len(rule.outputs)}"
        )
    return outs


def _store_outputs(ctx: RuleContext, outs: list[Any]) -> None:
    for name, value in zip(ctx.rule.outputs, outs, strict=True):
        node = ctx.artifacts.graph.artifacts[name]
        if node.durability in ("staged", "final") and name not in ctx.staged and isinstance(value, Doc):
            value = ctx.staging.put_doc(doc_name(name), value, adopt=node.kind in ADOPT_KINDS)
        ctx.artifacts.put(name, value)


def _apply_on_failure(ctx: RuleContext, failure: CheckFailed) -> FactoryError | None:
    """None to continue (warn); otherwise the error to raise."""
    of = ctx.rule.on_failure
    if of.action == "warn" and not ctx.rule.outputs:
        ctx.log("rule_warning", level="WARNING", code=failure.code)
        return None
    if of.action == "quarantine":
        ctx.services.state.put(
            ctx.run.run_dir,
            f"QUAR#{ctx.task_id}#{ctx.rule.rule_id}",
            {"rule_id": ctx.rule.rule_id, "code": failure.code, "at": int(ctx.services.clock())},
        )
        return DataError("quarantined", f"rule {ctx.rule.rule_id} quarantined the run ({failure.code})")
    err = _CLASS_TO_ERROR.get(ErrorClass(of.error_class), DataError)(failure.code, failure.message)
    err.__cause__ = failure
    return err


def execute(services: Services, req: TaskRequest) -> TaskOutcome:
    cfg = services.config
    rdir = run_dir(req.run_key)
    # logs carry the run key as a hash only (run ids embed a readable run key prefix, so they are not logged either)
    with bind(
        pipeline_id=cfg.pipeline_id,
        team=cfg.team,
        env=cfg.env,
        task_id=req.task_id,
        attempt=req.attempt,
        run_key=run_key_hash(req.run_key),
    ):
        bundle = load_bundle(services.aws, cfg, req.bundle, req.sha256)
        _check_environment(bundle, services, req.task_id)
        candidate = Pin(
            bundle_sha=bundle.sha256,
            image_digest=cfg.image_digest,
            logic_sha=bundle.hashes["logic_sha"],
            binding_sha=bundle.hashes["binding_sha"].get(cfg.env, ""),
            pinned_at=int(services.clock()),
        )
        _, pin = establish_pin(services.state, rdir, candidate)
        check_image(pin, cfg.image_digest)
        if pin.bundle_sha != bundle.sha256:
            services.log.event("run_pinned_bundle", pinned=pin.bundle_sha[:12], requested=bundle.sha256[:12])
            bundle = load_bundle(services.aws, cfg, bundle_uri(cfg, pin.bundle_sha), pin.bundle_sha)
        with bind(bundle_sha=bundle.sha256[:12], image_digest=cfg.image_digest):
            return _execute_task(services, req, bundle, pin, rdir)


def _execute_task(services: Services, req: TaskRequest, bundle: LoadedBundle, pin: Pin, rdir: str) -> TaskOutcome:
    cfg, spec = services.config, bundle.spec
    log = services.log
    sec = spec.security.security_and_access
    staging = Staging(services.aws, cfg.staging_bucket, cfg.staging_kms_key, cfg.pipeline_id, rdir)
    audit = AuditLog(
        services.aws,
        cfg.audit_store,
        cfg.audit_kms_key,
        f"{cfg.pipeline_id}/{rdir}/",
        frozenset(sec.audit.events) if sec.audit.enabled else frozenset(),
        base={"pipeline_id": cfg.pipeline_id, "env": cfg.env, "run": run_key_hash(req.run_key), "task_id": req.task_id},
        clock=services.clock,
    )
    done = services.state.get(rdir, f"TASK#{req.task_id}")
    if done is not None:
        log.event("task_already_done", attempt_done=done.doc.get("attempt"))
        return TaskOutcome(req.task_id, "already_done")
    trigger = _trigger_snapshot(bundle, staging, req.conf)
    graph = derive_artifacts(spec)
    store = ArtifactStore(graph, staging)
    run = RunInfo(
        run_key=req.run_key,
        run_dir=rdir,
        run_key_hash=run_key_hash(req.run_key),
        env=cfg.env,
        pipeline_id=cfg.pipeline_id,
        team=cfg.team,
        attempt=req.attempt,
        run_id=req.run_id or rdir,
        pinned_at=pin.pinned_at,
        run_date=run_date_of(pin.pinned_at, trigger),
    )
    vars_ = Vars(spec, trigger, _portal_lookup(bundle, store))
    workdir = cfg.workdir / "runs" / rdir / req.task_id
    workdir.mkdir(parents=True, exist_ok=True)
    rules = [r for t, r in spec.ordered_rules() if t.task_id == req.task_id]
    log.event("task_start", rules=len(rules))
    t0 = time.monotonic()
    ran: list[str] = []
    try:
        for rule in rules:
            ctx = RuleContext(rule, req.task_id, spec, services, run, vars_, store, staging, audit, workdir)
            with bind(rule_id=rule.rule_id):
                ran.append(_one_rule(ctx, services, rdir, req.task_id))
        services.state.put(rdir, f"TASK#{req.task_id}", {"attempt": req.attempt, "rules": ran})
        log.event("task_end", duration_ms=int((time.monotonic() - t0) * 1000))
        return TaskOutcome(req.task_id, "succeeded", ran, audit.flush(f"{req.task_id}/attempt-{req.attempt}"))
    except BaseException as exc:
        err = classified(exc, req.task_id)
        log.event("task_failed", level="ERROR", error_class=str(err.error_class), code=err.code)
        try:
            audit.flush(f"{req.task_id}/attempt-{req.attempt}")
        except Exception:  # the original failure matters more than the audit flush
            log.event("audit_flush_failed", level="ERROR")
        if err is exc:
            raise
        raise err from exc


def _one_rule(ctx: RuleContext, services: Services, rdir: str, task_id: str) -> str:
    rule = ctx.rule
    ck = f"RULE#{task_id}#{rule.rule_id}"
    if _checkpointable(rule, ctx.artifacts) and services.state.get(rdir, ck) is not None:
        services.log.event("rule_skipped_checkpoint")
        return str(rule.rule_id)
    t0 = time.monotonic()
    try:
        outs = _run_rule(ctx)
    except CheckFailed as failure:
        err = _apply_on_failure(ctx, failure)
        ctx.audit.record(
            "rule_result", rule_id=rule.rule_id, outcome="warned" if err is None else "failed", code=failure.code
        )
        if err is None:
            return str(rule.rule_id)
        raise err from failure
    except BaseException as exc:
        err = classified(exc, rule.rule_id)
        ctx.audit.record(
            "rule_result", rule_id=rule.rule_id, outcome="failed", error_class=str(err.error_class), code=err.code
        )
        if err is exc:
            raise
        raise err from exc
    _store_outputs(ctx, outs)
    if _checkpointable(rule, ctx.artifacts):
        services.state.put(rdir, ck, {"outputs": list(rule.outputs)})
    ms = int((time.monotonic() - t0) * 1000)
    ctx.audit.record("rule_result", rule_id=rule.rule_id, outcome="ok", duration_ms=ms)
    services.log.event("rule_end", duration_ms=ms)
    return str(rule.rule_id)
