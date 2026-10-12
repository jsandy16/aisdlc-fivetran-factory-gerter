"""`pf new` — generate the fill-in spec bundle for a source type and destination type.

Every field is present: defaults are filled in, values only a person can supply are <REQUIRED>,
and security-sensitive values (host keys, classifications, retention, buckets, roles) are never defaulted.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from jinja2 import Environment, StrictUndefined

from .catalog import comment_block
from .envelope import PfError
from .specs.common import REQUIRED

R = REQUIRED


def _c(text: str, indent: int = 0) -> str:
    pad = " " * indent
    return "\n".join(f"{pad}# {line}" if line else f"{pad}#" for line in text.splitlines())


PIPELINE = """\
# pipeline.yaml — identity, owners, requirement summary, environments and schedule.
# Owner: product owner. Fill every <REQUIRED>; keep or change the defaults.
api_version: pf/v1
name: {{ name }}                       # = Fivetran connection name = destination schema; never change after first deploy
version: 0.1.0                         # semver; `pf release` refuses a breaking change without a major bump
title: "{{ R }}"
gxp_tier: "{{ R }}"                    # non_gxp | gxp_indirect | gxp_direct  (decided with QA)
owners:                                # GitHub handles or @org/team — each approves the files they own (CODEOWNERS)
  product_owner: "{{ R }}"
  data_engineer: "{{ R }}"
  governance: "{{ R }}"
  quality: "{{ R }}"
requirement:
  purpose: "{{ R }}"
  consumers: ["{{ R }}"]
  freshness_sla_hours: 24
  late_data_policy: wait               # wait (hold newer until older are ready; reject late) | load_late | reject_late
  source_document: null                # set by `pf intake` when drafted from a requirement document
environments: [{{ envs|join(', ') }}]
schedule:
{%- for env in envs %}
  {{ env }}:
    sync_frequency: {{ 1440 if env == envs[0] and envs|length > 1 else 360 }}   # minutes: 1,5,15,30,60,120,180,360,480,720,1440
    daily_sync_time: null              # "HH:MM" UTC, only with 1440
    paused: {{ 'true' if env == envs[0] and envs|length > 1 else 'false' }}
{%- endfor %}
runtime:
  parallelism: 2                       # packages prepared in parallel (1-8); always emitted in order
  retry: {attempts: 5, base_seconds: 2.0, max_seconds: 60.0}
  checkpoint_every_rows: 5000
  python_version: "3.12"
"""

SOURCE_FILE = """\
# source.yaml — connection and delivery (package) rules. Owner: data engineer.
# Secrets are never written here: {secret: key} names a Fivetran configuration key that CI fills.
source:
  type: {{ type }}
  host: "{{ R }}"
  port: {{ 22 if type == 'sftp' else 21 }}
  username: "{{ R }}"
{%- if type == 'sftp' %}
  private_key: {secret: sftp_private_key}   # PEM key; or replace with password: {secret: sftp_password}
  host_key: "{{ R }}"                       # pinned server key, e.g. "ssh-ed25519 AAAA..." (ssh-keyscan); never defaulted
{%- else %}
  password: {secret: ftp_password}
  tls: explicit                             # explicit = FTPS; none needs a governance exception
  passive: true
  ca_cert: null                             # {secret: key} holding a PEM CA bundle if the server uses a private CA
{%- endif %}
  root: "{{ R }}"                           # folder scanned recursively, e.g. /outbound
  timeout_seconds: 60
  package:
    match: "{{ R }}"                        # regex on the path under root, with named groups, e.g. '^(?P<date>\\\\d{8})/LB_(?P<seq>\\\\d{3})\\\\.zip$'
    order_by: []                            # named groups that order deliveries, e.g. [date, seq]
    readiness: {min_age_seconds: 300, marker: null}
    on_redelivery: reject                   # same path, new content: skip | reprocess | reject
    max_package_bytes: 2147483648
    transforms: []                          # e.g. [{kind: unzip}]
{{ package_catalog }}
    checks:
      - {id: package_not_empty, kind: not_empty, description: "A delivery with no bytes is a broken source"}
      - {id: package_size, kind: file_size, min_bytes: 1, max_bytes: 2147483648, description: "Delivery size within agreed bounds"}
      - {id: package_not_duplicate, kind: duplicate_package, description: "The same file content is never loaded twice"}
      - id: package_name
        kind: file_name_pattern
        pattern: "{{ R }}"
        description: "File names follow the agreed naming convention"
