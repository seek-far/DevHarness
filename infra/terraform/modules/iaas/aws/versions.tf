# 1b.5 — AWS implementation of the SAME provider-neutral IaaS contract
# (../CONTRACT.md). This is the phase-2 target: swapping the module `source`
# from openstack/ to aws/ is the entire IaaS change; the k8s-bootstrap script,
# the Helm chart, and the app-* root stay byte-unchanged.
#
# Skeleton: it `terraform validate`s (locks the contract is AWS-satisfiable)
# but is intentionally never applied in phase 1 — no AWS account, no spend.
terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
