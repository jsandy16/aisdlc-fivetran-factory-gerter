variable "sandbox_account_id" {
  type        = string
  description = "The only account this stack may touch (also FACTORY_SANDBOX_ACCOUNT_ID for preflight)"
  validation {
    condition     = can(regex("^[0-9]{12}$", var.sandbox_account_id))
    error_message = "A 12-digit AWS account id."
  }
}

variable "region" {
  type    = string
  default = "eu-west-1"
}

variable "team" {
  type    = string
  default = "clinops_data"
}

variable "env" {
  type    = string
  default = "dev"
}

variable "admin_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to reach the EKS API endpoint (the operator's egress IP)"
}

variable "airflow_version" {
  type        = string
  default     = "3.3.1"
  description = "Must be a version MWAA offers in the region (checked by preflight)"
}

variable "budget_usd" {
  type    = number
  default = 50
}

variable "budget_email" {
  type        = string
  description = "Receives the budget alarm at 50% and 80%"
}

locals {
  team_kebab = replace(var.team, "_", "-")
  prefix     = "factory-${local.team_kebab}-${var.env}-${data.aws_caller_identity.me.account_id}"
  azs        = slice(data.aws_availability_zones.az.names, 0, 2)
}
