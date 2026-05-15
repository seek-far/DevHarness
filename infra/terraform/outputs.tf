output "namespace" {
  description = "Namespace Terraform manages."
  value       = kubernetes_namespace.sdlcma.metadata[0].name
}

output "release_name" {
  description = "Installed Helm release name."
  value       = helm_release.sdlcma.name
}

output "release_status" {
  description = "Helm release status (deployed/failed/…)."
  value       = helm_release.sdlcma.status
}

output "chart_version" {
  description = "Chart version that was installed."
  value       = helm_release.sdlcma.metadata[0].version
}

output "secret_prerequisite" {
  description = "Reminder: the Secret is NOT managed by Terraform."
  value       = "kubectl create secret generic sdlcma-secrets -n ${var.namespace} --from-literal=LLM_API_KEY=... --from-literal=GITLAB_PRIVATE_TOKEN=... --from-file=SSH_PRIVATE_KEY=$HOME/.ssh/id_ed25519"
}
