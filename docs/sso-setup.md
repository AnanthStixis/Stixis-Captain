# Single sign-on setup (Microsoft Entra ID, Okta, other OpenID Connect providers)

Captain signs administrators in with OpenID Connect (authorization code with PKCE; Captain does the token exchange on the server). You need an identity provider app registration, then the settings below, then `python -m captain.sso_check`.

Status: written from the providers' public documentation and Captain's tests. It has not been run against a real Entra ID or Okta tenant. Treat the provider-side steps as a checklist to confirm in your tenant, and use the check tool plus one real test sign-in as the proof.

## 1. Register Captain at the provider
- **Application type:** a web application with a client secret (Captain is a confidential client).
- **Redirect URI:** exactly `https://<console host>/admin/v1/auth/callback`. The console host must be https in production.
- **Scopes:** `openid profile email`. For Okta add `groups` to `scopes` as well.
- **Issuer:** Entra ID `https://login.microsoftonline.com/<tenant id>/v2.0`; Okta `https://<your-org>.okta.com` (organisation server) or `https://<your-org>.okta.com/oauth2/<server id>` (custom server). It must equal the `issuer` in the provider's discovery document character for character.

## 2. Tell Captain which users get which role
Captain maps a claim in the ID token to Captain roles (`admin`, `approver`, `finops`, `auditor`, `viewer`, ...). Two ways, pick one:

**Groups (`"roles_claim": "groups"`).** The ID token must carry the groups.
- Entra ID: configure the app's token groups claim. Microsoft documents choosing "Groups assigned to the application" instead of "All groups" to keep tokens small. The claim holds group object ids (GUIDs); Captain matches them case-insensitively.
- Okta: add a groups claim to the ID token (Claims tab, value type Groups, include in ID token "Always") and request the `groups` scope.
- **Group overage (Entra ID):** a user in more than 200 groups gets no `groups` claim, only a pointer (`_claim_names` / `hasgroups`). Captain recognises this and shows "too many groups for the sign-in token" instead of the misleading "no role". Fix it at the provider: send only the groups assigned to the application, or use app roles.

**App roles (`"roles_claim": "roles"`, Entra ID).** Define app roles on the registration, assign users or groups to them, and map the role values in `group_roles`. Microsoft recommends this for avoiding overage.

## 3. Captain settings (environment or `backend/.env`)
```
CAPTAIN_ADMIN_OIDC={"issuer":"https://login.microsoftonline.com/<tenant>/v2.0","client_id":"<app id>","client_secret":"<secret>","redirect_uri":"https://captain.example.com/admin/v1/auth/callback","scopes":"openid profile email","roles_claim":"groups","name_claim":"preferred_username","group_roles":{"<group object id>":["admin","approver","auditor"],"<other group id>":["viewer"]}}
CAPTAIN_SESSION_SECRET=<64 hex characters; keep it secret and stable>
```
Generate the session secret with `python -c "import secrets; print(secrets.token_hex(32))"`. Keep the client secret out of source control and rotate it before it expires at the provider.

## 4. Check it
```powershell
.venv\Scripts\python -m captain.sso_check --url
```
It reads the settings above, asks the provider for its discovery document and signing keys, and prints PASS / WARN / FAIL for: required settings, https redirect URI, session secret, group-to-role mapping (unknown role names, nothing mapped to admin), issuer match, PKCE (S256), the token endpoint authentication method Captain will use, usable signing algorithms, requested scopes, and signing keys. It never prints a secret. `--url` prints an authorization URL to open in a browser.

What it cannot check: that your test user's token actually contains the group or role claim. Sign in once with a test user and, if you land on "no role", fix the claim at the provider (or the `group_roles` ids) and try again.

## How Captain talks to the provider
- Token endpoint login: Captain uses `client_secret_post` when the provider offers it and `client_secret_basic` otherwise (the OpenID Connect default when the provider does not say).
- ID token: signature (RS/ES/PS algorithms only, keys from the provider's JWKS and refreshed on an unknown key id), issuer, audience, expiry, nonce, and, when the token has several audiences, that Captain is the authorized party.
- Sessions: an HttpOnly cookie holding a random id; only its hash is stored. Absolute lifetime 8 hours by default (`session_lifetime_s`). Changes made with the cookie also need a CSRF token and an allowed origin.

## Not covered yet
- Signing out at the identity provider (Captain ends its own session; the user may stay signed in at the provider).
- Refresh tokens (when the session ends the user signs in again).
- SAML, SCIM provisioning, and role assignment from provider-side user attributes other than one claim.
- Several identity providers at once.

## Okta walk-through (checklist, confirm in your Okta admin console)
1. **Create the app:** Applications, Create App Integration, OIDC - OpenID Connect, Web Application.
2. **Sign-in redirect URI:** `http://localhost:8100/admin/v1/auth/callback` for local testing (use `localhost`, and open the console at `http://localhost:8100`), or `https://<console host>/admin/v1/auth/callback` in production. Assign the groups or people who may use Captain to the app.
3. **Copy** the Client ID and Client secret. Your Okta domain is the host of your admin URL without `-admin` (for example `dev-123456.okta.com`).
4. **Groups in the ID token:** on the app's Sign On tab, under OpenID Connect ID Token, add a groups claim named `groups` with a filter that matches your Captain groups (for example "Starts with" `Captain`). With a custom authorization server, add the claim under Security, API, Authorization Servers, Claims, include it in the ID token, and use that server's issuer (`https://<domain>/oauth2/<id>`). Request the `groups` scope (the helper does).
5. **Okta group names** are what appears in the claim, so use the group NAMES in the role mapping.
6. **Run the helper in your own terminal** so the client secret is typed there and never pasted anywhere else:
   ```powershell
   cd backend
   .venv\Scripts\python -m captain.sso_setup
   ```
   It writes the settings to `backend\.env`, keeps the previous value as `CAPTAIN_ADMIN_OIDC_PREVIOUS`, and runs the check.
7. **Restart the Admin API**, open `http://localhost:8100`, click "Sign in with SSO", and sign in as a user in one of your mapped groups.
