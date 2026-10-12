"""Governance outside Fivetran, as Terraform per environment (hashicorp/aws ~> 6.68):

  access.tf        Lake Formation tags on the pipeline's Glue database + tag-based grants from policy.yaml.
                   Apply BEFORE the first sync, so no table is ever readable before its grants exist.
  audit.tf         write-once evidence bucket (Object Lock, versioning, KMS, public access blocked) and
                   CloudTrail data events on the pipeline's lake prefix.
  originals.tf     write-once bucket for original files (file sources with archive_originals).
  columns/         column-level classification tags; tables only exist after the first sync, so this is a
                   second, separate apply.
Account-wide settings (aws_lakeformation_data_lake_settings) are deliberately NOT generated: they belong to
the platform's foundation stack and would overwrite shared state.
"""
from __future__ import annotations

from ..bundle import Bundle

LEVELS = ["public", "internal", "confidential", "restricted"]


def _id(s: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_]", "_", s)


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


def access(b: Bundle, env: str) -> str:
    assert b.policy and b.destination
    db = b.name
    pol = b.policy
    out = [f'''# Lake Formation tag-based access for Glue database "{db}" ({env}).
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
  values = ["{db}"]
}}

# Tables inherit database tags; uncertified until the post-load checks pass (`pf dq run --certify`).
resource "aws_lakeformation_resource_lf_tags" "database" {{
  database {{
    name = "{db}"
  }}
  lf_tag {{
    key   = aws_lakeformation_lf_tag.classification.key
    value = "{pol.default_classification}"
  }}
  lf_tag {{
    key   = aws_lakeformation_lf_tag.certified.key
    value = "false"
  }}
  lf_tag {{
    key   = aws_lakeformation_lf_tag.pipeline.key
    value = "{db}"
  }}
}}
''']
    for i, g in enumerate(pol.environments[env].access):
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


def columns(b: Bundle, env: str) -> str:
    assert b.policy and b.sttm
    out = [f'# Column classification for "{b.name}" ({env}). Apply after the first sync (tables must exist).\n']
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


def _bucket(res: str, name: str, mode: str, days: int, purpose: str) -> str:
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
      sse_algorithm = "aws:kms"
    }}
    bucket_key_enabled = true
  }}
}}
'''


def audit(b: Bundle, env: str) -> str:
    assert b.policy and b.destination
    ap = b.policy.environments[env].audit
    de = b.destination.destination.environments[env]
    days = b.policy.retention.object_lock_days
    out = [_bucket("audit", ap.bucket, ap.object_lock_mode, days, f"Write-once evidence for {b.name} ({env}): DQ results, release records, CloudTrail.")]
    if ap.cloudtrail_data_events:
        out.append(f'''data "aws_caller_identity" "current" {{}}
data "aws_partition" "current" {{}}
data "aws_region" "current" {{}}

resource "aws_s3_bucket_policy" "audit" {{
  bucket = aws_s3_bucket.audit.id
  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
      {{
        Sid       = "CloudTrailAclCheck"
        Effect    = "Allow"
        Principal = {{ Service = "cloudtrail.amazonaws.com" }}
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.audit.arn
      }},
      {{
        Sid       = "CloudTrailWrite"
        Effect    = "Allow"
        Principal = {{ Service = "cloudtrail.amazonaws.com" }}
        Action    = "s3:PutObject"
        Resource  = "${{aws_s3_bucket.audit.arn}}/cloudtrail/AWSLogs/${{data.aws_caller_identity.current.account_id}}/*"
        Condition = {{ StringEquals = {{ "s3:x-amz-acl" = "bucket-owner-full-control" }} }}
      }},
      {{
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.audit.arn, "${{aws_s3_bucket.audit.arn}}/*"]
        Condition = {{ Bool = {{ "aws:SecureTransport" = "false" }} }}
      }}
    ]
  }})
}}

# Who read or wrote the pipeline's lake data (S3 object-level events on its prefix).
resource "aws_cloudtrail" "lake_data" {{
  name           = "pf-{b.name.replace('_', '-')}-{env}-data-events"
  s3_bucket_name = aws_s3_bucket.audit.id
  s3_key_prefix  = "cloudtrail"
  advanced_event_selector {{
    name = "Lake objects of {b.name}"
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
      starts_with = ["arn:${{data.aws_partition.current.partition}}:s3:::{de.bucket}/{de.prefix_path}/{b.name}/"]
    }}
  }}
  depends_on = [aws_s3_bucket_policy.audit]
}}
''')
    return "\n".join(out)


def originals(b: Bundle, env: str) -> str | None:
    assert b.policy
    o = b.policy.environments[env].originals
    if not (b.policy.archive_originals and b.is_file_source and o):
        return None
    mode = b.policy.environments[env].audit.object_lock_mode
    return _bucket("originals", o.bucket, mode, b.policy.retention.object_lock_days,
                   f"Original files exactly as received for {b.name} ({env}); written once by the connector (If-None-Match).")


def _list(items) -> str:
    return "[" + ", ".join(f'"{x}"' for x in items) + "]"


def render(b: Bundle, lock: dict, header: str) -> dict[str, str]:
    assert b.pipeline
    out: dict[str, str] = {}
    for env in b.pipeline.environments:
        out[f"{env}/versions.tf"] = header + versions()
        out[f"{env}/access.tf"] = header + access(b, env)
        out[f"{env}/audit.tf"] = header + audit(b, env)
        o = originals(b, env)
        if o:
            out[f"{env}/originals.tf"] = header + o
        out[f"{env}/columns/versions.tf"] = header + versions()
        out[f"{env}/columns/columns.tf"] = header + columns(b, env)
    return out
