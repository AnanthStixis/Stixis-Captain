# Captain on Azure: per-client install (plan E2)

One installation per client, in the client's own subscription. OpenTofu creates the Azure
resources; the Helm chart (`deploy/helm/captain`) runs Captain on them.

**Status:** `tofu validate` and `tofu fmt -check` pass (OpenTofu 1.13.10, azurerm 4.x); the Helm chart passes `helm lint` and renders with default and Azure values (Helm 4.3.2). **Not yet applied to a real subscription:** do a first install in a sandbox subscription before any client. Validate again with `tools\opentofu	ofu.exe init -backend=false` then `tofu validate` (set `TF_VAR_state_passphrase` to any 16+ characters).

## What it creates

| Resource | Why |
|---|---|
| VNet with an AKS subnet and a delegated PostgreSQL subnet | Captain and its database never get public addresses |
| AKS (zones 1-3, Entra ID sign-in only, API server limited to `admin_cidrs`, Azure network policy, workload identity, Key Vault CSI driver) | Runs the Helm chart; NetworkPolicy keeps provider egress to the gateway only |
| User-assigned managed identity, federated to the `captain` service account | Pods get Azure tokens without any stored credential |
| PostgreSQL 16 Flexible Server, private, TLS required, backups **max 30 days**, no geo-redundant copy | Captain's database. The 30-day cap keeps the erasure promise (ADR-005) |
| Key Vault (premium + HSM key in prod), purge protection, RBAC, network-restricted | The ledger signing key (EC P-256, never leaves Key Vault), the database URL and the key pepper |
| Storage account (ZRS, no account keys), container `anchors` with a time-based immutability policy | Write-once ledger anchors. Captain refuses to start if the container has no immutability policy |
| Log Analytics workspace | Platform logs (30 days). Evidence lives in the ledger, not here |

## Before the first install (once per client)

1. A storage account for OpenTofu state in the client's subscription (any name), with a container `tfstate`. State is also encrypted by OpenTofu itself before it is written.
2. A state passphrase (at least 16 characters) kept in the client's vault. Losing it means losing access to the state.
3. You are signed in with `az login` as someone who can create resources and role assignments in the subscription (Owner, or Contributor + User Access Administrator).
4. A container image built with the Azure libraries:
   `docker build --build-arg EXTRAS=azure -t <registry>/stixis/captain-backend:<tag> backend`
5. A signed policy bundle and its public key (ADR-003):
   `python -m captain.policy_bundle --policies ../policies --key signing.pem --out bundle.tar.gz`

## Install

From `deploy/opentofu/azure`:

```powershell
Copy-Item clients\example.tfvars clients\acme.tfvars            # edit: subscription, client, region, admin_cidrs
Copy-Item clients\example.backend.hcl clients\acme.backend.hcl  # edit: the state storage account
$env:TF_VAR_state_passphrase = "<from the client's vault>"
tofu init -backend-config=clients\acme.backend.hcl
tofu plan -var-file=clients\acme.tfvars -out=acme.tfplan
tofu apply acme.tfplan
tofu output -raw helm_values > ..\..\helm\captain\clients-acme.values.yaml
```

Add the secrets OpenTofu does not create to the Key Vault (provider API keys, the SSO client secret, `CAPTAIN_SESSION_SECRET`) and list each one under `azure.secrets` in the client values file (Kubernetes key -> Key Vault secret name).

Then, with `kubectl` pointed at the new cluster (`az aks get-credentials -g <resource_group> -n <aks_name>`):

```powershell
kubectl create namespace captain
kubectl -n captain create configmap captain-policy-bundle --from-file=bundle.tar.gz
kubectl -n captain create configmap captain-policy-key --from-file=public.pem
helm upgrade --install captain ..\..\helm\captain -n captain -f ..\..\helm\captain\clients-acme.values.yaml --set image.repository=<registry>/stixis/captain-backend --set image.tag=<tag>
```

Run the database migrations once from a pod that has the database URL: `kubectl -n captain exec deploy/captain-admin -- alembic upgrade head`.

## Check it

- `kubectl -n captain get pods`: every pod `Running`; OPA ready only with a valid signed bundle.
- Admin API `/admin/v1/health`, then **Audit & reports -> Verify now** after the first calls.
- The worker writes an anchor within an hour (`anchor_every_s`); it appears in the `anchors` container and cannot be deleted.
- Then the backup/restore drill: `python scripts/restore_drill.py` against a scratch database (runbook section 7).

## Production differences

`environment = "prod"` gives a paid AKS tier, premium Key Vault with an HSM-backed ledger key. Also set `postgres_high_availability = true` and, once the retention period is agreed, `lock_anchor_retention = true` (**irreversible**: a locked policy can only be extended). Narrow `networkPolicy.providerEgress` and `networkPolicy.azureEgress` with an egress firewall (Azure Firewall FQDN tags `AzureKeyVault`, `Storage`, `AzureActiveDirectory`, plus your model providers).

## Notes and limits

- The database URL uses the server's admin login. For production, create a separate application role with only the rights Captain needs and store its URL in `captain-database-url` instead.
- Live evaluation reads its rubric from `CAPTAIN_EVAL_RUBRIC`; mount `policies/evaluation/rubric.json` (for example from a ConfigMap) when you turn evaluation on.
- Rotating the ledger key: create a new key version in Key Vault, export the old version's public key as `azurekv-captain-ledger-<old version>.pem` into the directory named by `CAPTAIN_LEDGER_PUBLIC_KEYS_DIR`, then update `CAPTAIN_LEDGER_KEY` to the new version's URL. Old entries stay verifiable.
- `tofu destroy` does not purge Key Vault (purge protection) and cannot delete anchors still under retention. That is intended.
