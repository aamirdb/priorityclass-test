terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.23.0"
    }
  }
}

provider "kubernetes" {
  config_path = "~/.kube/config"
}

# 1. Least-privilege ServiceAccount & Role in kube-system to patch fluentbit-gke*
resource "kubernetes_service_account_v1" "fluentbit_patcher" {
  metadata {
    name      = "fluentbit-ds-eviction-patcher"
    namespace = "kube-system"
  }
}

resource "kubernetes_role_v1" "fluentbit_patcher" {
  metadata {
    name      = "fluentbit-ds-eviction-patcher"
    namespace = "kube-system"
  }

  rule {
    api_groups = ["apps"]
    resources  = ["daemonsets"]
    # Covers standard fluentbit-gke and high-PD variants on GKE
    resource_names = [
      "fluentbit-gke",
      "fluentbit-gke-256pd",
      "fluentbit-gke-max",
      "fluentbit-gke-max-pd-volumes",
      "fluentbit-gke-managed-node-small",
      "fluentbit-gke-managed-node-big",
    ]
    verbs = ["get", "patch"]
  }
}

resource "kubernetes_role_binding_v1" "fluentbit_patcher" {
  metadata {
    name      = "fluentbit-ds-eviction-patcher"
    namespace = "kube-system"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.fluentbit_patcher.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.fluentbit_patcher.metadata[0].name
    namespace = "kube-system"
  }
}

# 2. One-shot Job that applies the strategic merge patch to .spec.template.metadata.annotations
resource "kubernetes_job_v1" "patch_fluentbit_ds_eviction" {
  metadata {
    name      = "patch-fluentbit-ds-eviction"
    namespace = "kube-system"
  }

  spec {
    ttl_seconds_after_finished = 300
    backoff_limit              = 3

    template {
      metadata {
        annotations = {
          "cluster-autoscaler.kubernetes.io/safe-to-evict" = "true"
        }
      }

      spec {
        service_account_name = kubernetes_service_account_v1.fluentbit_patcher.metadata[0].name
        restart_policy       = "OnFailure"

        container {
          name  = "kubectl-patch"
          image = "registry.k8s.io/kubectl:v1.31.0"
          command = [
            "kubectl",
            "patch",
            "daemonset",
            "fluentbit-gke",
            "fluentbit-gke-256pd",
            "fluentbit-gke-max",
            "fluentbit-gke-managed-node-small",
            "fluentbit-gke-managed-node-big",
            "-n",
            "kube-system",
            "--type=strategic",
            "-p",
            jsonencode({
              spec = {
                template = {
                  metadata = {
                    annotations = {
                      "cluster-autoscaler.kubernetes.io/enable-ds-eviction" = "false"
                    }
                  }
                }
              }
            }),
          ]
        }
      }
    }
  }

  wait_for_completion = true
  timeouts {
    create = "1m"
    update = "1m"
  }

  depends_on = [kubernetes_role_binding_v1.fluentbit_patcher]
}

