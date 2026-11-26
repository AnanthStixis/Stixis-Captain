# Stixis Captain on Azure, one installation per client (plan E2).
# OpenTofu >= 1.8: state is encrypted client-side before it reaches the backend, because
# it holds generated secrets (database password, key pepper).
terraform {
  required_version = ">= 1.8.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.14"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state in the client's own storage account. Per client:
  #   tofu init -backend-config=clients/<client>.backend.hcl
  backend "azurerm" {}

  encryption {
    key_provider "pbkdf2" "state" {
      passphrase = var.state_passphrase
    }
    method "aes_gcm" "state" {
      keys = key_provider.pbkdf2.state
    }
    state {
      method   = method.aes_gcm.state
      enforced = true
    }
    plan {
      method   = method.aes_gcm.state
      enforced = true
    }
  }
}

provider "azurerm" {
  subscription_id     = var.subscription_id
  storage_use_azuread = true # the anchor storage account has account keys disabled
  features {
    key_vault {
      # Never purge the ledger signing key or secrets on destroy: purge protection is on.
      purge_soft_delete_on_destroy = false
    }
  }
}
