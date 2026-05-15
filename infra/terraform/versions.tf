# Stage 1a.5: Terraform orchestrates the 1a.4 Helm chart.
#
# Pinned to the helm provider v2 line (stable `set {}` block syntax). helm
# provider v3 reworked the kubernetes connection block and value setting —
# out of scope for 1a.5; revisit in 1a.6 if we move off kind.
terraform {
  required_version = ">= 1.9"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.33"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
  }
}
