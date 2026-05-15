locals {
  # Chart lives one level up from this module.
  chart_path = "${path.module}/../helm/sdlcma"
}

# Terraform owns the namespace (clean create/destroy lifecycle). The chart is
# installed with namespace.create=false so Helm does not also try to own it.
#
# Trade-off, documented: `terraform destroy` deletes this namespace and
# therefore CASCADES the out-of-band sdlcma-secrets Secret and the PVCs.
# That is acceptable — the Secret is recreated by the documented `kubectl
# create secret` prerequisite step on the next apply, and kind PVC data is
# not durable across teardown anyway. Terraform never reads or stores the
# Secret (consistent with the 1a.2–1a.4 "no credentials in any tool's
# state" stance).
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

  # Namespace is Terraform-managed above; never let Helm create it.
  create_namespace = false

  # See variables.tf: false so apply does not block on the orchestrator pod,
  # which is intentionally not Ready until the operator creates the
  # out-of-band Secret.
  wait    = var.helm_wait
  timeout = var.helm_timeout

  # Recompute/redeploy if the chart contents change between applies.
  recreate_pods = false

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

  depends_on = [kubernetes_namespace.sdlcma]
}
