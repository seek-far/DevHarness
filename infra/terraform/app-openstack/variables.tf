variable "kube_config_path" {
  description = "Path to the 1b.3 cluster kubeconfig (a leading ~ is expanded)."
  type        = string
  default     = "~/.kube/sdlcma-1b.config"
}

variable "kube_context" {
  description = "kubeconfig context. kubeadm's default admin context name."
  type        = string
  default     = "kubernetes-admin@kubernetes"
}

variable "namespace" {
  description = "Namespace Terraform owns and installs the release into."
  type        = string
  default     = "sdlcma"
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "sdlcma"
}

variable "gateway_image_tag" {
  description = "Image tag for the gateway Deployment (loaded onto nodes via the kind-load analog; see NOTES §6i)."
  type        = string
  default     = "latest"
}

variable "orchestrator_image_tag" {
  description = "Image tag for the orchestrator Deployment."
  type        = string
  default     = "latest"
}

variable "helm_wait" {
  description = "Kept false (same reason as the 1a root): sdlcma-secrets is an out-of-band prerequisite, so the orchestrator pod is intentionally not Ready until it exists — waiting would hang apply."
  type        = bool
  default     = false
}

variable "helm_timeout" {
  description = "helm_release timeout (seconds) when helm_wait is true."
  type        = number
  default     = 300
}

variable "ingress_enabled" {
  description = "Chart Ingress toggle. Default false for 1b.3: the kubeadm cluster has no ingress-nginx (it was a kind cluster add-on in 1a.6). Smoke test uses port-forward. This is a VALUES override, not a chart edit."
  type        = bool
  default     = false
}
