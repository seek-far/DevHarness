locals {
  # The SAME chart the 1a root installs (infra/helm/sdlcma), byte-for-byte.
  # This root is one level deeper than the 1a root, hence ../../ vs ../.
  chart_path = "${path.module}/../../helm/sdlcma"
}

# Terraform owns the namespace (clean create/destroy), chart installed with
# namespace.create=false — identical contract to the 1a root.
#
# Trade-off (same as 1a): `terraform destroy` deletes this namespace and
# cascades the out-of-band sdlcma-secrets Secret. Acceptable — the Secret is
# recreated by the documented kubectl prerequisite on the next apply, and
# Terraform never reads/stores it.
resource "kubernetes_namespace" "sdlcma" {
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/part-of"    = "sdlcma"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

resource "helm_release" "sdlcma" {
  name      = var.release_name
  chart     = local.chart_path
  namespace = kubernetes_namespace.sdlcma.metadata[0].name

  create_namespace = false
  wait             = var.helm_wait
  timeout          = var.helm_timeout
  recreate_pods    = false

  set {
    name  = "namespace.create"
    value = "false"
  }

  set {
    name  = "gateway.image.tag"
    value = var.gateway_image_tag
  }

  set {
    name  = "orchestrator.image.tag"
    value = var.orchestrator_image_tag
  }

  # Environment override, NOT a chart change: the kubeadm cluster has no
  # ingress-nginx (that was a kind add-on in 1a.6). The chart's own toggle.
  set {
    name  = "ingress.enabled"
    value = tostring(var.ingress_enabled)
  }

  depends_on = [kubernetes_namespace.sdlcma]
}
