# Stixis super admin: onboarding a new company

You run Stixis's own Captain, with the control plane switched on (`CAPTAIN_CONTROL_PLANE=1`) and the role **platform-admin**. Each company gets its own separate Captain; the **Companies** page tracks them. No company's AI activity or evidence ever comes to this page.

## 1. Add the company
1. **Companies (Stixis) → Add a company**: company name, a short code (2-12 lowercase letters/digits, used in install names), legal name, email domain, country, industry, the Azure region where its data must live, pilot or prod, and the **org admin** (email and name).
2. **Expected:** the company shows as **onboarding**, health "Not installed yet", and its page opens with four steps.

## 2. Download the install files
1. Press **Download install files**. The zip holds `client.tfvars`, `client.backend.hcl`, `helm-values-extra.yaml`, `platform-token.txt` and a README.
2. Follow `deploy/opentofu/azure/README.md` in the company's subscription (fill in the CHANGE lines, `tofu apply`, `helm install`).
3. Store `platform-token.txt` in the install's Key Vault as the secret `captain-platform-token`, merge `helm-values-extra.yaml` into the Helm values, then **delete `platform-token.txt`**.
4. **Expected:** the company's Captain answers at its admin address, `/admin/v1/health` returns 200.

## 3. Connect and bootstrap
1. Enter the install's admin address (https) under **Where the company's Captain is**, **Save**.
2. Press **Bootstrap**. **Expected:** a one-time sign-in link for the org admin. Copy it and send it to them securely. It works once, for 7 days, and is not kept here (press Bootstrap again for a new one).
   - If the company has already connected SSO, there is no link: the org admin signs in with SSO.
3. Press **Check now**. **Expected:** "1 users (1 org admins) · 0 Captain keys · ...", no problem shown.

## 4. Hand over
- Point the org admin to the onboarding guide (`org-admin-onboarding`, also on their dashboard).
- When they are live, set the status to **active**. **Suspended** marks a company that has stopped (it does not switch their Captain off).

## If something goes wrong
| You see | Likely cause | Fix |
|---|---|---|
| "did not accept the platform token" | Token not in the install's Key Vault / Helm values, or a different one | Redo step 2.3 with the token from this company's install files |
| "could not be reached" | Wrong address, network, or the install is down | Open `<address>/admin/v1/health` from a Stixis network |
| Bootstrap says invites are not configured | The install's Admin API has no invite store | Use the current Helm chart and image |
