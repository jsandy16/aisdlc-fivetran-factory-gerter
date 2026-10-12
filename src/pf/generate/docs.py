"""Human- and machine-readable descriptions: ODCS v3.2.0 data contract, catalog README with lineage and STTM,
and the Fivetran destination/connection settings per environment (no secrets)."""
from __future__ import annotations

import json

from ruamel.yaml import YAML

from ..bundle import Bundle

LOGICAL = {"STRING": "string", "INT": "integer", "LONG": "integer", "DOUBLE": "number", "DECIMAL": "number", "BOOLEAN": "boolean",
           "NAIVE_DATE": "date", "NAIVE_DATETIME": "timestamp", "UTC_DATETIME": "timestamp", "JSON": "object"}


def contract(b: Bundle, lock: dict) -> str:
    assert b.pipeline and b.sttm and b.policy and b.quality
    p, pol = b.pipeline, b.policy
    schema = []
    for e in b.sttm.entities:
        props = []
        for a in e.attributes:
            prop = {"name": a.name, "logicalType": LOGICAL[a.type],
                    "physicalType": f"DECIMAL({a.precision},{a.scale})" if a.type == "DECIMAL" else a.type,
                    "required": not a.nullable, "primaryKey": a.name in e.primary_key,
                    "classification": pol.classification.get(f"{e.name}.{a.name}", pol.default_classification),
                    "description": a.description or a.business_rule or a.name}
            if a.name in e.primary_key:
                prop["primaryKeyPosition"] = e.primary_key.index(a.name) + 1
            props.append(prop)
        quality = [{"rule": c.kind, "description": c.description, "severity": "error" if c.severity == "blocker" else "warning"}
                   for c in b.quality.checks if c.entity == e.name]
        obj = {"name": e.name, "physicalName": e.name, "logicalType": "object", "physicalType": "table",
               "description": e.description or e.name, "properties": props}
        if quality:
            obj["quality"] = quality
        schema.append(obj)
    doc = {
        "apiVersion": "v3.2.0", "kind": "DataContract", "id": f"{p.name}", "name": p.title, "version": p.version, "status": "active",
        "description": {"purpose": p.requirement.purpose, "usage": "Consumers read certified tables only (pf_certified = true)."},
        "team": [{"username": p.owners.product_owner, "role": "Owner"}, {"username": p.owners.data_engineer, "role": "Data engineer"},
                 {"username": p.owners.governance, "role": "Governance"}, {"username": p.owners.quality, "role": "Data quality"}],
        "schema": schema,
        "slaProperties": [{"property": "latency", "value": p.requirement.freshness_sla_hours, "unit": "h"},
                          {"property": "retention", "value": pol.retention.days, "unit": "d"}],
    }
    y = YAML()
    y.default_flow_style = False
    import io

    buf = io.StringIO()
    y.dump(doc, buf)
    return buf.getvalue()


