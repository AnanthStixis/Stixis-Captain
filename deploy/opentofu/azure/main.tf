data "azurerm_client_config" "current" {}

resource "random_string" "suffix" {
  length  = 4
  upper   = false
  special = false
}

locals {
  name    = "cpt-${var.client}-${var.environment}"
  compact = "cpt${var.client}${random_string.suffix.result}" # for names without hyphens
  prod    = var.environment == "prod"
  tags = merge(var.tags, {
    app         = "stixis-captain"
    client      = var.client
    environment = var.environment
    managed_by  = "opentofu"
  })
}

resource "azurerm_resource_group" "captain" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

# ----------------------------------------------------------------------------- network
resource "azurerm_virtual_network" "captain" {
  name                = "vnet-${local.name}"
  location            = azurerm_resource_group.captain.location
  resource_group_name = azurerm_resource_group.captain.name
  address_space       = [var.address_space]
  tags                = local.tags
}

resource "azurerm_subnet" "aks" {
  name                 = "snet-aks"
  resource_group_name  = azurerm_resource_group.captain.name
  virtual_network_name = azurerm_virtual_network.captain.name
  address_prefixes     = [cidrsubnet(var.address_space, 4, 0)] # /20
  # Key Vault and the anchor storage only accept traffic from this subnet (and admin IPs).
  service_endpoints = ["Microsoft.KeyVault", "Microsoft.Storage"]
}

