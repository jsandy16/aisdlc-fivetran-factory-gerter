# factory-trigger-<team>: the dispatcher assumes it to start DAG runs as the team's Airflow role only (spike S3).
data "aws_iam_policy_document" "trigger_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.me.account_id}:root"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::${data.aws_caller_identity.me.account_id}:role/factory-${local.team_kebab}-*"]
    }
  }
}

resource "aws_iam_role" "trigger" {
  name               = "factory-trigger-${var.team}"
  assume_role_policy = data.aws_iam_policy_document.trigger_trust.json
}

resource "aws_iam_role_policy" "trigger" {
  role = aws_iam_role.trigger.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["airflow:InvokeRestApi"]
      Resource = ["arn:aws:airflow:${var.region}:${data.aws_caller_identity.me.account_id}:role/${local.mwaa_name}/team_${var.team}"]
    }]
  })
}
