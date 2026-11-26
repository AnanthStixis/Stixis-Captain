# Copy to <client>.backend.hcl. A storage account the client owns, created once by hand
# (state is also encrypted by OpenTofu itself before it is written here).
resource_group_name  = "rg-captain-tfstate"   # CHANGE
storage_account_name = "stcaptaintfstate"     # CHANGE
container_name       = "tfstate"
key                  = "acme-pilot.tfstate"   # CHANGE: <client>-<environment>
use_azuread_auth     = true
