"""Load and validate a pipeline's spec bundle (six files), including cross-file rules.

Validation is fully deterministic. It never calls an LLM.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from . import yamlio
from .envelope import Issue, PfError
from .specs.common import REQUIRED
from .specs.destination import DestinationFile
from .specs.entities import Entity, SttmFile
from .specs.pipeline import PipelineFile
from .specs.policy import PolicyFile
from .specs.quality import QualityFile
from .specs.source import FILE_SOURCES, SourceFile

SPEC_FILES: dict[str, type[BaseModel]] = {
    "pipeline.yaml": PipelineFile,
    "source.yaml": SourceFile,
    "destination.yaml": DestinationFile,
    "sttm.yaml": SttmFile,
    "quality.yaml": QualityFile,
    "policy.yaml": PolicyFile,
}
OWNER_OF_FILE = {
    "pipeline.yaml": "product_owner",
    "source.yaml": "data_engineer",
    "destination.yaml": "data_engineer",
    "sttm.yaml": "data_engineer",
    "quality.yaml": "quality",
    "policy.yaml": "governance",
    "rules.lock.yaml": "quality",
}
REPO_MARKER = "pf.toml"


def find_repo_root(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for p in (here, *here.parents):
        if (p / REPO_MARKER).exists():
            return p
    raise PfError(f"not inside a pipeline-factory repository (no {REPO_MARKER} found above {here})", "no_repo")


def pipeline_dir(name: str, root: Path | None = None) -> Path:
    root = root or find_repo_root()
    d = root / "pipelines" / name
    if not d.is_dir():
        raise PfError(f"pipeline '{name}' not found at {d}", "not_found")
    return d


@dataclass
class Bundle:
    name: str
    path: Path
    pipeline: PipelineFile | None = None
    source: SourceFile | None = None
    destination: DestinationFile | None = None
    sttm: SttmFile | None = None
    quality: QualityFile | None = None
    policy: PolicyFile | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def is_file_source(self) -> bool:
        return bool(self.source and self.source.source.type in FILE_SOURCES)

    def entity(self, name: str) -> Entity | None:
        for e in self.sttm.entities if self.sttm else []:
            if e.name == name:
                return e
        return None


def _placeholders(node: Any, loc: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            found += _placeholders(v, f"{loc}.{k}" if loc else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _placeholders(v, f"{loc}.{i}" if loc else str(i))
    elif isinstance(node, str) and REQUIRED in node:
        found.append(loc)
    return found


def _loc(err_loc: tuple, raw: Any) -> str:
    """Pydantic inserts discriminator tags (e.g. 'sftp', 'file_size') into locations; drop them so the
    location is the real YAML path a person (or agent) can find."""
    parts: list[str] = []
    node = raw
    for p in err_loc:
        if isinstance(node, dict) and p not in node and p in (node.get("type"), node.get("kind")):
            continue
        parts.append(str(p))
        if isinstance(node, dict):
            node = node.get(p)
        elif isinstance(node, list) and isinstance(p, int) and p < len(node):
            node = node[p]
        else:
            node = None
    return ".".join(parts)


def load_bundle(path: Path) -> Bundle:
    b = Bundle(name=path.name, path=path)
    for fname, model in SPEC_FILES.items():
        f = path / fname
        if not f.exists():
            b.issues.append(Issue(file=fname, msg=f"{fname} is missing", code="missing_file"))
            continue
        try:
            raw = yamlio.to_plain(yamlio.load(f)) or {}
        except Exception as exc:  # YAML syntax error
            b.issues.append(Issue(file=fname, msg=f"YAML syntax error: {exc}", code="yaml"))
            continue
        b.raw[fname] = raw
        holes = _placeholders(raw)
        for h in holes:
            b.issues.append(Issue(file=fname, loc=h, msg="value not filled in yet", code="required"))
        try:
            setattr(b, fname.removesuffix(".yaml"), model.model_validate(raw))
        except ValidationError as exc:
            for e in exc.errors():
                loc = _loc(e["loc"], raw)
                if any(loc == h or loc.startswith(h + ".") or h.startswith(loc + ".") for h in holes):
                    continue  # already reported as "not filled in yet"
                b.issues.append(Issue(file=fname, loc=loc, msg=e["msg"], code="invalid"))
    if all(getattr(b, n.removesuffix(".yaml")) is not None for n in SPEC_FILES):
        b.issues += cross_validate(b)
    return b


# ---------------------------------------------------------------------------- cross-file rules
def _cel_names(expr: str) -> tuple[set[str], str | None]:
    """Return attribute names referenced as row.<name>, and a parse error if any."""
    import celpy

    try:
        celpy.Environment().compile(expr)
    except Exception as exc:  # CELParseError
        return set(), f"CEL does not parse: {str(exc).splitlines()[0]}"
    return set(re.findall(r"\brow\.([A-Za-z_][A-Za-z0-9_]*)", expr)), None


def _python_ref_ok(root: Path, ref: str) -> str | None:
    module, func = ref.split(":")
    f = root / "rules" / f"{module}.py"
    if not f.exists():
        return f"rules/{module}.py not found"
    tree = ast.parse(f.read_text(encoding="utf-8"))
    if not any(isinstance(n, ast.FunctionDef) and n.name == func for n in tree.body):
        return f"function {func} not defined in rules/{module}.py"
    return None


def _bound_fits(bound: Any, type_: str) -> bool:
    import datetime as _dt

    numeric = type_ in ("SHORT", "INT", "LONG", "FLOAT", "DOUBLE", "DECIMAL")
    if isinstance(bound, bool):
        return False
    if numeric:
        return isinstance(bound, (int, float))
    if type_ in ("NAIVE_DATE", "NAIVE_DATETIME", "UTC_DATETIME"):
        try:
            (_dt.date.fromisoformat if type_ == "NAIVE_DATE" else lambda x: _dt.datetime.fromisoformat(x.replace("Z", "+00:00")))(str(bound))
            return True
        except ValueError:
            return False
    if type_ == "STRING":
        return isinstance(bound, str)
    return False


def cross_validate(b: Bundle) -> list[Issue]:
    out: list[Issue] = []
    p, s, d, t, q, pol = b.pipeline, b.source, b.destination, b.sttm, b.quality, b.policy
    assert p and s and d and t and q and pol

    if p.name != b.name:
        out.append(Issue(file="pipeline.yaml", loc="name", msg=f"name '{p.name}' must equal the folder name '{b.name}'", code="cross_ref"))
    envs = set(p.environments)
    for fname, got in (("destination.yaml", set(d.destination.environments)), ("policy.yaml", set(pol.environments))):
        if got != envs:
            out.append(Issue(file=fname, loc="environments", code="cross_ref",
                             msg=f"environments {sorted(got)} must match pipeline.yaml environments {sorted(envs)}"))

    file_source = s.source.type in FILE_SOURCES
    for i, e in enumerate(t.entities):
        if file_source and e.file is None:
            out.append(Issue(file="sttm.yaml", loc=f"entities.{i}", code="cross_ref", msg=f"{s.source.type} source: entity '{e.name}' needs a file binding"))
        if not file_source and e.table is None:
            out.append(Issue(file="sttm.yaml", loc=f"entities.{i}", code="cross_ref", msg=f"rds source: entity '{e.name}' needs a table binding"))
        if not file_source and e.load == "append":
            out.append(Issue(file="sttm.yaml", loc=f"entities.{i}.load", code="cross_ref", msg="rds entities must use load: upsert (append keys rows by package)"))
        if not file_source and e.checks:
            for j, c in enumerate(e.checks):
                if c.kind in ("control_count", "unique"):
                    out.append(Issue(file="sttm.yaml", loc=f"entities.{i}.checks.{j}", code="cross_ref",
                                     msg=f"{c.kind} applies to file sources only (database rows are keyed by primary_key)"))
        attr_names = {a.name for a in e.attributes}
        sources = {a.source_column for a in e.attributes}
        if e.table and e.table.incremental and e.table.incremental.cursor_column not in sources:
            out.append(Issue(file="sttm.yaml", loc=f"entities.{i}.table.incremental.cursor_column", code="cross_ref",
                             msg=f"cursor column '{e.table.incremental.cursor_column}' must be mapped by an attribute"))
        if e.table and e.table.incremental:
            cur = next((a for a in e.attributes if a.source_column == e.table.incremental.cursor_column), None)
            if cur is not None and cur.nullable:
                out.append(Issue(file="sttm.yaml", loc=f"entities.{i}.attributes.{e.attributes.index(cur)}.nullable", code="cross_ref",
                                 msg="the incremental cursor column must be nullable: false — rows with a NULL cursor are never read again "
                                     "(fix the source column, or drop incremental for a full reload)"))
        if file_source and e.load == "append" and s.source.package.on_redelivery == "reprocess":
            out.append(Issue(file="source.yaml", loc="source.package.on_redelivery", code="cross_ref",
                             msg=f"on_redelivery: reprocess with load: append (entity '{e.name}') keeps the old rows next to the new ones; use upsert or reject"))
        for j, a in enumerate(e.attributes):
            for k, c in enumerate(a.checks):
                if c.kind == "range":
                    for bound in (c.min, c.max):
                        if bound is not None and not _bound_fits(bound, a.type):
                            out.append(Issue(file="sttm.yaml", loc=f"entities.{i}.attributes.{j}.checks.{k}", code="invalid",
                                             msg=f"range bound {bound!r} is not a {a.type} value (numbers for numeric types, "
                                                 "ISO dates/datetimes for date types, text only for STRING)"))
            if a.type == "DECIMAL" and (a.scale or 0) > 10:
                out.append(Issue(file="sttm.yaml", loc=f"entities.{i}.attributes.{j}.scale", code="invalid",
                                 msg="the Managed Data Lake stores DECIMAL with scale above 10 as DECIMAL(38,10) (or STRING for keys); use scale <= 10"))
        for j, c in enumerate(e.checks):
            if c.kind == "unique":
                bad = [x for x in c.columns if x not in attr_names]
                if bad:
                    out.append(Issue(file="sttm.yaml", loc=f"entities.{i}.checks.{j}.columns", code="cross_ref", msg=f"unknown columns {bad}"))
        exprs: list[tuple[str, str]] = []
        for j, rc in enumerate(e.row_checks):
            if rc.kind == "expression":
                exprs.append((f"entities.{i}.row_checks.{j}.expr", rc.expr))
        for k, a in enumerate(e.attributes):
            for j, c in enumerate(a.checks):
                if c.kind == "expression":
                    exprs.append((f"entities.{i}.attributes.{k}.checks.{j}.expr", c.expr))
                if c.kind == "range" and a.type in ("NAIVE_DATE", "NAIVE_DATETIME", "UTC_DATETIME") and not all(
                    isinstance(v, str) or v is None for v in (c.min, c.max)
                ):
                    out.append(Issue(file="sttm.yaml", loc=f"entities.{i}.attributes.{k}.checks.{j}", code="invalid",
                                     msg="date/time range bounds must be ISO strings"))
            for j, tr in enumerate(a.transforms):
                if tr.kind == "derive":
                    exprs.append((f"entities.{i}.attributes.{k}.transforms.{j}.expr", tr.expr))
            if a.type in ("NAIVE_DATE", "NAIVE_DATETIME", "UTC_DATETIME") and a.input_format:
                try:
                    import datetime as _dt

                    _dt.datetime(2024, 1, 31, 12, 30, 45).strftime(a.input_format)
                except ValueError:
                    out.append(Issue(file="sttm.yaml", loc=f"entities.{i}.attributes.{k}.input_format", code="invalid", msg="not a valid strptime format"))
        for loc, expr in exprs:
            names, err = _cel_names(expr)
            if err:
                out.append(Issue(file="sttm.yaml", loc=loc, msg=err, code="invalid"))
            unknown = sorted(names - attr_names)
            if unknown:
                out.append(Issue(file="sttm.yaml", loc=loc, code="cross_ref", msg=f"expression uses unknown attributes {unknown}"))
        for loc, ref in _python_refs(e, i):
            err = _python_ref_ok(b.path, ref)
            if err:
                out.append(Issue(file="sttm.yaml", loc=loc, msg=err, code="cross_ref"))
        for loc, n in _english_rules(e, i):
            if p.gxp_tier != "non_gxp" and n < 3:
                out.append(Issue(file="sttm.yaml", loc=loc, code="invalid",
                                 msg=f"{p.gxp_tier} pipelines need at least 3 owner-labelled examples per English rule (found {n})"))

    if file_source:
        pk = s.source.package
        for j, c in enumerate(pk.checks):
            if c.kind == "expression":
                _, err = _cel_names(c.expr)
                if err:
                    out.append(Issue(file="source.yaml", loc=f"source.package.checks.{j}.expr", msg=err))
            if c.kind == "python":
                err = _python_ref_ok(b.path, c.ref)
                if err:
                    out.append(Issue(file="source.yaml", loc=f"source.package.checks.{j}.ref", msg=err, code="cross_ref"))
            if c.kind == "english" and p.gxp_tier != "non_gxp" and len(c.examples) < 3:
                out.append(Issue(file="source.yaml", loc=f"source.package.checks.{j}", code="invalid",
                                 msg=f"{p.gxp_tier} pipelines need at least 3 owner-labelled examples per English rule"))
        if pol.archive_originals:
            for env, ep in pol.environments.items():
                if ep.originals is None:
                    out.append(Issue(file="policy.yaml", loc=f"environments.{env}.originals", code="required",
                                     msg="archive_originals is true: set the originals bucket for this environment"))
        if s.source.type == "ftp" and s.source.tls == "none":
            gxp_ftp = p.gxp_tier != "non_gxp"
            out.append(Issue(severity="error" if gxp_ftp else "warning", file="source.yaml", loc="source.tls",
                             msg="plain FTP sends credentials and data unencrypted" + (" — not allowed for GxP pipelines" if gxp_ftp
                                                                                       else "; needs a documented governance exception")))
        if p.gxp_tier != "non_gxp" and s.source.package.change_detection == "metadata":
            out.append(Issue(severity="warning", file="source.yaml", loc="source.package.change_detection",
                             msg="GxP: a re-upload that keeps size and modification time is not noticed with change_detection: metadata; "
                                 "consider content (costs a download per known delivery per sync)"))

    if b.source and b.pipeline and b.source.source.type == "rds":
        mode = b.source.source.ssl_mode
        gxp = b.pipeline.gxp_tier != "non_gxp"
        if mode != "verify-full":
            out.append(Issue(severity="error" if gxp and mode == "disable" else "warning", file="source.yaml", loc="source.ssl_mode",
                             msg=f"ssl_mode {mode} does not verify the database server's identity; use verify-full with ca_cert "
                                 f"(the AWS RDS CA bundle)" + (" — GxP pipelines must not disable TLS" if gxp and mode == "disable" else "")))
        elif b.source.source.ca_cert is None:
            out.append(Issue(severity="warning", file="source.yaml", loc="source.ca_cert",
                             msg="verify-full without ca_cert uses the system trust store, which does not contain the AWS RDS CA"))

    # Fivetran's destination naming inserts "_" at letter/digit boundaries (sha256 -> sha_256): a name that would be
    # renamed breaks every downstream reference (quality SQL, Lake Formation column tags, consumers)
    if t:
        for i, e in enumerate(t.entities):
            for loc, nm in [(f"entities.{i}.name", e.name)] + [(f"entities.{i}.attributes.{j}.name", a.name) for j, a in enumerate(e.attributes)]:
                if re.search(r"[a-z][0-9]|[0-9][a-z]|__|_$", nm):
                    fixed = re.sub(r"_+", "_", re.sub(r"(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])", "_", nm)).rstrip("_")
                    out.append(Issue(file="sttm.yaml", loc=loc, code="invalid",
                                     msg=f"'{nm}' would be renamed '{fixed}' in the destination (Fivetran naming); use '{fixed}'"))

    # Glue allows one table per schema.table per region: two environments in one account+region would collide
    if d.destination.environments:
        seen_loc: dict[tuple, str] = {}
        for env, de in d.destination.environments.items():
            if not de.glue_catalog:
                continue
            key = (de.fivetran_role_arn.split(":")[4], de.region)
            if key in seen_loc:
                out.append(Issue(file="destination.yaml", loc=f"destination.environments.{env}", code="cross_ref",
                                 msg=f"'{env}' and '{seen_loc[key]}' register the same Glue database '{p.name}' in account {key[0]} "
                                     f"region {key[1]}; use separate accounts or regions per environment"))
            seen_loc[key] = env
        if p.gxp_tier != "non_gxp":
            for env, de in d.destination.environments.items():
                if env in ("prod", "production") and de.snapshot_retention_period != "RETAIN_ALL_SNAPSHOTS":
                    out.append(Issue(severity="warning", file="destination.yaml", loc=f"destination.environments.{env}.snapshot_retention_period",
                                     msg="GxP: Iceberg history of the lake tables expires after this period; evidence is also written "
                                         "write-once to the audit bucket, but consider RETAIN_ALL_SNAPSHOTS"))

    # every check id unique across the pipeline (evidence and rules.lock are keyed by id)
    ids: dict[str, str] = {}
    for loc, rid in _all_rule_ids(b):
        if rid in ids:
            out.append(Issue(file=loc.split(":")[0], loc=loc.split(":")[1], code="cross_ref", msg=f"check id '{rid}' is also used at {ids[rid]}"))
        else:
            ids[rid] = loc

    entities = {e.name: e for e in t.entities}
    for j, c in enumerate(q.checks):
        e = entities.get(c.entity)
        if e is None:
            out.append(Issue(file="quality.yaml", loc=f"checks.{j}.entity", code="cross_ref", msg=f"unknown entity '{c.entity}'"))
            continue
        cols = {a.name for a in e.attributes}
        named = [getattr(c, "column", None), *getattr(c, "columns", [])]
        bad = [x for x in named if x and x not in cols]
        if bad:
            out.append(Issue(file="quality.yaml", loc=f"checks.{j}", code="cross_ref", msg=f"unknown columns {bad} on {c.entity}"))
        if c.kind == "referential":
            ref = entities.get(c.ref_entity)
            if ref is None or c.ref_column not in {a.name for a in ref.attributes}:
                out.append(Issue(file="quality.yaml", loc=f"checks.{j}", code="cross_ref", msg=f"unknown reference {c.ref_entity}.{c.ref_column}"))
        if c.id in ids:
            out.append(Issue(file="quality.yaml", loc=f"checks.{j}.id", code="cross_ref", msg=f"check id '{c.id}' is also used at {ids[c.id]}"))

    known = {f"{e.name}.{a.name}" for e in t.entities for a in e.attributes}
    for k in pol.classification:
        if k not in known:
            out.append(Issue(file="policy.yaml", loc=f"classification.{k}", code="cross_ref", msg=f"unknown attribute '{k}'"))

    secrets = collect_secrets(b)
    if len(secrets) > 90:
        out.append(Issue(file="source.yaml", msg=f"{len(secrets)} secrets exceed the Fivetran configuration limit (100 keys, 10 reserved)"))
    return out


def _python_refs(e: Entity, i: int):
    for k, a in enumerate(e.attributes):
        for j, c in enumerate(a.checks):
            if c.kind == "python":
                yield f"entities.{i}.attributes.{k}.checks.{j}.ref", c.ref
    for j, c in enumerate(e.row_checks):
        if c.kind == "python":
            yield f"entities.{i}.row_checks.{j}.ref", c.ref


def _english_rules(e: Entity, i: int):
    for k, a in enumerate(e.attributes):
        for j, c in enumerate(a.checks):
            if c.kind == "english":
                yield f"entities.{i}.attributes.{k}.checks.{j}", len(c.examples)
    for j, c in enumerate(e.row_checks):
        if c.kind == "english":
            yield f"entities.{i}.row_checks.{j}", len(c.examples)


def _all_rule_ids(b: Bundle):
    if b.source and b.source.source.type in FILE_SOURCES:
        for j, c in enumerate(b.source.source.package.checks):
            yield f"source.yaml:source.package.checks.{j}", c.id
    for i, e in enumerate(b.sttm.entities if b.sttm else []):
        for j, c in enumerate(e.checks):
            yield f"sttm.yaml:entities.{i}.checks.{j}", c.id
        for j, c in enumerate(e.row_checks):
            yield f"sttm.yaml:entities.{i}.row_checks.{j}", c.id
        for k, a in enumerate(e.attributes):
            for j, c in enumerate(a.checks):
                yield f"sttm.yaml:entities.{i}.attributes.{k}.checks.{j}", c.id


def collect_secrets(b: Bundle, include_originals: bool = True) -> list[str]:
    """Every configuration key the connector needs at runtime (CI fills them from the secret store)."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if set(node) == {"secret"} and isinstance(node["secret"], str):
                found.append(node["secret"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for fname in ("source.yaml", "sttm.yaml"):
        walk(b.raw.get(fname, {}))
    if include_originals and b.policy and b.policy.archive_originals and b.is_file_source:
        for ep in b.policy.environments.values():
            if ep.originals:
                found += [ep.originals.access_key_id.secret, ep.originals.secret_access_key.secret]
    return sorted(set(found))
