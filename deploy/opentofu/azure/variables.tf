variable "subscription_id" {
  description = "The client's Azure subscription."
  type        = string
}

variable "client" {
  description = "Short client code used in every resource name (lowercase letters and digits, 2-12)."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,11}$", var.client))
    error_message = "client must be 2-12 lowercase letters/digits, starting with a letter."
  }
}

variable "environment" {
  description = "pilot | prod"
  type        = string
  default     = "pilot"
  validation {
    condition     = contains(["pilot", "prod"], var.environment)
    error_message = "environment must be pilot or prod."
  }
}

variable "location" {
  description = "Azure region (data residency: pick the client's region)."
  type        = string
  default     = "westeurope"
}

variable "state_passphrase" {
  description = "Encrypts the OpenTofu state and plans (min 16 characters). Keep it in the client's vault, not in a file."
  type        = string
  sensitive   = true
  validation {
    condition     = length(var.state_passphrase) >= 16
    error_message = "state_passphrase must be at least 16 characters."
  }
}

variable "address_space" {
  description = "VNet range. Must not overlap the client's networks that will peer with it."
  type        = string
  default     = "10.40.0.0/16"
}

variable "admin_cidrs" {
  description = "Networks allowed to reach the AKS API server (the client's admin / VPN ranges)."
  type        = list(string)
}

variable "aks_node_size" {
  type    = string
  default = "Standard_D4s_v5"
}

variable "aks_node_count" {
  description = "Nodes in the user pool (spread across zones)."
  type        = number
  default     = 3
}

variable "kubernetes_version" {
  description = "AKS version; null = the region's default."
  type        = string
  default     = null
}

variable "postgres_sku" {
  type    = string
  default = "GP_Standard_D2ds_v5"
}

variable "postgres_storage_mb" {
  type    = number
  default = 65536
}

variable "postgres_high_availability" {
  description = "Zone-redundant standby (recommended for prod)."
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  description = "PostgreSQL backups. Erasure (ADR-005) is complete only when the last backup holding a person's key expires, so this is capped at 30."
  type        = number
  default     = 30
  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 30
    error_message = "backup_retention_days must be 7-30 (erasure promise, ADR-005)."
  }
}

variable "anchor_retention_days" {
  description = "Write-once retention of ledger anchors (CAPTAIN_WORM_RETENTION_DAYS; Captain requires >= 183)."
  type        = number
  default     = 400
  validation {
    condition     = var.anchor_retention_days >= 183
    error_message = "anchor_retention_days must be at least 183."
  }
}

variable "lock_anchor_retention" {
  description = "Lock the anchor container's retention policy. Locking is IRREVERSIBLE (it can then only be extended). Leave false for a pilot, set true for production."
  type        = bool
  default     = false
}

variable "kubernetes_namespace" {
  type    = string
  default = "captain"
}

variable "service_account" {
  description = "Kubernetes service account the Captain pods run as (the Helm chart's serviceAccount.name)."
  type        = string
  default     = "captain"
}

variable "tags" {
  type    = map(string)
  default = {}
}
