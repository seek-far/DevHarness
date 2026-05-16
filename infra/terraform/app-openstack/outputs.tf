output "namespace" {
  value = kubernetes_namespace.sdlcma.metadata[0].name
}

output "release_name" {
  value = helm_release.sdlcma.name
}

output "release_status" {
  value = helm_release.sdlcma.status
}

output "chart_version" {
  value = helm_release.sdlcma.version
}

output "secret_prerequisite" {
  description = "Out-of-band step (Terraform never manages the Secret)."
  value       = "kubectl --kubeconfig ${var.kube_config_path} -n ${var.namespace} create secret generic sdlcma-secrets ..."
}
