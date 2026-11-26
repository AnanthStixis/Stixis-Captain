#!/bin/sh
# Create qa.env (fresh random secrets) and edge/Caddyfile for the shared Captain.
#
#   sh make-env.sh <base-domain> [certs|http]
#   sh make-env.sh stixis.com certs
#
# Stixis console:  https://captain.<base-domain>      (CAPTAIN_PLATFORM_HOST)
# Each company:    https://<code>.<base-domain>        (no per-company setup at all)
#
# certs  a wildcard certificate for *.<base-domain> (covers captain.<base-domain> too):
#        edge/certs/wildcard.crt + edge/certs/wildcard.key          <- recommended
# http   plain http (QA on a closed network only; sign-in cookies are not marked secure)
# Existing files are never overwritten.
set -e
DOMAIN="$1"; TLS="${2:-certs}"
[ -n "$DOMAIN" ] || { echo "usage: sh make-env.sh <base-domain> [certs|http]"; exit 2; }
case "$TLS" in certs|http) ;; *) echo "tls: certs or http"; exit 2 ;; esac
[ -e qa.env ] && { echo "qa.env exists; not overwritten"; exit 1; }
[ -e edge/Caddyfile ] && { echo "edge/Caddyfile exists; not overwritten"; exit 1; }
hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }
SCHEME=https; [ "$TLS" = "http" ] && SCHEME=http
PLATFORM="captain.$DOMAIN"

umask 077
cat > qa.env <<EOF
# Shared Captain (QA). Contains secrets: keep it on the server only.
DB_PASSWORD=$(hex 24)

CAPTAIN_BASE_DOMAIN=$DOMAIN
CAPTAIN_PLATFORM_HOST=$PLATFORM
CAPTAIN_PUBLIC_SCHEME=$SCHEME

CAPTAIN_KEY_PEPPER=$(hex 32)
CAPTAIN_SECRETS_KEY=hex:$(hex 32)
CAPTAIN_SESSION_SECRET=$(hex 32)

# Fair share between companies (per gateway copy) and database connections per company.
CAPTAIN_TENANT_RPM=600
CAPTAIN_TENANT_DB_POOL_SIZE=3

# Optional: email sign-in links for companies without SSO.
# CAPTAIN_SMTP_HOST=
# CAPTAIN_SMTP_PORT=587
# CAPTAIN_SMTP_FROM=
# CAPTAIN_SMTP_USER=
# CAPTAIN_SMTP_PASSWORD=
EOF

umask 022
mkdir -p edge/certs
if [ "$TLS" = "certs" ]; then
  SITE="$PLATFORM, *.$DOMAIN"
  TLSLINE="	tls /etc/caddy/certs/wildcard.crt /etc/caddy/certs/wildcard.key"
else
  SITE="http://$PLATFORM, http://*.$DOMAIN"
  TLSLINE=""
fi
cat > edge/Caddyfile <<EOF
# Shared Captain: Stixis at $PLATFORM, every company at <code>.$DOMAIN (written by make-env.sh)
{
	auto_https disable_redirects
}

$SITE {
$TLSLINE
	encode gzip
	# AI calls stream: pass every chunk through at once.
	@gateway path /v1/* /v1beta/*
	handle @gateway {
		reverse_proxy gateway:8000 {
			flush_interval -1
		}
	}
	handle /core/* {
		reverse_proxy core:8080
	}
	handle /mcp* {
		reverse_proxy mcp:8300 {
			flush_interval -1
		}
	}
	handle {
		reverse_proxy admin:8100
	}
}
EOF
echo "wrote qa.env and edge/Caddyfile for $SCHEME://$PLATFORM and $SCHEME://<code>.$DOMAIN"
[ "$TLS" = "certs" ] && echo "now copy the *.$DOMAIN certificate to edge/certs/wildcard.crt and wildcard.key"
exit 0
