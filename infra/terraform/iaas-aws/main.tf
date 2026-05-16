# The phase-2 swap, in one line: source = "../modules/iaas/aws" instead of
# ".../openstack". Every argument below is byte-identical to the openstack
# root's module call — that is the whole point of the locked contract.
module "iaas" {
  source = "../modules/iaas/aws"

  name_prefix      = var.name_prefix
  control_count    = var.control_count
  worker_count     = var.worker_count
  control_flavor   = var.control_flavor
  worker_flavor    = var.worker_flavor
  image_name       = var.image_name
  ssh_public_key   = var.ssh_public_key
  ssh_user         = var.ssh_user
  network_cidr     = var.network_cidr
  external_network = var.external_network
  cloud_init       = var.cloud_init_file != "" ? file("${path.root}/${var.cloud_init_file}") : var.cloud_init
}
