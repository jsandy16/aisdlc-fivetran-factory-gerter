"""Governance outside Fivetran, as Terraform (hashicorp/aws ~> 6.68), in two layers.

foundation/<env>/   (repository level, aggregated over every pipeline; apply first)
  lake_formation.tf  LF-tags pf_classification / pf_certified / pf_pipeline (one definition per account), the lake
                     data locations registered with Lake Formation (hybrid access off), and DATA_LOCATION_ACCESS
                     for each Fivetran role.
  buckets.tf         write-once audit and originals buckets (Object Lock, versioning, KMS, TLS-only, no public access).
  trail.tf           CloudTrail with log-file validation: data events on every pipeline's lake prefix and, unless an
                     organization trail covers them, management events (Lake Formation grants and tags, Glue changes).
pipelines/<p>/build/terraform/<env>/   (per pipeline)
  access.tf          the pipeline's Glue database, created before the first sync with its LF-tags (uncertified);
                     Fivetran's database permissions; consumers' tag-based grants (certified tables only); the
                     DQ runner's grants (read uncertified, associate pf_certified).
  originals_writer.tf least-privilege IAM policy for the connector's originals credentials (put + list, never delete).
  columns/           column classification tags; tables only exist after the first sync, so a second apply.

Prerequisite outside this code (platform foundation): Lake Formation data lake settings with no IAMAllowedPrincipals
defaults, and no consumer IAM access to the lake prefix; `pf dq run --certify` refuses to certify otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..bundle import Bundle

LEVELS = ["public", "internal", "confidential", "restricted"]


def _id(s: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def _list(items) -> str:
    return "[" + ", ".join(f'"{x}"' for x in items) + "]"


def versions() -> str:
    return '''terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.68"
    }
  }
}
'''


# ---------------------------------------------------------------------------------------------- per pipeline
def access(b: Bundle, env: str) -> str:
    assert b.policy and b.destination
    db = b.name
    pol = b.policy
    de = b.destination.destination.environments[env]
    ep = pol.environments[env]
    out = [f'''# Glue database "{db}" ({env}): created here, before the first sync, so tables land under its tags and grants.
# Requires foundation/{env} (LF-tags, registered data location) to be applied first.
resource "aws_glue_catalog_database" "this" {{
  name         = "{db}"
  location_uri = "s3://{de.bucket}/{de.prefix_path}/{db}"
  description  = "pipeline-factory: {db}"
}}

# Tables inherit database tags; uncertified until the post-load checks pass (`pf dq run --certify`).
resource "aws_lakeformation_resource_lf_tags" "database" {{
  database {{
    name = aws_glue_catalog_database.this.name
  }}
  lf_tag {{
    key   = "pf_classification"
    value = "{pol.default_classification}"
  }}
  lf_tag {{
    key   = "pf_certified"
    value = "false"
  }}
  lf_tag {{
    key   = "pf_pipeline"
    value = "{db}"
  }}
}}

# Fivetran creates and maintains the tables (Managed Data Lake); data location access is granted in foundation.
resource "aws_lakeformation_permissions" "fivetran_database" {{
  principal   = "{de.fivetran_role_arn}"
  permissions = ["CREATE_TABLE", "ALTER", "DROP", "DESCRIBE"]
  database {{
    name = aws_glue_catalog_database.this.name
  }}
}}

resource "aws_lakeformation_permissions" "fivetran_tables" {{
  principal   = "{de.fivetran_role_arn}"
  permissions = ["ALL"]
  table {{
    database_name = aws_glue_catalog_database.this.name
    wildcard      = true
  }}
}}
''']
    if ep.dq_runner_principal:
        out.append(f'''# The DQ runner reads every table of the pipeline (certified or not) and sets pf_certified.
resource "aws_lakeformation_permissions" "dq_runner_tables" {{
  principal   = "{ep.dq_runner_principal}"
  permissions = ["SELECT", "DESCRIBE", "ALTER"]
  lf_tag_policy {{
    resource_type = "TABLE"
    expression {{
      key    = "pf_pipeline"
      values = ["{db}"]
    }}
  }}
  depends_on = [aws_lakeformation_resource_lf_tags.database]
}}

resource "aws_lakeformation_permissions" "dq_runner_certify" {{
  principal   = "{ep.dq_runner_principal}"
  permissions = ["ASSOCIATE", "DESCRIBE"]
  lf_tag {{
    key    = "pf_certified"
    values = ["true", "false"]
  }}
}}
''')
    for i, g in enumerate(ep.access):
        name = f"grant_{i}_{_id(g.principal.split('/')[-1])}"
        out.append(f'''# {g.justification}
resource "aws_lakeformation_permissions" "{name}_tables" {{
  principal   = "{g.principal}"
  permissions = {_list(sorted(g.permissions))}
  lf_tag_policy {{
    resource_type = "TABLE"
    expression {{
      key    = "pf_pipeline"
      values = ["{db}"]
    }}
    expression {{
      key    = "pf_classification"
      values = {_list(sorted(g.classifications, key=LEVELS.index))}
    }}
    expression {{
      key    = "pf_certified"
      values = ["true"]
    }}
  }}
  depends_on = [aws_lakeformation_resource_lf_tags.database]
}}

resource "aws_lakeformation_permissions" "{name}_database" {{
  principal   = "{g.principal}"
  permissions = ["DESCRIBE"]
  lf_tag_policy {{
    resource_type = "DATABASE"
    expression {{
      key    = "pf_pipeline"
      values = ["{db}"]
    }}
  }}
  depends_on = [aws_lakeformation_resource_lf_tags.database]
}}
''')
    return "\n".join(out)


def originals_writer(b: Bundle, env: str) -> tuple[str, str] | None:
    assert b.policy
    o = b.policy.environments[env].originals
    if not (b.policy.archive_originals and b.is_file_source and o):
        return None
    base = f"{o.prefix.strip('/')}/{b.name}"
    doc = {"Version": "2012-10-17", "Statement": [
        {"Sid": "WriteOriginalsOnce", "Effect": "Allow", "Action": ["s3:PutObject"], "Resource": f"arn:aws:s3:::{o.bucket}/{base}/*"},
        {"Sid": "ReadLoadedRegistry", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{o.bucket}/{base}/_loaded/*"},
        {"Sid": "ListOwnPrefix", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{o.bucket}",
         "Condition": {"StringLike": {"s3:prefix": [f"{base}/*"]}}},
        {"Sid": "NeverDeleteOrBypass", "Effect": "Deny", "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention",
                                                                     "s3:PutObjectRetention", "s3:PutObjectLegalHold"],
         "Resource": f"arn:aws:s3:::{o.bucket}/*"}]}
    if o.kms_key_arn:
        doc["Statement"].append({"Sid": "UseBucketKey", "Effect": "Allow", "Action": ["kms:GenerateDataKey", "kms:Decrypt"], "Resource": o.kms_key_arn})
    tf = f'''# Least privilege for the connector's originals credentials ({env}): attach to the IAM user or role whose keys are
# stored as Fivetran configuration secrets. Put and list its own prefix, read its registry; never delete.
resource "aws_iam_policy" "originals_writer" {{
  name   = "pf-{b.name.replace('_', '-')}-{env}-originals-writer"
  policy = file("${{path.module}}/originals-writer-policy.json")
}}
'''
    return tf, json.dumps(doc, indent=2, sort_keys=True) + "\n"


def columns(b: Bundle, env: str) -> str:
    assert b.policy and b.sttm
    out = [f'# Column classification for "{b.name}" ({env}). Apply after the first sync (tables must exist).\n'
           f'# `pf dq run --certify` refuses to certify a table until these tags are in place.\n']
    by: dict[tuple[str, str], list[str]] = {}
    for key, level in sorted(b.policy.classification.items()):
        ent, attr = key.split(".")
        if level != b.policy.default_classification:
            by.setdefault((ent, level), []).append(attr)
    for (ent, level), attrs in sorted(by.items()):
        out.append(f'''resource "aws_lakeformation_resource_lf_tags" "{_id(ent)}_{level}" {{
  table_with_columns {{
    database_name = "{b.name}"
    name          = "{ent}"
    column_names  = {_list(sorted(attrs))}
  }}
  lf_tag {{
    key   = "pf_classification"
    value = "{level}"
  }}
}}
''')
    if len(out) == 1:
        out.append("# Every column uses the database default classification; nothing to tag.\n")
    return "\n".join(out)


def render(b: Bundle, lock: dict, header: str) -> dict[str, str]:
    assert b.pipeline
    out: dict[str, str] = {}
    for env in b.pipeline.environments:
        out[f"{env}/versions.tf"] = header + versions()
        out[f"{env}/access.tf"] = header + access(b, env)
        w = originals_writer(b, env)
        if w:
            out[f"{env}/originals_writer.tf"] = header + w[0]
            out[f"{env}/originals-writer-policy.json"] = w[1]
        out[f"{env}/columns/versions.tf"] = header + versions()
        out[f"{env}/columns/columns.tf"] = header + columns(b, env)
    return out


# ---------------------------------------------------------------------------------------------- foundation
def _bucket(res: str, name: str, mode: str, days: int, kms: str | None, purpose: str, extra_statements: str = "") -> str:
    kms_line = f'\n      kms_master_key_id = "{kms}"' if kms else ""
    return f'''# {purpose}
resource "aws_s3_bucket" "{res}" {{
  bucket              = "{name}"
  object_lock_enabled = true
}}

resource "aws_s3_bucket_versioning" "{res}" {{
  bucket = aws_s3_bucket.{res}.id
  versioning_configuration {{
    status = "Enabled"
  }}
}}

resource "aws_s3_bucket_object_lock_configuration" "{res}" {{
  bucket = aws_s3_bucket.{res}.id
  rule {{
    default_retention {{
      mode = "{mode}"
      days = {days}
    }}
  }}
  depends_on = [aws_s3_bucket_versioning.{res}]
}}

resource "aws_s3_bucket_public_access_block" "{res}" {{
  bucket                  = aws_s3_bucket.{res}.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}

resource "aws_s3_bucket_server_side_encryption_configuration" "{res}" {{
  bucket = aws_s3_bucket.{res}.id
  rule {{
    apply_server_side_encryption_by_default {{
      sse_algorithm = "aws:kms"{kms_line}
    }}
    bucket_key_enabled = true
  }}
}}

resource "aws_s3_bucket_policy" "{res}" {{
  bucket = aws_s3_bucket.{res}.id
  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{extra_statements}
      {{
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.{res}.arn, "${{aws_s3_bucket.{res}.arn}}/*"]
        Condition = {{ Bool = {{ "aws:SecureTransport" = "false" }} }}
      }}
    ]
  }})
}}
'''


def _strictest(modes: list[str]) -> str:
    return "COMPLIANCE" if "COMPLIANCE" in modes else "GOVERNANCE"


def foundation(bundles: list[Bundle]) -> dict[str, str]:
    """Account-level objects for every environment, aggregated over all pipelines of the repository."""
    envs = sorted({e for b in bundles if b.pipeline for e in b.pipeline.environments})
    out: dict[str, str] = {}
    for env in envs:
        bs = [b for b in bundles if b.pipeline and env in b.pipeline.environments and b.policy and b.destination]
        names = sorted(b.name for b in bs)
        locations: dict[tuple[str, str], set[str]] = {}
        audits: dict[str, dict] = {}
        originals: dict[str, dict] = {}
        for b in bs:
            de = b.destination.destination.environments[env]
            locations.setdefault((de.bucket, de.prefix_path), set()).add(de.fivetran_role_arn)
            ap = b.policy.environments[env].audit
            a = audits.setdefault(ap.bucket, {"modes": [], "days": 0, "kms": None, "mgmt": False, "data": False, "prefixes": []})
            a["modes"].append(ap.object_lock_mode)
            a["days"] = max(a["days"], b.policy.retention.object_lock_days)
            a["kms"] = a["kms"] or ap.kms_key_arn
            a["mgmt"] = a["mgmt"] or ap.cloudtrail_management_events
            if ap.cloudtrail_data_events:
                a["data"] = True
                a["prefixes"].append(f"{de.bucket}/{de.prefix_path}/{b.name}/")
            o = b.policy.environments[env].originals
            if b.policy.archive_originals and b.is_file_source and o:
                oo = originals.setdefault(o.bucket, {"modes": [], "days": 0, "kms": None})
                oo["modes"].append(ap.object_lock_mode)
                oo["days"] = max(oo["days"], b.policy.retention.object_lock_days)
                oo["kms"] = oo["kms"] or o.kms_key_arn
        lf = [f'''# Lake Formation for {env}: one definition per account, shared by every pipeline ({", ".join(names)}).
resource "aws_lakeformation_lf_tag" "classification" {{
  key    = "pf_classification"
  values = {_list(LEVELS)}
}}

resource "aws_lakeformation_lf_tag" "certified" {{
  key    = "pf_certified"
  values = ["true", "false"]
}}

resource "aws_lakeformation_lf_tag" "pipeline" {{
  key    = "pf_pipeline"
  values = {_list(names)}
}}
''']
        for (bucket, prefix), roles in sorted(locations.items()):
            res = _id(f"{bucket}_{prefix}")
            lf.append(f'''# Lake data written by Fivetran: governed by Lake Formation only (no hybrid IAM access).
resource "aws_lakeformation_resource" "{res}" {{
  arn                     = "arn:aws:s3:::{bucket}/{prefix}"
  use_service_linked_role = true
  hybrid_access_enabled   = false
}}
''')
            for role in sorted(roles):
                lf.append(f'''resource "aws_lakeformation_permissions" "{res}_{_id(role.split('/')[-1])}" {{
  principal   = "{role}"
  permissions = ["DATA_LOCATION_ACCESS"]
  data_location {{
    arn = aws_lakeformation_resource.{res}.arn
  }}
}}
''')
        buckets, trails = [], ['data "aws_caller_identity" "current" {}\ndata "aws_partition" "current" {}\ndata "aws_region" "current" {}\n']
        for name, a in sorted(audits.items()):
            res = "audit_" + _id(name)
            trail = f"pf-audit-{env}-{_id(name).replace('_', '-')}"[:128]
            arn = f"arn:${{data.aws_partition.current.partition}}:cloudtrail:${{data.aws_region.current.region}}:${{data.aws_caller_identity.current.account_id}}:trail/{trail}"
            ct = f'''
      {{
        Sid       = "CloudTrailAclCheck"
        Effect    = "Allow"
        Principal = {{ Service = "cloudtrail.amazonaws.com" }}
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.{res}.arn
        Condition = {{ StringEquals = {{ "aws:SourceArn" = "{arn}" }} }}
      }},
      {{
        Sid       = "CloudTrailWrite"
        Effect    = "Allow"
        Principal = {{ Service = "cloudtrail.amazonaws.com" }}
        Action    = "s3:PutObject"
        Resource  = "${{aws_s3_bucket.{res}.arn}}/cloudtrail/AWSLogs/${{data.aws_caller_identity.current.account_id}}/*"
        Condition = {{ StringEquals = {{ "s3:x-amz-acl" = "bucket-owner-full-control", "aws:SourceArn" = "{arn}" }} }}
      }},''' if (a["data"] or a["mgmt"]) else ""
            buckets.append(_bucket(res, name, _strictest(a["modes"]), a["days"], a["kms"],
                                   f"Write-once evidence ({env}): DQ results and certification records, CloudTrail logs.", ct))
            if a["data"] or a["mgmt"]:
                selectors = []
                if a["mgmt"]:
                    selectors.append('''  advanced_event_selector {
    name = "Management events: Lake Formation grants and tags, Glue changes"
    field_selector {
      field  = "eventCategory"
      equals = ["Management"]
    }
  }''')
                if a["data"]:
                    starts = ", ".join(f'"arn:${{data.aws_partition.current.partition}}:s3:::{p}"' for p in sorted(a["prefixes"]))
                    selectors.append(f'''  advanced_event_selector {{
    name = "Lake objects of the pipelines"
    field_selector {{
      field  = "eventCategory"
      equals = ["Data"]
    }}
    field_selector {{
      field  = "resources.type"
      equals = ["AWS::S3::Object"]
    }}
    field_selector {{
      field       = "resources.ARN"
      starts_with = [{starts}]
    }}
  }}''')
                kms = f'\n  kms_key_id                    = "{a["kms"]}"' if a["kms"] else ""
                trails.append(f'''resource "aws_cloudtrail" "{res}" {{
  name                          = "{trail}"
  s3_bucket_name                = aws_s3_bucket.{res}.id
  s3_key_prefix                 = "cloudtrail"
  enable_log_file_validation    = true
  is_multi_region_trail         = true
  include_global_service_events = true{kms}
{chr(10).join(selectors)}
  depends_on = [aws_s3_bucket_policy.{res}]
}}
''')
        for name, o in sorted(originals.items()):
            buckets.append(_bucket("originals_" + _id(name), name, _strictest(o["modes"]), o["days"], o["kms"],
                                   f"Original files exactly as received ({env}); written once by the connectors (If-None-Match)."))
        out[f"{env}/versions.tf"] = versions()
        out[f"{env}/lake_formation.tf"] = "\n".join(lf)
        out[f"{env}/buckets.tf"] = "\n".join(buckets)
        out[f"{env}/trail.tf"] = "\n".join(trails)
    return out


def render_foundation(root: Path, header: str) -> dict[str, bytes]:
    from ..bundle import load_bundle

    bundles = []
    for d in sorted((root / "pipelines").iterdir()) if (root / "pipelines").is_dir() else []:
        if (d / "pipeline.yaml").exists():
            b = load_bundle(d)
            if b.ok:
                bundles.append(b)
    return {f"foundation/{k}": (header + v).encode() for k, v in foundation(bundles).items()}