"""

SOURCE_RDS = """\
# source.yaml — database connection. Owner: data engineer.
# Secrets are never written here: {secret: key} names a Fivetran configuration key that CI fills.
source:
  type: rds
  engine: "{{ R }}"                         # postgres | mysql
  host: "{{ R }}"                           # RDS endpoint
  port: null                                # default 5432 (postgres) / 3306 (mysql)
  database: "{{ R }}"
  username: "{{ R }}"                       # read-only user with SELECT on the mapped tables only
  password: {secret: rds_password}
  ssl_mode: verify-full                     # verify-full (verifies the server) | require (encrypted only) | disable
  ca_cert: {secret: rds_ca_bundle}          # PEM: AWS RDS global bundle (https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem)
  batch_size: 5000
  timeout_seconds: 60
"""

DESTINATION = """\
# destination.yaml — Fivetran Managed Data Lake on Amazon S3, per environment. Owner: data engineer (platform).
# Bucket, prefix, region and role are immutable once the Fivetran destination exists.
destination:
  type: s3
  processing_region: "{{ R }}"              # Fivetran processing location, e.g. AWS_US_EAST_1, AWS_EU_WEST_1
  time_zone_offset: "0"
  environments:
{%- for env in envs %}
    {{ env }}:
      fivetran_group: "{{ R }}"             # Fivetran destination (group) name
      bucket: "{{ R }}"
      prefix_path: fivetran
      region: "{{ R }}"                     # e.g. us-east-1
      fivetran_role_arn: "{{ R }}"          # arn:aws:iam::<account>:role/<name>
      glue_catalog: true                    # can only be chosen when the destination is created
      snapshot_retention_period: ONE_WEEK
      write_delta: false
{%- endfor %}
"""

STTM = """\
# sttm.yaml — source-to-target mapping. Owner: BA (meaning) + data engineer (technical columns).
# One entity per target table; one attribute per target column (source column -> transforms -> checks).
{{ catalog }}
entities:
{%- for e in entities %}
  - name: {{ e }}
    description: "{{ R }}"
{%- if file %}
    file:
      member: "{{ R }}"                     # regex on the member path inside the package, e.g. '^{{ e }}_.*\\\\.csv$'
      format: {type: csv, delimiter: ",", quotechar: '"', header: true, encoding: utf-8, null_tokens: [""]}
      extra_columns: reject                 # reject | ignore source columns not mapped below
    load: upsert                            # upsert (needs primary_key) | append
{%- else %}
    table:
      table: "{{ R }}"                      # schema.table
      incremental: {cursor_column: "{{ R }}", initial_value: null}   # remove to reload the full table each sync
    load: upsert
{%- endif %}
    primary_key: ["{{ R }}"]
    attributes:
      - name: "{{ R }}"                     # target column (snake_case)
        source: null                        # source column if different
        type: STRING                        # STRING INT LONG DOUBLE DECIMAL BOOLEAN NAIVE_DATE NAIVE_DATETIME UTC_DATETIME JSON
        nullable: false
        description: "{{ R }}"
        business_rule: ""
        transforms: []
        checks: []
    row_checks: []
    checks:
      - {id: {{ e }}_has_rows, kind: row_count, min: 1, description: "Every delivery of {{ e }} has rows"}
{%- endfor %}
"""

QUALITY = """\
# quality.yaml — whole-table checks after each sync, run on the lake (Athena). Owner: data quality.
# severity: blocker keeps the table uncertified for consumers (a human-owned decision); warning only alerts.
{{ catalog }}
ticket_route: "{{ R }}"
checks:
{%- for e in entities %}
  - {id: {{ e }}_lake_rows, kind: row_count, entity: {{ e }}, min: 1, severity: blocker, description: "{{ e }} is not empty"}
  - {id: {{ e }}_fresh, kind: freshness, entity: {{ e }}, max_hours: 24, severity: warning, description: "{{ e }} synced within the SLA"}
  - {id: {{ e }}_reconciled, kind: reconciliation, entity: {{ e }}, severity: blocker, description: "Lake rows equal rows the connector emitted"}
{%- endfor %}
"""

POLICY = """\
# policy.yaml — governance decisions, enforced outside Fivetran. Owner: governance.
# Classification, access and retention are human-owned decisions: none of them is defaulted.
default_classification: "{{ R }}"           # public | internal | confidential | restricted
classification: {}                          # entity.attribute: classification, for columns that differ from the default
archive_originals: {{ 'true' if file else 'false' }}                    # keep every received file byte-for-byte (file sources)
retention:
  days: "{{ R }}"
  rationale: "{{ R }}"
  object_lock_days: "{{ R }}"
