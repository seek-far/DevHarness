# Both providers target the same kind cluster via the local kubeconfig +
# context. pathexpand() resolves a leading ~ (Terraform does not expand it).
provider "kubernetes" {
  config_path    = pathexpand(var.kube_config_path)
  config_context = var.kube_context
}

provider "helm" {
  kubernetes {
    config_path    = pathexpand(var.kube_config_path)
    config_context = var.kube_context
  }
}
