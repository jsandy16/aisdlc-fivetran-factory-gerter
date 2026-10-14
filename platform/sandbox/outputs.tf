# Everything `python -m factory_testing.sandbox settings` needs to render real platform settings for this account.
output "settings" {
  value = {
    account_id         = data.aws_caller_identity.me.account_id
    region             = var.region
    team               = var.team
    env                = var.env
    staging_bucket     = aws_s3_bucket.store["staging"].id
    bundle_store       = aws_s3_bucket.store["bundles"].id
    audit_store        = aws_s3_bucket.store["audit"].id
    run_state_table    = aws_dynamodb_table.run_state.name
    landing_bucket     = aws_s3_bucket.store["landing"].id
    delivery_bucket    = aws_s3_bucket.store["delivery"].id
    dags_bucket        = aws_s3_bucket.store["dags"].id
    kms_aliases        = { for k, a in aws_kms_alias.k : k => a.name }
    kms_key_arn        = aws_kms_key.k["staging"].arn
    mwaa_environment   = aws_mwaa_environment.main.name
    trigger_role       = aws_iam_role.trigger.name
    eks_cluster        = aws_eks_cluster.main.name
    subnet_ids         = aws_subnet.private[*].id
    security_group_id  = aws_security_group.workload.id
    ecr_repository_url = aws_ecr_repository.runtime.repository_url
  }
}
