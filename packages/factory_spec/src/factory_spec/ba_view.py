"""A plain-English rendering of a spec for business analysts (`factory show <p> --view ba`)."""

from __future__ import annotations

from factory_core import units

from .spec import PipelineSpec


def render_ba(spec: PipelineSpec) -> str:
    p = spec.pipeline
    md = p.metadata
    sec = spec.security.security_and_access
    out: list[str] = [f"# {md.name} ({md.pipeline_id}, version {md.spec_version})", "", md.description, ""]
    out += ["## 1. Who owns it", ""]
    out += [f"- {role.replace('_', ' ').capitalize()}: {h}" for role, h in md.owners.model_dump().items()]
    trig = p.trigger
    out += ["", "## 2. What starts a run", ""]
    if trig.type == "sqs":
        out.append(f"- A message on queue `{trig.queue}` with the fields: {', '.join(sorted(trig.message.fields))}.")
    elif trig.type == "s3_event":
        out.append(f"- A new object in the source bucket (queue `{trig.queue}`).")
    elif trig.type == "schedule":
        out.append(f"- The schedule `{trig.cron}` (UTC).")
    else:
        out.append("- A person starts it manually.")
    out.append(f"- Each run is identified by `{p.reliability.run_key}`; the same request never runs twice.")
    out += ["", "## 3. Steps", ""]
    task_of = spec.task_of_rule()
    for t in p.orchestration.tasks:
        out.append(f"### {t.label or t.task_id} (`{t.task_id}`, engine: {t.engine})")
        out.append("")
        for i, rid in enumerate(t.rules, 1):
            r = spec.rule(rid)
            if r is None:
                continue
            how = r.implementation.op if r.implementation.kind == "builtin" else r.implementation.kind
            out.append(f"{i}. {r.plain_english.strip()}  _(rule `{rid}`, {r.category}, {how})_")
        out.append("")
    unassigned = [r.rule_id for r in spec.rules.rules if r.rule_id not in task_of]
    if unassigned:
        out += [f"Rules not yet in a task: {', '.join(unassigned)}", ""]
    out += ["## 4. Where data goes", ""]
    for s in spec.integrations.destinations:
        how = "chosen by the self-service portal" if s.resolve.via == "portal" else "fixed"
        out.append(
            f"- `{s.id}`: {s.label or 'destination ' + s.id}; {how}; allowed types {', '.join(s.allowed_types)}."
        )
    allow = spec.destinations.destinations
    if allow:
        out += ["", "Approved destinations (the portal can only choose among these):", ""]
        # environments in the order environments.yaml lists them, then any others
        envs = [e for e in spec.environments.environments if e in allow] + sorted(
            set(allow) - set(spec.environments.environments)
        )
        for env in envs:
            names = ", ".join(f"{e.id} ({e.type}, for {'/'.join(e.slots)})" for e in allow[env]) or "none"
            out.append(f"- {env}: {names}")
    out += ["", "## 5. Protection and evidence", ""]
    out.append(f"- Data classification: {sec.data_classification}; GxP tier: {sec.gxp_tier}.")
    out.append(
        f"- Encryption at rest: staging {sec.encryption_at_rest.staging}, "
        f"S3 destinations {sec.encryption_at_rest.s3_destinations}; "
        f"package encryption: {sec.package_encryption} (done outside the pipeline for now)."
    )
    out.append(
        f"- Checksums: manifest {', '.join(sec.checksums.manifest)}; "
        f"delivered checksum files {', '.join(sec.checksums.delivery_sidecars) or 'none'}."
    )
    out.append(f"- Audit trail: {'on' if sec.audit.enabled else 'off'}; evidence kept {sec.audit.evidence_retention}.")
    wl = p.workload
    out += ["", "## 6. Size and cost", ""]
    out.append(
        f"- Typical run {units.format_bytes(wl.bytes_per_run.p50)}, "
        f"largest {units.format_bytes(wl.bytes_per_run.max)}, about {wl.runs_per_day} runs per day."
    )
    out.append(f"- Budget: ${p.cost.budget_per_run_usd} per run, ${p.cost.budget_per_month_usd} per month.")
    if p.open_questions:
        out += ["", "## Open questions", ""] + [f"- {q}" for q in p.open_questions]
    return "\n".join(out) + "\n"
