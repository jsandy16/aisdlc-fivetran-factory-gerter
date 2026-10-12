"""Fivetran Connector SDK project: connector.py (thin, fixed), spec.lock.json, vendored pf_runtime, reviewed
custom rules (pf_rules), pinned requirements.txt and a configuration example with every key the connector needs."""
from __future__ import annotations

import json
from pathlib import Path

import pf_runtime

from ..bundle import Bundle

# Pinned to the versions the framework is tested with. fivetran_connector_sdk and requests are pre-installed
# by Fivetran and must not be listed.
PINS = {
    "cel": ["cel-python==0.5.0", "google-re2==1.1.20251105", "pendulum==3.2.0", "lark==1.3.1", "jmespath==1.1.0", "PyYAML==6.0.3"],
    "sftp": ["paramiko==5.0.0"],
    "postgres": ["psycopg2-binary==2.9.13"],
    "mysql": ["PyMySQL==1.2.3"],
    "boto3": ["boto3==1.43.111"],
}

CONNECTOR_PY = '''{header}"""Fivetran Connector SDK connector for pipeline `{name}` — {title}.

Everything this connector does is declared in spec.lock.json and executed by the vendored pf_runtime
library (version {runtime}). Read the specs in pipelines/{name}/ to understand it; nothing here is hand-written.
"""
import json
from pathlib import Path

from fivetran_connector_sdk import Connector
from fivetran_connector_sdk import Logging as log
from fivetran_connector_sdk import Operations as op

from pf_runtime import engine

LOCK = json.loads((Path(__file__).parent / "spec.lock.json").read_text(encoding="utf-8"))


def schema(configuration: dict):
    """Tables, primary keys and column types, including lineage columns and the pf_* evidence tables."""
    return engine.schema(LOCK)


def update(configuration: dict, state: dict):
    """One sync: read deliveries, run every structural, entity and attribute rule, emit in order, checkpoint."""
    engine.update(LOCK, configuration, state, op, log)


connector = Connector(update=update, schema=schema)

if __name__ == "__main__":
    with open("configuration.json", encoding="utf-8") as fh:
        connector.debug(configuration=json.load(fh))
'''


def requirements(lock: dict) -> list[str]:
    req = list(PINS["cel"])
    src = lock["source"]
    if src["type"] == "sftp":
        req += PINS["sftp"]
    if src["type"] == "rds":
        req += PINS["postgres"] if src["engine"] == "postgres" else PINS["mysql"]
    if lock["archive_originals"]:
        req += PINS["boto3"]
    return sorted(set(req), key=str.lower)


def render(b: Bundle, lock: dict, header: str) -> dict[str, str | bytes]:
    out: dict[str, str | bytes] = {}
    out["connector.py"] = CONNECTOR_PY.format(header=header, name=b.name, title=lock["title"], runtime=lock["runtime_version"])
    out["spec.lock.json"] = json.dumps(lock, indent=2, sort_keys=True) + "\n"
    out["requirements.txt"] = "".join(f"{r}\n" for r in requirements(lock))
    keys = sorted({k for ks in lock["secrets"].values() for k in ks})
    example = {"pf_environment": "|".join(lock["environments"])}
    example.update({k: "" for k in keys})
    out["configuration.example.json"] = json.dumps(example, indent=2, sort_keys=True) + "\n"
    rt = Path(pf_runtime.__file__).parent
    for f in sorted(rt.rglob("*.py")):
        out[f"pf_runtime/{f.relative_to(rt).as_posix()}"] = f.read_bytes()
    rules_dir = b.path / "rules"
    has_rules = False
    if rules_dir.is_dir():
        for f in sorted(rules_dir.glob("*.py")):
            out[f"pf_rules/{f.name}"] = f.read_bytes()
            has_rules = True
    if not has_rules:
        out["pf_rules/__init__.py"] = '"""No custom python rules in this pipeline."""\n'
    elif "pf_rules/__init__.py" not in out:
        out["pf_rules/__init__.py"] = ""
    return out
