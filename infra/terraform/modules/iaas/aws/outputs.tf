# IDENTICAL output names to ../openstack/outputs.tf — the locked seam.
# Consumers (k8s-bootstrap, app root) never branch on provider.

output "control_ips" {
  description = "Publicly reachable (Elastic) IPs of control nodes."
  value       = aws_eip.control[*].public_ip
}

output "worker_ips" {
  description = "Private IPs of worker nodes (reachable from control / in-cluster)."
  value       = aws_instance.worker[*].private_ip
}

output "ssh_user" {
  description = "Login user for the chosen image (echoed)."
  value       = var.ssh_user
}

output "network_id" {
  description = "AWS id of the VPC (the private node network)."
  value       = aws_vpc.this.id
}

output "kubeapi_endpoint" {
  description = "Where the kube API server is stood up."
  value       = var.control_count > 0 ? "https://${aws_eip.control[0].public_ip}:6443" : null
}