environments:
{%- for env in envs %}
  {{ env }}:
    access: []                              # e.g. - {principal: arn:aws:iam::123456789012:role/analyst, classifications: [internal], justification: "..."}
    audit:
      bucket: "{{ R }}"                     # write-once evidence bucket (S3 Object Lock)
      object_lock_mode: {{ 'GOVERNANCE' if env != envs[-1] else 'COMPLIANCE' }}
      cloudtrail_data_events: true
{%- if file %}
    originals:
      bucket: "{{ R }}"                     # write-once bucket for original files
      prefix: originals
      region: "{{ R }}"
      access_key_id: {secret: originals_access_key_id_{{ env|replace('-', '_') }}}
      secret_access_key: {secret: originals_secret_access_key_{{ env|replace('-', '_') }}}
{%- endif %}
{%- endfor %}
"""


def render_bundle(name: str, source_type: str, destination_type: str, entities: list[str], envs: list[str]) -> dict[str, str]:
    if source_type not in ("sftp", "ftp", "rds"):
        raise PfError(f"unsupported source type '{source_type}' (supported: sftp, ftp, rds)", "unsupported")
    if destination_type != "s3":
        raise PfError(f"unsupported destination type '{destination_type}' (supported: s3)", "unsupported")
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)
    file = source_type in ("sftp", "ftp")
    ctx = {"name": name, "R": R, "envs": envs, "entities": entities, "type": source_type, "file": file}
    pkg_cat = textwrap.indent(_c(comment_block("package_checks") + "\n" + comment_block("package_transforms")), "    ")
    sttm_cat = _c("\n".join(comment_block(c) for c in ("attribute_transforms", "attribute_checks", "row_checks", "entity_checks")))
    out = {
        "pipeline.yaml": env.from_string(PIPELINE).render(**ctx),
        "source.yaml": env.from_string(SOURCE_FILE if file else SOURCE_RDS).render(**ctx, package_catalog=pkg_cat),
        "destination.yaml": env.from_string(DESTINATION).render(**ctx),
        "sttm.yaml": env.from_string(STTM).render(**ctx, catalog=sttm_cat),
        "quality.yaml": env.from_string(QUALITY).render(**ctx, catalog=_c(comment_block("lake_checks"))),
        "policy.yaml": env.from_string(POLICY).render(**ctx),
    }
    return out


def new_pipeline(root: Path, name: str, source_type: str, destination_type: str, entities: list[str], envs: list[str], force: bool = False) -> Path:
    import re

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise PfError("pipeline name must be snake_case (^[a-z][a-z0-9_]*$): it becomes the Fivetran connection and schema name", "invalid")
    for e in entities:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", e) or e.startswith("pf_"):
            raise PfError(f"entity name '{e}' must be snake_case and not start with pf_", "invalid")
    for e in envs:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", e):
            raise PfError(f"environment name '{e}' is invalid", "invalid")
    target = root / "pipelines" / name
    if target.exists() and any(target.iterdir()) and not force:
        raise PfError(f"{target} already exists; edit its specs instead (or pass --force to overwrite)", "exists")
    files = render_bundle(name, source_type, destination_type, entities, envs)
    target.mkdir(parents=True, exist_ok=True)
    for fname, text in files.items():
        (target / fname).write_text(text, encoding="utf-8", newline="\n")
    (target / "rules").mkdir(exist_ok=True)
    (target / "rules" / "__init__.py").write_text('"""Reviewed custom checks (python kind). Each function: (data: dict, params: dict) -> list[str] failure reasons."""\n', encoding="utf-8")
    (target / "fixtures").mkdir(exist_ok=True)
    (target / "fixtures" / "README.md").write_text(
        "Synthetic test deliveries for this pipeline (never real data).\n\n"
        "- `good/` deliveries that must load\n- `bad/` deliveries that each break one rule\n", encoding="utf-8")
    return target
