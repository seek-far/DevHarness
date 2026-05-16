# Contract outputs (see ../CONTRACT.md). The future aws/ module emits the same
# names so consumers (1b.3 k8s install, 1b.4 Helm) never branch on provider.

output "control_ips" {
  description = "Publicly reachable (floating) IPs of control nodes."
  value       = openstack_networking_floatingip_v2.control[*].address
}

output "worker_ips" {
  description = "Fixed (private) IPs of worker nodes; reachable from control / in-cluster."
  value       = openstack_compute_instance_v2.worker[*].access_ip_v4
}

output "ssh_user" {
  description = "Login user for the chosen image (echoed so consumers don't re-derive)."
  value       = var.ssh_user
}

output "network_id" {
  description = "OpenStack id of the private node network."
  value       = openstack_networking_network_v2.this.id
}

output "kubeapi_endpoint" {
  description = "Where 1b.3 will stand up the kube API server."
  value       = var.control_count > 0 ? "https://${openstack_networking_floatingip_v2.control[0].address}:6443" : null
}
