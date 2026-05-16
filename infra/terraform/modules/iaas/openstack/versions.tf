# 1b.2: OpenStack implementation of the provider-neutral IaaS contract
# (see ../CONTRACT.md). Pinned to the openstack provider v3 line.
terraform {
  required_version = ">= 1.9"

  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "~> 3.0"
    }
  }
}
