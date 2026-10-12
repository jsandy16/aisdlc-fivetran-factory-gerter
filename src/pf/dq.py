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

    con = duckdb.connect(db, read_only=True)
    out = []
    try:
        for c in checks["checks"]:
            sql = c["sql"] if not schema else c["sql"].replace(f'"{checks["schema"]}".', f'"{schema}".')
            try:
                failed, observed = con.execute(sql).fetchone()
                out.append({**c, "failed": int(failed or 0), "observed": int(observed or 0), "status": "fail" if failed else "pass"})
            except Exception as exc:  # a check that cannot run is a failed check
                out.append({**c, "failed": 1, "observed": 0, "status": "error", "error": type(exc).__name__ + ": " + str(exc)[:200]})
    finally:
        con.close()
    return out


def _run_athena(checks: dict, workgroup: str, output: str | None, run_id: str) -> list[dict]:
    import boto3

    ath = boto3.client("athena")
    out = []
    for c in checks["checks"]:
        kw = dict(QueryString=c["sql"], QueryExecutionContext={"Database": checks["schema"], "Catalog": "AwsDataCatalog"},
                  WorkGroup=workgroup, ClientRequestToken=hashlib.sha256(f"{run_id}:{c['id']}".encode()).hexdigest()[:64])
        if output:
            kw["ResultConfiguration"] = {"OutputLocation": output}
        qid = ath.start_query_execution(**kw)["QueryExecutionId"]
        while True:
            st = ath.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
            if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(1)
        if st["State"] != "SUCCEEDED":
            out.append({**c, "failed": 1, "observed": 0, "status": "error", "error": st.get("StateChangeReason", st["State"])[:200]})
            continue
        rows = ath.get_query_results(QueryExecutionId=qid, MaxResults=2)["ResultSet"]["Rows"]
        vals = [d.get("VarCharValue") for d in rows[1]["Data"]] if len(rows) > 1 else ["1", "0"]
        failed = int(vals[0] or 0)
        out.append({**c, "failed": failed, "observed": int(vals[1] or 0), "status": "fail" if failed else "pass"})
    return out


def _certify(schema: str, tables: list[str], certified: bool) -> None:
    import boto3

    lf = boto3.client("lakeformation")
    for t in tables:
        lf.add_lf_tags_to_resource(Resource={"Table": {"DatabaseName": schema, "Name": t}},
                                   LFTags=[{"TagKey": "pf_certified", "TagValues": ["true" if certified else "false"]}])


def run(root: Path, name: str, engine: str, *, db: str | None = None, schema: str | None = None, workgroup: str = "primary",
        output: str | None = None, certify: bool = False, evidence_bucket: str | None = None) -> dict:
    checks = _load(root, name)
    run_id = uuid.uuid4().hex
    if engine == "duckdb":
        if not db:
            raise PfError("--db path to warehouse.db is required for duckdb", "invalid")
        results = _run_duckdb(checks, db, schema)
    elif engine == "athena":
        results = _run_athena(checks, workgroup, output, run_id)
    else:
        raise PfError(f"unknown engine {engine}", "invalid")
    blockers = [r for r in results if r["severity"] == "blocker" and r["status"] != "pass"]
    report = {"pipeline": name, "run_id": run_id, "engine": engine, "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "certified": not blockers, "results": [{k: v for k, v in r.items() if k != "sql"} for r in results]}
    if certify:
        if engine != "athena":
            raise PfError("--certify applies to the athena engine", "invalid")
        entities = sorted({r["entity"] for r in results})
        _certify(checks["schema"], entities, not blockers)
        report["certification_applied"] = entities
    if evidence_bucket:
        import boto3

        body = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
        boto3.client("s3").put_object(Bucket=evidence_bucket, Key=f"dq/{name}/{report['ran_at'][:10]}/{run_id}.json", Body=body,
                                      IfNoneMatch="*", ChecksumAlgorithm="SHA256")
    return report
