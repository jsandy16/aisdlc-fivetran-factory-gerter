#!/usr/bin/env bash
# Offline IaC checks (Phase 5): Terraform + a local AWS provider mirror (no registry access needed) + checkov.
# Usage: scripts/install-iac-tools.sh [DIR]   then export FACTORY_IAC_TOOLS=DIR
set -euo pipefail
DIR="${1:-$HOME/.factory-iac-tools}"
TF_VERSION=1.16.3
AWS_PROVIDER_VERSION=6.66.0
mkdir -p "$DIR/bin" "$DIR/plugins/registry.terraform.io/hashicorp/aws"
curl -fsSLo "$DIR/tf.zip" "https://releases.hashicorp.com/terraform/${TF_VERSION}/terraform_${TF_VERSION}_linux_amd64.zip"
python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$DIR/tf.zip" "$DIR/bin"
chmod +x "$DIR/bin/terraform"
curl -fsSLo "$DIR/plugins/registry.terraform.io/hashicorp/aws/terraform-provider-aws_${AWS_PROVIDER_VERSION}_linux_amd64.zip" \
  "https://releases.hashicorp.com/terraform-provider-aws/${AWS_PROVIDER_VERSION}/terraform-provider-aws_${AWS_PROVIDER_VERSION}_linux_amd64.zip"
python3 -m venv "$DIR/ckv"
"$DIR/ckv/bin/pip" install -q "checkov==3.2.533"
echo "export FACTORY_IAC_TOOLS=$DIR"
