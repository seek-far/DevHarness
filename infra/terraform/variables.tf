variable "kube_config_path" {
  description = "Path to the kubeconfig (a leading ~ is expanded)."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "kubeconfig context to target."
  type        = string
  default     = "kind-sdlcma-dev"
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
  description = "Image tag for the gateway Deployment. Override with a git short SHA to defeat the :latest rolling-update cache trap (NOTES §9)."
  type        = string
  default     = "latest"
}

variable "orchestrator_image_tag" {
  description = "Image tag for the orchestrator Deployment."
  type        = string
  default     = "latest"
}

variable "helm_wait" {
  description = "If true, helm_release blocks until all objects are Ready. Kept false by default: sdlcma-secrets is an out-of-band prerequisite, so the orchestrator pod is intentionally not Ready until the operator creates it — waiting would hang apply."
  type        = bool
  default     = false
}

variable "helm_timeout" {
  description = "helm_release timeout (seconds) when helm_wait is true."
  type        = number
  default     = 300
}