def readme(b: Bundle, lock: dict) -> str:
    assert b.pipeline and b.sttm and b.source and b.policy and b.quality
    p, s = b.pipeline, b.source.source
    lines = [f"# {p.title} (`{p.name}` v{p.version})", "",
             f"{p.requirement.purpose}", "",
             f"- Owners: product {p.owners.product_owner} · engineering {p.owners.data_engineer} · governance {p.owners.governance} · quality {p.owners.quality}",
             f"- GxP tier: {p.gxp_tier} · freshness SLA: {p.requirement.freshness_sla_hours} h · consumers: {', '.join(p.requirement.consumers)}",
             f"- Source: {s.type} → Fivetran Connector SDK (`{p.name}`) → S3 lake (Iceberg), schema `{p.name}`",
             f"- Spec sha256: `{lock['spec_sha256']}`", "", "## Lineage", "", "```mermaid", "flowchart LR"]
    src = f"{s.type}:{getattr(s, 'host', '')}"
    lines.append(f'  S["{src}"] --> C["Fivetran connector {p.name}"]')
    if lock["archive_originals"]:
        lines.append('  C --> O[("originals bucket (Object Lock)")]')
    for e in b.sttm.entities:
        lines.append(f'  C --> T_{e.name}["{p.name}.{e.name}"]')
        lines.append(f'  T_{e.name} --> Q["post-load checks"]')
    lines.append('  C --> E["pf_* evidence tables"]')
    lines.append('  Q --> R["certified tables (Lake Formation)"]')
    for i, c in enumerate(p.requirement.consumers):
        lines.append(f'  R --> K{i}["{c}"]')
    lines += ["```", "", "## Source-to-target mapping", ""]
    for e in b.sttm.entities:
        binding = e.file.member if e.file else e.table.table if e.table else ""
        lines += [f"### {e.name} — {e.description}", "", f"Source: `{binding}` · load: {e.load} · key: {', '.join(e.primary_key) or '(package, member, row)'}", "",
                  "| Source column | Transforms | Target | Type | Null | Classification | Checks | Business rule |", "|---|---|---|---|---|---|---|---|"]
        for a in e.attributes:
            cls = b.policy.classification.get(f"{e.name}.{a.name}", b.policy.default_classification)
            tr = ", ".join(t.kind for t in a.transforms) or "—"
            ck = ", ".join(c.id for c in a.checks) or "—"
            typ = f"DECIMAL({a.precision},{a.scale})" if a.type == "DECIMAL" else a.type
            lines.append(f"| `{a.source_column}` | {tr} | `{a.name}` | {typ} | {'yes' if a.nullable else 'no'} | {cls} | {ck} | {a.business_rule or '—'} |")
        lines.append("")
    lines += ["## Checks", "", "| Id | Where | Kind | On fail | Mode | Intent |", "|---|---|---|---|---|---|"]
    if b.is_file_source:
        for c in s.package.checks:  # type: ignore[union-attr]
            lines.append(f"| {c.id} | package | {c.kind} | {c.on_fail} | {c.mode} | {c.description} |")
    for e in b.sttm.entities:
        for c in e.checks:
            lines.append(f"| {c.id} | {e.name} | {c.kind} | {c.on_fail} | {c.mode} | {c.description} |")
        for c in e.row_checks:
            lines.append(f"| {c.id} | {e.name} (row) | {c.kind} | {c.on_fail} | {c.mode} | {c.description} |")
        for a in e.attributes:
            for c in a.checks:
                lines.append(f"| {c.id} | {e.name}.{a.name} | {c.kind} | {c.on_fail} | {c.mode} | {c.description} |")
    for c in b.quality.checks:
        lines.append(f"| {c.id} | {c.entity} (lake) | {c.kind} | {c.severity} | enforce | {c.description} |")
    lines += ["", "## Evidence tables", "",
              "`pf_package_audit` (one row per delivery per run), `pf_entity_audit` (rows read/emitted/quarantined per entity),",
              "`pf_rule_results` (every rule, evaluated and failed counts), `pf_quarantine` (row numbers and rule ids, never values).", ""]
    return "\n".join(lines)


def fivetran_settings(b: Bundle, env: str) -> str:
    assert b.destination and b.pipeline
    d = b.destination.destination
    de = d.environments[env]
    sch = b.pipeline.schedule.get(env)
    payload = {
        "destination": {
            "group_name": de.fivetran_group,
            "create_destination_request": {
                "service": "managed_data_lake", "region": d.processing_region, "time_zone_offset": d.time_zone_offset,
                "run_setup_tests": True,
                "config": {"storage_provider": "AWS", "bucket": de.bucket, "prefix_path": de.prefix_path, "region": de.region,
                           "fivetran_role_arn": de.fivetran_role_arn, "should_maintain_tables_in_glue": de.glue_catalog,
                           "should_write_delta": de.write_delta, "snapshot_retention_period": de.snapshot_retention_period},
            },
        },
        "connection": {
            "name": b.name, "service": "connector_sdk", "python_version": b.pipeline.runtime.python_version,
            "schedule": (sch.model_dump() if sch else {"sync_frequency": 360, "daily_sync_time": None, "paused": False}),
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render(b: Bundle, lock: dict, header: str) -> dict[str, str]:
    assert b.pipeline
    out = {"contract.odcs.yaml": header + contract(b, lock), "README.md": readme(b, lock)}
    for env in b.pipeline.environments:
        out[f"fivetran/{env}.json"] = fivetran_settings(b, env)
    return out
