# 1b.5 — phase-2 target root. Wires the aws IaaS module. Its ONLY purpose in
# phase 1 is `terraform validate`: it locks, in CI, that the contract is
# AWS-satisfiable and that going to AWS is a module-source swap — nothing in
# k8s-bootstrap / the Helm chart / the app root changes. NEVER applied here
# (no AWS account, no spend); apply belongs to phase 2.
terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
