# MWAA with the factory operators installed from plugins.zip (scripts/sandbox/package-mwaa.sh builds it).
variable "mwaa_plugins_zip" {
  type        = string
  description = "Local path of plugins.zip holding the factory_airflow wheel"
}

variable "mwaa_environment_class" {
  type    = string
  default = "mw1.small"
}

locals {
  mwaa_name = "factory-${var.env}"
}

resource "aws_s3_object" "plugins" {
  bucket = aws_s3_bucket.store["dags"].id
  key    = "plugins.zip"
  source = var.mwaa_plugins_zip
  etag   = filemd5(var.mwaa_plugins_zip)
}

resource "aws_s3_object" "requirements" {
  bucket  = aws_s3_bucket.store["dags"].id
  key     = "requirements.txt"
  content = "/usr/local/airflow/plugins/factory_airflow-0.2.0.dev0-py3-none-any.whl\n"
}

data "aws_iam_policy_document" "mwaa_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["airflow.amazonaws.com", "airflow-env.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mwaa" {
  name               = "${local.mwaa_name}-execution"
  assume_role_policy = data.aws_iam_policy_document.mwaa_trust.json
}

data "aws_iam_policy_document" "mwaa" {
  statement {
    actions   = ["airflow:PublishMetrics"]
    resources = ["arn:aws:airflow:${var.region}:${data.aws_caller_identity.me.account_id}:environment/${local.mwaa_name}"]
  }
  statement {
    actions   = ["s3:GetObject*", "s3:GetBucket*", "s3:List*"]
    resources = [aws_s3_bucket.store["dags"].arn, "${aws_s3_bucket.store["dags"].arn}/*"]
  }
  statement {
    actions   = ["s3:GetAccountPublicAccessBlock"]
    resources = ["*"] # account-level read; no resource form exists
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:CreateLogGroup", "logs:PutLogEvents", "logs:GetLogEvents", "logs:GetLogRecord", "logs:GetLogGroupFields", "logs:GetQueryResults", "logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${var.region}:${data.aws_caller_identity.me.account_id}:log-group:airflow-${local.mwaa_name}-*"]
  }
  statement {
    actions   = ["sqs:ChangeMessageVisibility", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:ReceiveMessage", "sqs:SendMessage"]
    resources = ["arn:aws:sqs:${var.region}:*:airflow-celery-*"] # MWAA's own Celery queues live in a service account
  }
  statement {
    actions   = ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey*", "kms:Encrypt"]
    resources = [aws_kms_key.k["platform"].arn]
  }
  statement {
    sid       = "InvokeFactoryTasks"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${var.region}:${data.aws_caller_identity.me.account_id}:function:factory-${local.team_kebab}-*"]
  }
  statement {
    sid       = "ReachTheTeamCluster"
    actions   = ["eks:DescribeCluster"]
    resources = [aws_eks_cluster.main.arn]
  }
  statement {
    sid       = "SecretsBackendS9"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.me.account_id}:secret:airflow/*"]
  }
}

resource "aws_iam_role_policy" "mwaa" {
  role   = aws_iam_role.mwaa.id
  policy = data.aws_iam_policy_document.mwaa.json
}

resource "aws_mwaa_environment" "main" {
  name                  = local.mwaa_name
  airflow_version       = var.airflow_version
  environment_class     = var.mwaa_environment_class
  execution_role_arn    = aws_iam_role.mwaa.arn
  source_bucket_arn     = aws_s3_bucket.store["dags"].arn
  dag_s3_path           = "dags/"
  plugins_s3_path       = aws_s3_object.plugins.key
  requirements_s3_path  = aws_s3_object.requirements.key
  kms_key               = aws_kms_key.k["platform"].arn
  webserver_access_mode = "PUBLIC_ONLY" # IAM-authenticated UI; sandbox has no VPN
  max_workers           = 2
  airflow_configuration_options = {
    "secrets.backend"        = "airflow.providers.amazon.aws.secrets.secrets_manager.SecretsManagerBackend"
    "secrets.backend_kwargs" = jsonencode({ connections_prefix = "airflow/connections", variables_prefix = "airflow/variables" })
  }
  network_configuration {
    security_group_ids = [aws_security_group.workload.id]
    subnet_ids         = aws_subnet.private[*].id
  }
  logging_configuration {
    dag_processing_logs {
      enabled   = true
      log_level = "INFO"
    }
    scheduler_logs {
      enabled   = true
      log_level = "INFO"
    }
    task_logs {
      enabled   = true
      log_level = "INFO"
    }
    webserver_logs {
      enabled   = true
      log_level = "INFO"
    }
    worker_logs {
      enabled   = true
      log_level = "INFO"
    }
  }
}
