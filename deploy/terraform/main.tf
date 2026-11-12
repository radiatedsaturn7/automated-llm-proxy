terraform {
  required_version = ">= 1.6, < 2.0"
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.3"
    }
  }
}

provider "helm" {
  kubernetes {
    config_path    = var.kubeconfig_path
    config_context = var.kube_context
  }
}

# The namespace, Secret, TLS ingress controller and HA PostgreSQL exist already.
# This example intentionally does not put credential values into Terraform state.
resource "helm_release" "gateway" {
  name             = var.release_name
  namespace        = var.namespace
  chart            = "${path.module}/../helm/llm-gateway"
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  wait_for_jobs    = true
  timeout          = 600
  values = [yamlencode({
    existingSecret = var.existing_secret
    publicUrl      = var.public_url
    image = {
      repository = var.image_repository
      tag        = var.image_tag
      digest     = var.image_digest
    }
    replicaCount = var.replicas
    autoscaling = {
      enabled     = var.enable_autoscaling
      minReplicas = var.replicas
      maxReplicas = var.max_replicas
    }
    podDisruptionBudget = {
      enabled      = var.replicas > 1
      minAvailable = max(1, var.replicas - 1)
    }
    ingress = {
      enabled   = var.ingress_host != ""
      className = var.ingress_class
      host      = var.ingress_host
      tls = var.ingress_host != "" ? [{
        hosts      = [var.ingress_host]
        secretName = var.tls_secret
      }] : []
    }
  })]
  lifecycle {
    precondition {
      condition     = var.ingress_host == "" || var.tls_secret != ""
      error_message = "A TLS Secret is required when enabling public ingress."
    }
    precondition {
      condition     = var.max_replicas >= var.replicas
      error_message = "max_replicas must not be lower than replicas."
    }
  }
}

output "release" {
  value = helm_release.gateway.name
}
