"""Lake-side governance: foundation stack, contracts, DQ certification safety (findings of the lakehouse review)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.conftest import FRAMEWORK

EXAMPLES = ["oncology_labs", "site_monitoring", "trial_sites"]


@pytest.fixture
def three(tmp_path, monkeypatch):
    (tmp_path / "pf.toml").write_text("")
    for n in EXAMPLES:
        shutil.copytree(FRAMEWORK / "examples" / "pipelines" / n, tmp_path / "pipelines" / n, ignore=shutil.ignore_patterns("build"))
    monkeypatch.chdir(tmp_path)
    from pf.bundle import load_bundle
    from pf.generate import render_all, render_foundation, write, write_foundation

    for n in EXAMPLES:
        write(tmp_path, render_all(load_bundle(tmp_path / "pipelines" / n), tmp_path), n)
    write_foundation(tmp_path, render_foundation(tmp_path))
    return tmp_path


def _unq(x):
    if isinstance(x, dict):
        return {k.strip('"'): _unq(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_unq(v) for v in x]
    return x.strip('"') if isinstance(x, str) else x


def _hcl(path: Path) -> dict:
    import hcl2

    with open(path) as fh:
        return _unq(hcl2.load(fh))


def test_account_objects_declared_once_in_foundation(three):
    for env in ("dev", "prod"):
        lf = _hcl(three / "foundation" / env / "lake_formation.tf")
        tags = [r for blk in lf["resource"] for t, r in blk.items() if t == "aws_lakeformation_lf_tag"]
        assert len(tags) == 3
        pipeline_tag = next(v for r in tags for k, v in r.items() if k == "pipeline")
        assert sorted(pipeline_tag["values"]) == sorted(EXAMPLES)
        regs = [r for blk in lf["resource"] for t, r in blk.items() if t == "aws_lakeformation_resource"]
        assert regs and all(v["hybrid_access_enabled"] is False for r in regs for v in r.values())
        trail = _hcl(three / "foundation" / env / "trail.tf")
        trails = [v for blk in trail["resource"] for t, r in blk.items() if t == "aws_cloudtrail" for v in r.values()]
        assert trails and all(t["enable_log_file_validation"] is True for t in trails)
    for n in EXAMPLES:  # no per-pipeline stack declares account-level objects any more
        for f in (three / "pipelines" / n / "build" / "terraform").rglob("*.tf"):
            text = f.read_text()
            assert "aws_lakeformation_lf_tag\"" not in text and "aws_s3_bucket\"" not in text and "aws_cloudtrail" not in text, f


def test_glue_database_created_before_first_sync(three):
    acc = _hcl(three / "pipelines" / "oncology_labs" / "build" / "terraform" / "dev" / "access.tf")
    types = {t for blk in acc["resource"] for t in blk}
    assert {"aws_glue_catalog_database", "aws_lakeformation_resource_lf_tags", "aws_lakeformation_permissions"} <= types


def test_contracts_validate_against_odcs_v3_2_0(three):
    import jsonschema
    import yaml

    schema = json.loads((FRAMEWORK / "tests" / "schemas" / "odcs-json-schema-v3.2.0.json").read_text())
    for n in EXAMPLES:
        doc = yaml.safe_load((three / "pipelines" / n / "build" / "contract.odcs.yaml").read_text())
        errors = [e.message for e in jsonschema.Draft201909Validator(schema).iter_errors(doc)]
        assert errors == [], (n, errors[:3])


def test_null_check_result_is_an_error():
    from pf.dq import _outcome

    assert _outcome({"id": "x"}, [(None, 5)])["status"] == "error"
    assert _outcome({"id": "x"}, [])["status"] == "error"
    assert _outcome({"id": "x"}, [(0, 1), (0, 1)])["status"] == "error"
    assert _outcome({"id": "x"}, [(0, 3)])["status"] == "pass"


class _Fakes:
    def __init__(self, lf_defaults=None, column_tags=None):
        self.calls = []
        self.lf_defaults = lf_defaults or []
        self.column_tags = column_tags or {}

    def client(self, name, **kw):
        fakes = self

        class C:
            def get_data_lake_settings(self):
                return {"DataLakeSettings": {"CreateTableDefaultPermissions": fakes.lf_defaults}}

            def get_resource_lf_tags(self, **kw):
                cols = kw["Resource"]["TableWithColumns"]["ColumnNames"]
                return {"LFTagsOnColumns": [{"Name": c, "LFTags": [{"TagKey": "pf_classification", "TagValues": [fakes.column_tags[c]]}]}
                                            for c in cols if c in fakes.column_tags]}

            def add_lf_tags_to_resource(self, **kw):
                fakes.calls.append(("tag", kw["LFTags"][0]["TagValues"][0]))

            def put_object(self, **kw):
                fakes.calls.append(("evidence", kw["Key"].rsplit("/", 1)[-1]))

            def get_caller_identity(self):
                return {"Arn": "arn:aws:sts::1:assumed-role/dq/x"}

            def start_query_execution(self, **kw):
                return {"QueryExecutionId": "q"}

            def get_query_execution(self, **kw):
                return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

            def get_query_results(self, **kw):
                return {"ResultSet": {"Rows": [{"Data": []}, {"Data": [{"VarCharValue": "0"}, {"VarCharValue": "1"}]}]}}

            def stop_query_execution(self, **kw):
                pass
        return C()


def _certify(three, monkeypatch, fakes):
    import boto3

    from pf.dq import run

    monkeypatch.setattr(boto3, "client", fakes.client)
    return run(three, "oncology_labs", "athena", certify=True, evidence_bucket="audit")


def test_certify_needs_evidence(three):
    from pf.dq import run
    from pf.envelope import PfError

    with pytest.raises(PfError):
        run(three, "oncology_labs", "athena", certify=True)


def test_certify_writes_evidence_before_tagging(three, monkeypatch):
    lock = json.loads((three / "pipelines" / "oncology_labs" / "build" / "spec.lock.json").read_text())
    f = _Fakes(column_tags={k.split(".")[1]: v for k, v in lock["classification"].items()})
    rep = _certify(three, monkeypatch, f)
    kinds = [c[0] for c in f.calls]
    assert rep["certified"] is True and kinds.index("evidence") < kinds.index("tag") and kinds[-1] == "evidence"
    assert rep["ran_by"] and rep["checks_sha256"] and all("sql_sha256" in r for r in rep["results"])


def test_certify_refused_without_column_tags(three, monkeypatch):
    rep = _certify(three, monkeypatch, _Fakes(column_tags={}))
    assert rep["certified"] is False and "column classification tags missing" in rep["certification_refused"]


def test_certify_refused_when_lake_formation_not_enforcing(three, monkeypatch):
    f = _Fakes(lf_defaults=[{"Principal": {"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"}, "Permissions": ["ALL"]}],
               column_tags={"subject_key": "restricted"})
    rep = _certify(three, monkeypatch, f)
    assert rep["certified"] is False and "IAM_ALLOWED_PRINCIPALS" in rep["certification_refused"]
    assert ("tag", "false") in f.calls


def test_upsert_reconciliation_finds_lost_rows(tmp_path):
    """Lake rows deleted behind the connector's back are detected through the key registry."""
    import duckdb

    from pf.generate.quality import check_sql
    from pf.specs.quality import QualityFile

    c = QualityFile.model_validate({"ticket_route": "x", "checks": [{"id": "r", "kind": "reconciliation", "entity": "t",
                                                                      "severity": "blocker", "description": "reconciled"}]}).checks[0]
    sql = check_sql(c, "s", {"t": "upsert"})
    con = duckdb.connect(str(tmp_path / "w.db"))
    con.execute("CREATE SCHEMA s")
    con.execute("CREATE TABLE s.t (id INT, pf_key_hash TEXT, pf_package_hash TEXT, _fivetran_deleted BOOLEAN)")
    con.execute("CREATE TABLE s.pf_keys_t (key_hash TEXT, package_hash TEXT, _fivetran_deleted BOOLEAN)")
    con.execute("CREATE TABLE s.pf_entity_audit (package_hash TEXT, entity TEXT, status TEXT, rows_emitted INT, _fivetran_deleted BOOLEAN)")
    con.execute("INSERT INTO s.pf_entity_audit VALUES ('p1', 't', 'loaded', 3, FALSE)")
    for i in range(3):
        con.execute(f"INSERT INTO s.pf_keys_t VALUES ('k{i}', 'p1', FALSE)")
        con.execute(f"INSERT INTO s.t VALUES ({i}, 'k{i}', 'p1', FALSE)")
    assert con.execute(sql).fetchone()[0] == 0
    con.execute("DELETE FROM s.t WHERE id = 1")  # a row lost in the lake
    assert con.execute(sql).fetchone()[0] == 1
