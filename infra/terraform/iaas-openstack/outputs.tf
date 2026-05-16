# Re-export the contract outputs so 1b.3/1b.4 consume this root, not the module.
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
