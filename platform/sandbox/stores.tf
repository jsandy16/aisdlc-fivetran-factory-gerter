# Team stores and keys, named as the platform settings expect (aliases are per account; bucket names carry the account).
locals {
  keys    = { staging = "alias/factory-${local.team_kebab}-staging", audit = "alias/factory-${local.team_kebab}-audit", platform = "alias/factory-sandbox-platform" }
  buckets = { staging = "staging", bundles = "platform", audit = "audit", dags = "platform", landing = "staging", delivery = "staging" } # bucket => key
}

data "aws_iam_policy_document" "key" {
  statement {
    sid       = "AccountAdministersAndIamDelegates"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.me.account_id}:root"]
    }
  }
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_kms_key" "k" {
  for_each                = local.keys
  description             = "factory sandbox ${each.key}"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.key.json
}

resource "aws_kms_alias" "k" {
  for_each      = local.keys
  name          = each.value
  target_key_id = aws_kms_key.k[each.key].key_id
}

resource "aws_s3_bucket" "store" {
  for_each            = local.buckets
  bucket              = "${local.prefix}-${each.key}"
  force_destroy       = true # sandbox: teardown must not leave buckets behind
  object_lock_enabled = each.key == "audit"
}

resource "aws_s3_bucket_versioning" "store" {
  for_each = aws_s3_bucket.store
  bucket   = each.value.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.store["audit"].id
  rule {
    default_retention {
      mode = "GOVERNANCE" # sandbox: a privileged teardown can still remove it; COMPLIANCE in real stages
      days = 1
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "store" {
  for_each = aws_s3_bucket.store
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.k[local.buckets[each.key]].arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "store" {
  for_each                = aws_s3_bucket.store
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "store" {
  for_each = { for k, v in aws_s3_bucket.store : k => v if k != "audit" }
  bucket   = each.value.id
  rule {
    id     = "sandbox-expiry"
    status = "Enabled"
    filter {}
    expiration { days = 7 }
    noncurrent_version_expiration { noncurrent_days = 1 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

resource "aws_dynamodb_table" "run_state" {
  name         = "${local.prefix}-run-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.k["staging"].arn
  }
}

resource "aws_ecr_repository" "runtime" {
  name                 = "factory-runtime"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.k["platform"].arn
  }
}
