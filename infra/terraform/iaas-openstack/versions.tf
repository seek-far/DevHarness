# 1b.2 root: drives ONLY the IaaS layer (bare VMs on DevStack).
#
# Deliberately a separate root from ../  (the 1a helm_release root). The 1a
# state is live (kind + chart deployed). Keeping IaaS in its own root with its
# own state guarantees 1b.2 cannot disturb 1a. The provider-neutral seam is the
# module under ../modules/iaas/; phase 2 swaps openstack -> aws there, this root
# only changes the module `source` + provider block.
terraform {
  required_version = ">= 1.9"

  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "~> 3.0"
    }
  }
}
