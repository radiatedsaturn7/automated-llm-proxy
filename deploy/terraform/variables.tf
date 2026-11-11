variable "kubeconfig_path" {
  type        = string
  description = "Path to an already-authorized kubeconfig; no credentials are created."
  default     = "~/.kube/config"
}
variable "kube_context" {
  type        = string
  description = "Explicit kubeconfig context (null uses current context)."
  default     = null
}
variable "namespace" {
  type    = string
  default = "llm-gateway"
}
variable "release_name" {
  type    = string
  default = "gateway"
}
variable "existing_secret" {
  type        = string
  description = "Pre-created Secret containing DATABASE_URL and gateway keys."
}
variable "image_repository" {
  type        = string
  description = "Registry repository where you published your image."
}
variable "image_tag" {
  type    = string
  default = "0.1.0"
}
variable "image_digest" {
  type        = string
  default     = ""
  description = "Prefer a verified sha256 digest for immutable deployments."
}
variable "replicas" {
  type    = number
  default = 3
  validation {
    condition     = var.replicas >= 1 && floor(var.replicas) == var.replicas
    error_message = "replicas must be a positive integer."
  }
}
variable "enable_autoscaling" {
  type    = bool
  default = true
}
variable "max_replicas" {
  type    = number
  default = 10
}
variable "ingress_host" {
  type    = string
  default = ""
}
variable "ingress_class" {
  type    = string
  default = ""
}
variable "tls_secret" {
  type    = string
  default = ""
}

variable "public_url" {
  type        = string
  description = "Canonical HTTPS URL used by authentication callbacks."
  validation {
    condition     = startswith(var.public_url, "https://")
    error_message = "public_url must use HTTPS."
  }
}