resource "azurerm_subnet" "postgres" {
  name                 = "snet-postgres"
  resource_group_name  = azurerm_resource_group.captain.name
  virtual_network_name = azurerm_virtual_network.captain.name
  address_prefixes     = [cidrsubnet(var.address_space, 8, 16)] # /24
  delegation {
    name = "postgres"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

# ------------------------------------------------------------------------- monitoring
resource "azurerm_log_analytics_workspace" "captain" {
  name                = "log-${local.name}"
  location            = azurerm_resource_group.captain.location
  resource_group_name = azurerm_resource_group.captain.name
  sku                 = "PerGB2018"
  retention_in_days   = 30 # platform logs; evidence lives in the ledger, not here
  tags                = local.tags
}

# --------------------------------------------------------------------------------- AKS
resource "azurerm_kubernetes_cluster" "captain" {
  name                      = "aks-${local.name}"
  location                  = azurerm_resource_group.captain.location
  resource_group_name       = azurerm_resource_group.captain.name
  dns_prefix                = local.name
  kubernetes_version        = var.kubernetes_version
  sku_tier                  = local.prod ? "Standard" : "Free"
  automatic_upgrade_channel = "patch"
  oidc_issuer_enabled       = true # workload identity: pods get Azure tokens, no secrets
  workload_identity_enabled = true
  local_account_disabled    = true # Entra ID sign-in only
  azure_policy_enabled      = true

  default_node_pool {
    name                         = "system"
    vm_size                      = "Standard_D2s_v5"
    node_count                   = 2
    vnet_subnet_id               = azurerm_subnet.aks.id
    zones                        = ["1", "2", "3"]
    only_critical_addons_enabled = true
    upgrade_settings {
      max_surge = "33%"
    }
  }

  identity {
    type = "SystemAssigned"
  }

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = data.azurerm_client_config.current.tenant_id
  }

  api_server_access_profile {
    authorized_ip_ranges = var.admin_cidrs
  }

  network_profile {
    network_plugin    = "azure"
    network_policy    = "azure" # enforces the Helm chart's NetworkPolicy (egress to providers only)
    load_balancer_sku = "standard"
    service_cidr      = "172.20.0.0/16"
    dns_service_ip    = "172.20.0.10"
  }

  key_vault_secrets_provider {
    secret_rotation_enabled = true # Key Vault secrets synced into the captain-secrets Secret
  }

  oms_agent {
    log_analytics_workspace_id = azurerm_log_analytics_workspace.captain.id
  }

  tags = local.tags
}

resource "azurerm_kubernetes_cluster_node_pool" "user" {
  name                  = "captain"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.captain.id
  vm_size               = var.aks_node_size
  node_count            = var.aks_node_count
  vnet_subnet_id        = azurerm_subnet.aks.id
  zones                 = ["1", "2", "3"]
  mode                  = "User"
  upgrade_settings {
    max_surge = "33%"
  }
  tags = local.tags
}

# The identity Captain's pods run as (federated to the Kubernetes service account).
resource "azurerm_user_assigned_identity" "captain" {
  name                = "id-${local.name}"
  location            = azurerm_resource_group.captain.location
  resource_group_name = azurerm_resource_group.captain.name
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "captain" {
  name                = "captain-pods"
  resource_group_name = azurerm_resource_group.captain.name
  parent_id           = azurerm_user_assigned_identity.captain.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.captain.oidc_issuer_url
  subject             = "system:serviceaccount:${var.kubernetes_namespace}:${var.service_account}"
}

# -------------------------------------------------------------------------- PostgreSQL
resource "azurerm_private_dns_zone" "postgres" {
  name                = "${local.name}.private.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.captain.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                  = "postgres"
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  resource_group_name   = azurerm_resource_group.captain.name
  virtual_network_id    = azurerm_virtual_network.captain.id
}

resource "random_password" "postgres" {
  length  = 40
  special = false # goes into a URL
}

resource "azurerm_postgresql_flexible_server" "captain" {
  name                          = "psql-${local.name}-${random_string.suffix.result}"
  location                      = azurerm_resource_group.captain.location
  resource_group_name           = azurerm_resource_group.captain.name
  version                       = "16"
  sku_name                      = var.postgres_sku
  storage_mb                    = var.postgres_storage_mb
  delegated_subnet_id           = azurerm_subnet.postgres.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  public_network_access_enabled = false
  administrator_login           = "captainadmin"
  administrator_password        = random_password.postgres.result
  backup_retention_days         = var.backup_retention_days
  geo_redundant_backup_enabled  = false # a second copy outlives an erasure (ADR-005)
  zone                          = "1"

  dynamic "high_availability" {
    for_each = var.postgres_high_availability ? [1] : []
    content {
      mode                      = "ZoneRedundant"
      standby_availability_zone = "2"
    }
  }

  tags       = local.tags
  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres]

  lifecycle {
    ignore_changes = [zone, high_availability[0].standby_availability_zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "captain" {
  name      = "captain"
  server_id = azurerm_postgresql_flexible_server.captain.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_postgresql_flexible_server_configuration" "tls" {
  name      = "require_secure_transport"
  server_id = azurerm_postgresql_flexible_server.captain.id
  value     = "on"
}

# --------------------------------------------------------------------------- Key Vault
resource "azurerm_key_vault" "captain" {
  name                          = "kv${local.compact}"
  location                      = azurerm_resource_group.captain.location
  resource_group_name           = azurerm_resource_group.captain.name
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = local.prod ? "premium" : "standard" # premium = HSM-backed keys
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = true

  network_acls {
    default_action             = "Deny"
    bypass                     = "AzureServices"
    ip_rules                   = var.admin_cidrs
    virtual_network_subnet_ids = [azurerm_subnet.aks.id]
  }

  tags = local.tags
}

# Whoever runs OpenTofu creates the key and secrets.
resource "azurerm_role_assignment" "deployer_kv" {
  scope                = azurerm_key_vault.captain.id
  role_definition_name = "Key Vault Administrator"
  principal_id         = data.azurerm_client_config.current.object_id
}

# The ledger signing key never leaves Key Vault: Captain sends a digest, gets a signature.
resource "azurerm_key_vault_key" "ledger" {
  name         = "captain-ledger"
  key_vault_id = azurerm_key_vault.captain.id
  key_type     = local.prod ? "EC-HSM" : "EC"
  curve        = "P-256"
  key_opts     = ["sign", "verify"]
  depends_on   = [azurerm_role_assignment.deployer_kv]
}

# Wraps the per-key data keys of company AI keys entered on the dashboard (plan step 3).
# RSA, wrap/unwrap only: the private half never leaves Key Vault.
resource "azurerm_key_vault_key" "secrets" {
  name         = "captain-secrets"
  key_vault_id = azurerm_key_vault.captain.id
  key_type     = local.prod ? "RSA-HSM" : "RSA"
  key_size     = 3072
  key_opts     = ["wrapKey", "unwrapKey"]
  depends_on   = [azurerm_role_assignment.deployer_kv]
}

resource "random_id" "pepper" {
  byte_length = 32
}

resource "azurerm_key_vault_secret" "database_url" {
  name         = "captain-database-url"
  key_vault_id = azurerm_key_vault.captain.id
  value = format(
    "postgresql+psycopg://%s:%s@%s:5432/%s?sslmode=require",
    azurerm_postgresql_flexible_server.captain.administrator_login,
    random_password.postgres.result,
    azurerm_postgresql_flexible_server.captain.fqdn,
    azurerm_postgresql_flexible_server_database.captain.name,
  )
  depends_on = [azurerm_role_assignment.deployer_kv]
}

resource "azurerm_key_vault_secret" "pepper" {
  name         = "captain-key-pepper"
  key_vault_id = azurerm_key_vault.captain.id
  value        = random_id.pepper.hex
  depends_on   = [azurerm_role_assignment.deployer_kv]
}

# Pods: sign with the ledger key, read the secrets.
resource "azurerm_role_assignment" "pods_crypto" {
  scope                = azurerm_key_vault_key.ledger.resource_versionless_id
  role_definition_name = "Key Vault Crypto User"
  principal_id         = azurerm_user_assigned_identity.captain.principal_id
}

resource "azurerm_role_assignment" "pods_wrap" {
  scope                = azurerm_key_vault_key.secrets.resource_versionless_id
  role_definition_name = "Key Vault Crypto User"
  principal_id         = azurerm_user_assigned_identity.captain.principal_id
}

resource "azurerm_role_assignment" "pods_secrets" {
  scope                = azurerm_key_vault.captain.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.captain.principal_id
}

# ----------------------------------------------------------- write-once ledger anchors
resource "azurerm_storage_account" "anchors" {
  name                            = "st${local.compact}"
  location                        = azurerm_resource_group.captain.location
  resource_group_name             = azurerm_resource_group.captain.name
  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false # Entra ID only: no account keys to leak

  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"]
    ip_rules                   = var.admin_cidrs
    virtual_network_subnet_ids = [azurerm_subnet.aks.id]
  }

  tags = local.tags
}

resource "azurerm_role_assignment" "deployer_blob" {
  scope                = azurerm_storage_account.anchors.id
  role_definition_name = "Storage Blob Data Owner"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_storage_container" "anchors" {
  name                  = "anchors"
  storage_account_id    = azurerm_storage_account.anchors.id
  container_access_type = "private"
  depends_on            = [azurerm_role_assignment.deployer_blob]
}

# Captain refuses to start unless this policy exists (AzureImmutableBlobWormStore).
resource "azurerm_storage_container_immutability_policy" "anchors" {
  storage_container_resource_manager_id = azurerm_storage_container.anchors.id
  immutability_period_in_days           = var.anchor_retention_days
  protected_append_writes_all_enabled   = false
  locked                                = var.lock_anchor_retention
}

resource "azurerm_role_assignment" "pods_blob" {
  scope                = azurerm_storage_container.anchors.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.captain.principal_id
}
