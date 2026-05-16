# Identical re-export to the openstack root. Consumers are provider-blind.
output "control_ips" {
  value = module.iaas.control_ips
}

output "worker_ips" {
  value = module.iaas.worker_ips
}

output "ssh_user" {
  value = module.iaas.ssh_user
}

output "network_id" {
  value = module.iaas.network_id
}

output "kubeapi_endpoint" {
  value = module.iaas.kubeapi_endpoint
}
