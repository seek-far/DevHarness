# Same provider blocks as the 1a root — only the kubeconfig differs. That is
# the whole point of 1b.4: the chart/k8s layer is provider-neutral, so moving
# from kind to a real OpenStack kubeadm cluster is a kubeconfig swap, nothing
# else. The kubeconfig is produced out-of-band by k8s-bootstrap/bootstrap.sh
# (1b.3), server already rewritten to the control floating IP.
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
