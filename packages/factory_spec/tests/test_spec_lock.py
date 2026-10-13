"""spec.lock.json: golden lock of the worked example, lock checks and hash stability."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from factory_spec.load import load_spec
from factory_spec.lock import LOCK_FILE, build_lock, check_lock, lock_text
from factory_spec.spec import PipelineSpec
from spec_helpers import EXAMPLE_REPO, PIPELINE, edit, rule, task


def _spec(d: Path) -> PipelineSpec:
    res = load_spec(d)
    assert res.spec is not None, res.issues
    return res.spec


def _hashes(d: Path) -> dict[str, Any]:
    return build_lock(_spec(d))["hashes"]  # type: ignore[no-any-return]


def test_committed_lock_is_current() -> None:
    """Golden lock: the committed spec.lock.json of the worked example matches the spec byte for byte."""
    spec = _spec(EXAMPLE_REPO / "pipelines" / PIPELINE)
    assert check_lock(spec) == []


def test_lock_missing_and_stale(pipeline_dir: Path) -> None:
    edit(pipeline_dir, "rules.yaml", lambda doc: rule(doc, "r_zip")["implementation"]["params"].update(level=9))
    assert [i.code for i in check_lock(_spec(pipeline_dir))] == ["lock_stale"]
    (pipeline_dir / LOCK_FILE).unlink()
    assert [i.code for i in check_lock(_spec(pipeline_dir))] == ["lock_missing"]


def test_lock_shape(pipeline_dir: Path) -> None:
    lock = build_lock(_spec(pipeline_dir))
    assert set(lock) == {
        "lock_version",
        "api_version",
        "schema_version",
        "pipeline_id",
        "spec_version",
        "logic",
        "bindings",
        "derived",
        "hashes",
    }
    assert sorted(lock["bindings"]) == ["dev", "ppt", "prod", "test"]
    # sizes and durations are integers; nothing hashed is a float
    assert lock["logic"]["pipeline"]["workload"]["bytes_per_run"]["max"] == 10_000_000_000
    assert lock["logic"]["pipeline"]["reliability"]["replay_window"] == 30 * 86400
    # id-keyed lists are sorted in the lock, whatever the YAML order
    rule_ids = [r["rule_id"] for r in lock["logic"]["rules"]["rules"]]
    assert rule_ids == sorted(rule_ids)
    arts = lock["derived"]["artifacts"]
    assert arts["files"]["durability"] == "stream"
    assert arts["package"]["durability"] == "staged"
    assert arts["delivered_x"]["durability"] == "final"
    assert set(lock["derived"]["op_contracts"]) >= {"destination.deliver", "package.zip", "s3.list"}


def test_reordering_does_not_change_hashes(pipeline_dir: Path) -> None:
    before = _hashes(pipeline_dir)
    edit(pipeline_dir, "rules.yaml", lambda doc: doc["rules"].reverse())
    edit(pipeline_dir, "destinations.yaml", lambda doc: [doc["destinations"][e].reverse() for e in doc["destinations"]])

    def reorder_integrations(doc: Any) -> None:
        doc["destinations"].reverse()
        secrets = dict(doc["secrets"])
        doc["secrets"].clear()
        for k in reversed(list(secrets)):
            doc["secrets"][k] = secrets[k]
        doc["destinations"][0]["allowed_types"] = list(reversed(doc["destinations"][0]["allowed_types"]))

    edit(pipeline_dir, "integrations.yaml", reorder_integrations)
    assert _hashes(pipeline_dir) == before


def test_equivalent_units_hash_identically(pipeline_dir: Path) -> None:
    before = _hashes(pipeline_dir)["logic_sha"]
    edit(pipeline_dir, "pipeline.yaml", lambda doc: doc["workload"]["bytes_per_run"].update(max="10000MB"))
    edit(pipeline_dir, "pipeline.yaml", lambda doc: task(doc, "t2_checksum").update(timeout="900s"))
    assert _hashes(pipeline_dir)["logic_sha"] == before


def test_cosmetic_metadata_does_not_change_logic_sha(pipeline_dir: Path) -> None:
    before = _hashes(pipeline_dir)

    def cosmetic(doc: Any) -> None:
        doc["metadata"].update(name="Renamed", description="Another description", spec_version="0.2.0")
        doc["metadata"]["owners"]["business_owner"] = "@someone-else"
        task(doc, "t1_collect")["label"] = "Another label"

    edit(pipeline_dir, "pipeline.yaml", cosmetic)
    assert _hashes(pipeline_dir) == before


def test_logic_change_moves_logic_sha_only(pipeline_dir: Path) -> None:
    before = _hashes(pipeline_dir)
    edit(pipeline_dir, "rules.yaml", lambda doc: rule(doc, "r_zip")["implementation"]["params"].update(level=9))
    after = _hashes(pipeline_dir)
    assert after["logic_sha"] != before["logic_sha"]
    assert after["binding_sha"] == before["binding_sha"]


def test_plain_english_is_part_of_logic(pipeline_dir: Path) -> None:
    before = _hashes(pipeline_dir)["logic_sha"]
    edit(pipeline_dir, "rules.yaml", lambda doc: rule(doc, "r_zip").update(plain_english="Zip the files, any order."))
    assert _hashes(pipeline_dir)["logic_sha"] != before


def test_prod_only_change_moves_only_prod_binding(pipeline_dir: Path) -> None:
    before = _hashes(pipeline_dir)
    edit(
        pipeline_dir,
        "destinations.yaml",
        lambda doc: doc["destinations"]["prod"][1].update(host_key_fingerprint="SHA256:" + "A" * 43),
    )
    after = _hashes(pipeline_dir)
    assert after["logic_sha"] == before["logic_sha"]
    assert after["derived_sha"] == before["derived_sha"]
    changed = {env for env in before["binding_sha"] if after["binding_sha"][env] != before["binding_sha"][env]}
    assert changed == {"prod"}


def test_lock_is_identical_across_processes() -> None:
    """No dependence on hash seeds, dict order or the working directory."""
    code = (
        "import sys; from pathlib import Path; from factory_spec.load import load_spec; from factory_spec.lock import "
        "lock_text; sys.stdout.write(lock_text(load_spec(Path(sys.argv[1])).spec))"
    )
    outs = []
    for seed in ("0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", code, str(EXAMPLE_REPO / "pipelines" / PIPELINE)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            cwd="/",
        )
        outs.append(out.stdout)
    assert outs[0] == outs[1]
    assert outs[0] == lock_text(_spec(EXAMPLE_REPO / "pipelines" / PIPELINE))
