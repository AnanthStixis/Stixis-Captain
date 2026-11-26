# Everything the Helm install needs (none of these is a secret).

output "resource_group" {
  value = azurerm_resource_group.captain.name
}

output "aks_name" {
  value = azurerm_kubernetes_cluster.captain.name
}

output "workload_identity_client_id" {
  description = "Helm: azure.workloadIdentityClientId"
  value       = azurerm_user_assigned_identity.captain.client_id
}

output "tenant_id" {
  description = "Helm: azure.tenantId"
  value       = data.azurerm_client_config.current.tenant_id
}

output "key_vault_name" {
  description = "Helm: azure.keyVaultName"
  value       = azurerm_key_vault.captain.name
}

output "captain_ledger_key" {
  description = "Helm env CAPTAIN_LEDGER_KEY (pinned key version: a rotation is a new version and a new key id)"
  value       = "azurekv:${azurerm_key_vault_key.ledger.id}"
}

output "captain_secrets_key" {
  description = "Helm env CAPTAIN_SECRETS_KEY (company AI keys entered on the dashboard)"
  value       = "azurekv:${azurerm_key_vault_key.secrets.id}"
}

output "captain_worm" {
  description = "Helm env CAPTAIN_WORM"
  value       = "azure:${azurerm_storage_account.anchors.primary_blob_endpoint}|${azurerm_storage_container.anchors.name}"
}

output "captain_worm_retention_days" {
  value = tostring(var.anchor_retention_days)
}

output "postgres_fqdn" {
  value = azurerm_postgresql_flexible_server.captain.fqdn
}

output "postgres_subnet_cidr" {
  description = "Helm: networkPolicy.databaseCidr"
  value       = azurerm_subnet.postgres.address_prefixes[0]
}

output "helm_values" {
  description = "Paste into clients/<client>.values.yaml (see deploy/opentofu/azure/README.md)."
  value = yamlencode({
    serviceAccount = { name = var.service_account }
    azure = {
      enabled                  = true
      tenantId                 = data.azurerm_client_config.current.tenant_id
      workloadIdentityClientId = azurerm_user_assigned_identity.captain.client_id
      keyVaultName             = azurerm_key_vault.captain.name
    }
    env = {
      CAPTAIN_LEDGER_KEY          = "azurekv:${azurerm_key_vault_key.ledger.id}"
      CAPTAIN_WORM                = "azure:${azurerm_storage_account.anchors.primary_blob_endpoint}|${azurerm_storage_container.anchors.name}"
      CAPTAIN_WORM_RETENTION_DAYS = tostring(var.anchor_retention_days)
      CAPTAIN_SECRETS_KEY         = "azurekv:${azurerm_key_vault_key.secrets.id}"
    }
    networkPolicy = { databaseCidr = azurerm_subnet.postgres.address_prefixes[0] }
  })
}
