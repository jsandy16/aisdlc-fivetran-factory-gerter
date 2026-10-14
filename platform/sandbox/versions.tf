# Sandbox platform stack (Phase 8): the prerequisites a generated pipeline stack expects, in ONE sandbox account.
# The provider refuses any other account (allowed_account_ids). Apply, validate, tear down the same day.
terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = { source = "hashicorp/aws", version = "6.66.0" }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.sandbox_account_id]
  default_tags {
    tags = { "factory:stack" = "sandbox", "factory:team" = var.team, "factory:ttl" = "same-day" }
  }
}

data "aws_caller_identity" "me" {}
data "aws_availability_zones" "az" { state = "available" }
