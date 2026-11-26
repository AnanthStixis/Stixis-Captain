# Copy to <client>.tfvars. Never put state_passphrase here: pass it as
# TF_VAR_state_passphrase from the client's vault.
subscription_id = "00000000-0000-0000-0000-000000000000" # CHANGE
client          = "acme"                                 # CHANGE: short code
environment     = "pilot"                                # pilot | prod
location        = "westeurope"                           # CHANGE: the client's region
address_space   = "10.40.0.0/16"                         # must not overlap peered networks
admin_cidrs     = ["203.0.113.0/24"]                     # CHANGE: admin / VPN ranges

# Production: HA database, locked anchor retention (irreversible), bigger nodes.
# postgres_high_availability = true
# lock_anchor_retention      = true
