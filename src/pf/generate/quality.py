"""Post-load data-quality checks as SQL that runs on Athena (Trino) and, for dry runs, on DuckDB.

Each query returns exactly two columns: failed (0 = pass) and observed. Only SELECT statements are generated;
soft-deleted rows (_fivetran_deleted) are excluded.
"""
from __future__ import annotations

import json

from ..bundle import Bundle


def q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def live(table: str, schema: str, alias: str = "") -> str:
    a = f" {alias}" if alias else ""
    col = f"{alias}._fivetran_deleted" if alias else "_fivetran_deleted"
    return f"{q(schema)}.{q(table)}{a} WHERE COALESCE({col}, FALSE) = FALSE"


def check_sql(c, schema: str, entity_load: dict[str, str]) -> str:
    t = c.entity
    if c.kind == "row_count":
        cond = f"COUNT(*) >= {c.min}" + (f" AND COUNT(*) <= {c.max}" if c.max is not None else "")
        return f"SELECT CASE WHEN {cond} THEN 0 ELSE 1 END AS failed, COUNT(*) AS observed FROM {live(t, schema)}"
    if c.kind == "unique":
        cols = ", ".join(q(x) for x in c.columns)
        return (f"SELECT COUNT(*) AS failed, COUNT(*) AS observed FROM (SELECT {cols} FROM {live(t, schema)} "
                f"GROUP BY {cols} HAVING COUNT(*) > 1) d")
    if c.kind == "not_null":
        return f"SELECT COUNT(*) AS failed, COUNT(*) AS observed FROM {live(t, schema)} AND {q(c.column)} IS NULL"
    if c.kind == "freshness":  # when data last arrived, not when an unchanged row was last touched
        return (f"SELECT CASE WHEN MAX(finished_at) >= CURRENT_TIMESTAMP - INTERVAL '{c.max_hours}' HOUR THEN 0 ELSE 1 END AS failed, "
                f"COUNT(*) AS observed FROM {live('pf_package_audit', schema)} AND status = 'loaded'")
    if c.kind == "quarantine_rate":
        ea = live("pf_entity_audit", schema)
        return (f"SELECT CASE WHEN COALESCE(SUM(rows_quarantined), 0) * 100.0 > {c.max_pct} * GREATEST(COALESCE(SUM(rows_read), 0), 1) "
                f"THEN 1 ELSE 0 END AS failed, COALESCE(SUM(rows_quarantined), 0) AS observed FROM {ea} AND entity = '{t}' "
                f"AND _fivetran_synced >= CURRENT_TIMESTAMP - INTERVAL '{c.window_hours}' HOUR")
    if c.kind == "referential":
        return (f"SELECT COUNT(*) AS failed, COUNT(*) AS observed FROM {q(schema)}.{q(t)} a "
                f"LEFT JOIN (SELECT {q(c.ref_column)} AS k FROM {live(c.ref_entity, schema)}) r ON a.{q(c.column)} = r.k "
                f"WHERE COALESCE(a._fivetran_deleted, FALSE) = FALSE AND a.{q(c.column)} IS NOT NULL AND r.k IS NULL")
    if c.kind == "reconciliation":
        audit = f"{q(schema)}.{q('pf_entity_audit')}"
        orphans = (f"SELECT COUNT(*) FROM {live(t, schema, 'x')} AND NOT EXISTS (SELECT 1 FROM {audit} a "
                   f"WHERE a.package_hash = x.pf_package_hash AND a.entity = '{t}' AND a.status = 'loaded')")
        if entity_load.get(t) == "append":
            emitted = (f"SELECT COALESCE(SUM(rows_emitted), 0) FROM (SELECT DISTINCT package_hash, rows_emitted FROM {audit} "
                       f"WHERE entity = '{t}' AND status = 'loaded') e")
            count = f"SELECT COUNT(*) FROM {live(t, schema)}"
            return (f"SELECT ({orphans}) + ABS(({emitted}) - ({count})) AS failed, ({count}) AS observed")
        reg = q(schema) + "." + q(f"pf_keys_{t}")
        lost = (f"SELECT COUNT(*) FROM {reg} k WHERE COALESCE(k._fivetran_deleted, FALSE) = FALSE AND NOT EXISTS (SELECT 1 FROM "
                f"{live(t, schema, 'y')} AND y.pf_key_hash = k.key_hash AND y.pf_package_hash = k.package_hash)")
        unregistered = (f"SELECT COUNT(*) FROM {live(t, schema, 'z')} AND NOT EXISTS (SELECT 1 FROM {reg} k "
                        f"WHERE k.key_hash = z.pf_key_hash AND COALESCE(k._fivetran_deleted, FALSE) = FALSE)")
        return (f"SELECT ({orphans}) + ({lost}) + ({unregistered}) AS failed, (SELECT COUNT(*) FROM {live(t, schema)}) AS observed")
    if c.kind == "sql_assert":
        return f"SELECT failed, 0 AS observed FROM ({c.query.strip().rstrip(';')}) s"
    raise ValueError(c.kind)


def render(b: Bundle, lock: dict, header: str) -> dict[str, str]:
    assert b.quality and b.sttm
    schema = b.name
    loads = {e.name: e.load for e in b.sttm.entities}
    checks = []
    sql_lines = [header]
    for c in b.quality.checks:
        sql = check_sql(c, schema, loads)
        checks.append({"id": c.id, "kind": c.kind, "entity": c.entity, "severity": c.severity, "description": c.description, "sql": sql})
        sql_lines.append(f"-- {c.id} ({c.severity}): {c.description}\n{sql};\n")
    return {
        "checks.json": json.dumps({"pipeline": b.name, "schema": schema, "checks": checks}, indent=2, sort_keys=True) + "\n",
        "checks.sql": "\n".join(sql_lines),
    }
