# 1b.4 root: deploys the UNCHANGED 1a.4 Helm chart onto the 1b.3 OpenStack
# kubeadm cluster. Separate root + state from both the 1a helm root (kind) and
# the 1b iaas-openstack root — same isolation rule as 1b.2 (§6g.2#1): never
# let a 1b apply touch the live 1a state.
#
# Provider pins match the 1a root on purpose: proving the chart is
# provider-neutral means changing *nothing* but the kubeconfig it targets.
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
