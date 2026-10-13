"""`pf dq run` — run the generated post-load checks and decide certification.

Engines: athena (production, boto3) and duckdb (dry runs on the Fivetran local tester's warehouse.db).
Results are written as JSON (and, with --evidence-bucket, write-once to the audit bucket). With --certify
(athena), every table of the pipeline gets the Lake Formation tag pf_certified = true only when no blocker
failed; consumers' grants require pf_certified = true.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
import uuid
from pathlib import Path

from .envelope import PfError


def _load(root: Path, name: str) -> dict:
    f = root / "pipelines" / name / "build" / "quality" / "checks.json"
    if not f.exists():
        raise PfError("no generated checks; run `pf build` first", "not_built")
    return json.loads(f.read_text(encoding="utf-8"))


def _run_duckdb(checks: dict, db: str, schema: str | None) -> list[dict]:
    import duckdb

    import re

    con = duckdb.connect(db, read_only=True)
    out = []
    try:
        for c in checks["checks"]:
            sql = c["sql"]
            if schema:  # requalify quoted and unquoted references to the pipeline schema
                sql = re.sub(rf'(?<![\w"])"?{re.escape(checks["schema"])}"?\.', f'"{schema}".', sql)
            try:
                rows = con.execute(sql).fetchall()
                out.append(_outcome(c, rows))
            except Exception as exc:  # a check that cannot run is a failed check
                out.append({**c, "failed": 1, "observed": 0, "status": "error", "error": type(exc).__name__ + ": " + str(exc)[:200]})
    finally:
        con.close()
    return out


def _run_athena(checks: dict, workgroup: str, output: str | None, run_id: str, timeout_s: int = 600) -> list[dict]:
    import boto3

    ath = boto3.client("athena")
    out = []
    for c in checks["checks"]:
        kw = dict(QueryString=c["sql"], QueryExecutionContext={"Database": checks["schema"], "Catalog": "AwsDataCatalog"},
                  WorkGroup=workgroup, ClientRequestToken=hashlib.sha256(f"{run_id}:{c['id']}".encode()).hexdigest()[:64])
        if output:
            kw["ResultConfiguration"] = {"OutputLocation": output}
        qid = ath.start_query_execution(**kw)["QueryExecutionId"]
        deadline = time.time() + timeout_s
        while True:
            st = ath.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
            if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            if time.time() > deadline:
                ath.stop_query_execution(QueryExecutionId=qid)
                st = {"State": "TIMEOUT", "StateChangeReason": f"no result within {timeout_s}s"}
                break
            time.sleep(1)
        if st["State"] != "SUCCEEDED":
            out.append({**c, "failed": 1, "observed": 0, "status": "error", "error": st.get("StateChangeReason", st["State"])[:200]})
            continue
        rows = ath.get_query_results(QueryExecutionId=qid, MaxResults=3)["ResultSet"]["Rows"]
        data = [[d.get("VarCharValue") for d in r["Data"]] for r in rows[1:]]
        out.append({**_outcome(c, data), "query_execution_id": qid})
    return out


def _outcome(c: dict, rows: list) -> dict:
    """Exactly one row with a non-NULL integer `failed`; anything else is an error (fails closed, never a pass)."""
    if len(rows) != 1 or rows[0][0] is None:
        return {**c, "failed": 1, "observed": 0, "status": "error", "error": f"expected one row with a non-NULL 'failed', got {len(rows)} row(s)"}
    try:
        failed = int(rows[0][0])
        observed = int(rows[0][1]) if len(rows[0]) > 1 and rows[0][1] is not None else 0
    except (TypeError, ValueError):
        return {**c, "failed": 1, "observed": 0, "status": "error", "error": "'failed' is not an integer"}
    return {**c, "failed": failed, "observed": observed, "status": "fail" if failed else "pass"}


def _lake_formation_enforcing() -> str | None:
    """Tag-based grants only protect data when Lake Formation, not IAM, decides: refuse to certify when new tables
    would still get IAMAllowedPrincipals (the account default) or the database grants it."""
    import boto3

    s = boto3.client("lakeformation").get_data_lake_settings()["DataLakeSettings"]
    for key in ("CreateTableDefaultPermissions", "CreateDatabaseDefaultPermissions"):
        if any(p.get("Principal", {}).get("DataLakePrincipalIdentifier") == "IAM_ALLOWED_PRINCIPALS" for p in s.get(key, [])):
            return f"Lake Formation {key} still grants IAM_ALLOWED_PRINCIPALS: IAM, not the tag grants, controls access"
    return None


def _column_tags_missing(schema: str, tables: list[str], classification: dict[str, str]) -> list[str]:
    """Certification exposes a table to its consumers: every column with its own classification must already carry
    its tag, or it would inherit the (lower) table default."""
    import boto3

    lf = boto3.client("lakeformation")
    missing = []
    for t in tables:
        want = {k.split(".", 1)[1]: v for k, v in classification.items() if k.split(".", 1)[0] == t}
        if not want:
            continue
        got = lf.get_resource_lf_tags(Resource={"TableWithColumns": {"DatabaseName": schema, "Name": t, "ColumnNames": sorted(want)}},
                                      ShowAssignedLFTags=True)
        tagged = {c["Name"]: {x["TagKey"]: x["TagValues"] for x in c.get("LFTags", [])} for c in got.get("LFTagsOnColumns", [])}
        for col, level in want.items():
            if tagged.get(col, {}).get("pf_classification") != [level]:
                missing.append(f"{t}.{col} ({level})")
    return missing


def _snapshots(ath, schema: str, tables: list[str], workgroup: str, output: str | None) -> dict[str, str | None]:
    """Current Iceberg snapshot of each table: what exactly was certified."""
    out = {}
    for t in tables:
        kw = dict(QueryString=f'SELECT snapshot_id FROM "{schema}"."{t}$snapshots" ORDER BY committed_at DESC LIMIT 1',
                  QueryExecutionContext={"Database": schema, "Catalog": "AwsDataCatalog"}, WorkGroup=workgroup)
        if output:
            kw["ResultConfiguration"] = {"OutputLocation": output}
        try:
            qid = ath.start_query_execution(**kw)["QueryExecutionId"]
            for _ in range(120):
                st = ath.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
                if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
                    break
                time.sleep(1)
            rows = ath.get_query_results(QueryExecutionId=qid, MaxResults=2)["ResultSet"]["Rows"] if st == "SUCCEEDED" else []
            out[t] = rows[1]["Data"][0].get("VarCharValue") if len(rows) > 1 else None
        except Exception:  # noqa: BLE001 - evidence records the gap rather than failing the run
            out[t] = None
    return out


def _certify(schema: str, tables: list[str], certified: bool) -> None:
    import boto3

    lf = boto3.client("lakeformation")
    for t in tables:
        lf.add_lf_tags_to_resource(Resource={"Table": {"DatabaseName": schema, "Name": t}},
                                   LFTags=[{"TagKey": "pf_certified", "TagValues": ["true" if certified else "false"]}])


def _identity() -> str | None:
    try:
        import boto3

        return boto3.client("sts").get_caller_identity()["Arn"]
    except Exception:  # noqa: BLE001
        return None


def _put_evidence(bucket: str, key: str, doc: dict) -> None:
    import boto3

    body = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*", ChecksumAlgorithm="SHA256")


def run(root: Path, name: str, engine: str, *, db: str | None = None, schema: str | None = None, workgroup: str = "primary",
        output: str | None = None, certify: bool = False, evidence_bucket: str | None = None, timeout_s: int = 600) -> dict:
    checks = _load(root, name)
    if certify and engine != "athena":
        raise PfError("--certify applies to the athena engine", "invalid")
    if certify and not evidence_bucket:
        raise PfError("--certify needs --evidence-bucket: certification without a write-once record is not allowed", "invalid")
    run_id = uuid.uuid4().hex
    lock = json.loads((root / "pipelines" / name / "build" / "spec.lock.json").read_text(encoding="utf-8"))
    checks_file = root / "pipelines" / name / "build" / "quality" / "checks.json"
    if engine == "duckdb":
        if not db:
            raise PfError("--db path to warehouse.db is required for duckdb", "invalid")
        results = _run_duckdb(checks, db, schema)
    elif engine == "athena":
        results = _run_athena(checks, workgroup, output, run_id, timeout_s)
    else:
        raise PfError(f"unknown engine {engine}", "invalid")
    blockers = [r for r in results if r["severity"] == "blocker" and r["status"] != "pass"]
    entities = sorted({r["entity"] for r in results})
    report = {"pipeline": name, "run_id": run_id, "engine": engine, "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "ran_by": _identity() if engine == "athena" else None, "pipeline_version": lock["version"],
              "spec_sha256": lock["spec_sha256"], "checks_sha256": hashlib.sha256(checks_file.read_bytes()).hexdigest(),
              "workgroup": workgroup if engine == "athena" else None, "certified": not blockers,
              "results": [{**{k: v for k, v in r.items() if k != "sql"}, "sql_sha256": hashlib.sha256(r["sql"].encode()).hexdigest()}
                          for r in results]}
    if certify:
        import boto3

        refused = _lake_formation_enforcing()
        missing = _column_tags_missing(checks["schema"], entities, (lock.get("classification") or {})) if not blockers else []
        if missing and not refused:
            refused = f"column classification tags missing: {missing[:10]} (apply terraform/<env>/columns)"
        if refused:
            report["certified"] = False
            report["certification_refused"] = refused
        report["snapshots"] = _snapshots(boto3.client("athena"), checks["schema"], entities, workgroup, output)
        # evidence first (write-once), then the tag change, then a record that the change was applied
        _put_evidence(evidence_bucket, f"dq/{name}/{report['ran_at'][:10]}/{run_id}.json", report)
        _certify(checks["schema"], entities, report["certified"])
        _put_evidence(evidence_bucket, f"dq/{name}/{report['ran_at'][:10]}/{run_id}.certification.json",
                      {"run_id": run_id, "tables": entities, "pf_certified": report["certified"], "by": report["ran_by"],
                       "at": dt.datetime.now(dt.timezone.utc).isoformat(), "snapshots": report["snapshots"]})
        report["certification_applied"] = entities
    elif evidence_bucket:
        _put_evidence(evidence_bucket, f"dq/{name}/{report['ran_at'][:10]}/{run_id}.json", report)
    return report


def decertify(root: Path, name: str) -> dict:
    """Call at sync start (Fivetran webhook sync_start): consumers stop seeing tables until checks pass again."""
    checks = _load(root, name)
    entities = sorted({c["entity"] for c in checks["checks"]})
    _certify(checks["schema"], entities, False)
    return {"pipeline": name, "decertified": entities}
