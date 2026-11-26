#!/bin/sh
# One image, one role per container. Shared Captain (CAPTAIN_MULTI_TENANT=1): every service
# is a router that serves Stixis and every company by web address.
set -e
run() { exec python -m uvicorn --factory "$1" --host 0.0.0.0 --port "$2" --proxy-headers --forwarded-allow-ips='*'; }
case "$1" in
  init)    exec python -m captain.tenancy migrate-all ;;
  admin)   run captain.tenancy:create_admin_router 8100 ;;
  gateway) run captain.tenancy:create_gateway_router 8000 ;;
  core)    run captain.tenancy:create_core_router 8080 ;;
  mcp)     run captain.tenancy:create_mcp_router 8300 ;;
  worker)  exec python -m captain.workers ;;
  *)       exec "$@" ;;
esac
