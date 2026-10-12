"""Fivetran Connector SDK project: connector.py (thin, fixed), spec.lock.json, vendored pf_runtime, reviewed
custom rules (pf_rules), pinned requirements.txt and a configuration example with every key the connector needs."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pf_runtime

from ..bundle import Bundle
from ..envelope import PfError

# Pinned to the versions the framework is tested with. fivetran_connector_sdk and requests are pre-installed
# by Fivetran and must not be listed. Names are written in the normalised form (lowercase, "_") that the SDK's
# requirements check compares literally: "psycopg2-binary==x" would read as a mismatch and be rewritten.
PINS = {
    "cel": ["cel_python==0.5.0", "google_re2==1.1.20251105", "pendulum==3.2.0", "lark==1.3.1", "jmespath==1.1.0", "pyyaml==6.0.3"],
    "sftp": ["paramiko==5.0.0"],
    "postgres": ["psycopg2_binary==2.9.13"],
    "mysql": ["pymysql==1.2.3"],
    "aws": ["boto3==1.43.111", "botocore==1.43.111"],
}
# Source adapters vendored per source type: a pipeline ships only the code (and imports) it can run.
SOURCE_MODULES = {"sftp": {"sftp.py"}, "ftp": {"ftp.py", "tlsx.py"}, "rds": {"rds.py", "tlsx.py"}}

# Every third-party import the vendored code may make, and the pinned distribution that provides it. `fivetran deploy`
# runs with --force, which skips the SDK's pipreqs-based (network) dependency rewrite; this offline check replaces it.
IMPORT_TO_DIST = {"celpy": "cel_python", "paramiko": "paramiko", "psycopg2": "psycopg2_binary", "pymysql": "pymysql",
                  "boto3": "boto3", "botocore": "botocore", "fivetran_connector_sdk": None}
LOCAL = {"pf_runtime", "pf_rules"}


def check_imports(files: dict[str, str | bytes], reqs: list[str]) -> None:
    """Fail the build if generated, vendored or custom code imports anything that is neither stdlib, local,
    pre-installed by Fivetran, nor pinned in this pipeline's requirements.txt."""
    pinned = {r.split("==")[0].lower() for r in reqs}
    problems = []
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse(content if isinstance(content, str) else content.decode("utf-8"), filename=path)
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in ("import_module", "__import__"):
                arg = node.args[0] if node.args else None  # dynamic imports count too, and must be literal
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names = [arg.value]
                elif isinstance(arg, ast.JoinedStr) and arg.values and isinstance(arg.values[0], ast.Constant) \
                        and str(arg.values[0].value).startswith("pf_rules."):
                    names = ["pf_rules"]  # custom rule modules, resolved from the reviewed rules/ folder
                elif isinstance(arg, ast.JoinedStr) and arg.values and isinstance(arg.values[0], ast.FormattedValue) \
                        and getattr(arg.values[0].value, "id", "") in ("__name__", "__package__"):
                    names = ["pf_runtime"]  # relative: a module of the vendored runtime
                else:
                    problems.append(f"{path}: dynamic import with a non-literal module name")
            for n in names:
                top = n.split(".")[0]
                if top in sys.stdlib_module_names or top in LOCAL or top == "__future__":
                    continue
                if top not in IMPORT_TO_DIST:
                    problems.append(f"{path}: import '{top}' is not an allowed dependency")
                elif IMPORT_TO_DIST[top] and IMPORT_TO_DIST[top] not in pinned:
                    problems.append(f"{path}: import '{top}' needs {IMPORT_TO_DIST[top]}, which this pipeline does not install")
    problems += _pipreqs_view(files)
    if problems:
        raise PfError("connector dependency check failed:\n" + "\n".join(problems), "dependencies")


def _pipreqs_view(files: dict[str, str | bytes]) -> list[str]:
    """Run the import scan the Connector SDK runs at deploy (pipreqs) over the generated project: every name it
    reports must be one the framework pins under a known import name. Anything else would be looked up on PyPI by
    import name at deploy, and `fivetran deploy --yes` would add whatever package answers to that name."""
    import tempfile

    try:
        from pipreqs import pipreqs as pr
    except ImportError:  # pipreqs ships with the Connector SDK CLI; without it this check cannot run
        return []
    with tempfile.TemporaryDirectory() as d:
        for path, content in files.items():
            if path.endswith(".py"):
                f = Path(d) / path
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(content if isinstance(content, bytes) else content.encode())
        names = pr.get_all_imports(d, encoding="utf-8")
    allowed = set(IMPORT_TO_DIST)
    return [f"the Connector SDK's dependency scan would see import '{n}', which the framework does not pin" for n in sorted(names)
            if n not in allowed]


CONNECTOR_PY = '''{header}"""Fivetran Connector SDK connector for pipeline `{name}` — {title}.

Everything this connector does is declared in spec.lock.json and executed by the vendored pf_runtime
library (version {runtime}). Read the specs in pipelines/{name}/ to understand it; nothing here is hand-written.
"""
import ast
import json
import sys
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
        req += PINS["postgres"] + PINS["mysql"]  # rds.py imports both drivers (lazily); pipreqs sees both
    req += PINS["aws"]  # the vendored package module imports boto3 for the originals archive
    return sorted(set(req))


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
    keep = SOURCE_MODULES[lock["source"]["type"]] | {"__init__.py"}
    for f in sorted(rt.rglob("*.py")):
        rel = f.relative_to(rt).as_posix()
        if rel.startswith("sources/") and f.name not in keep:
            continue
        out[f"pf_runtime/{rel}"] = f.read_bytes()
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
    check_imports(out, requirements(lock))
    return out
