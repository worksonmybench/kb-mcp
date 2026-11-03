#!/bin/sh
# Run on the Docker host after `docker compose up -d` (and, for the sweep half, after
# `docker compose --profile sweep build kb-sweep`). Each line should print the expected result.
probe='
import socket,sys; h,p,x=sys.argv[1],int(sys.argv[2]),sys.argv[3]; s=socket.socket(); s.settimeout(4)
try: s.connect((h,p)); print("OPEN    %s:%d  (expect %s)" % (h,p,x))
except Exception: print("BLOCKED %s:%d  (expect %s)" % (h,p,x))'
cd "$(dirname "$0")/.."
. ./.env.firewall

blocked() { for target in $BLOCKED_TARGETS; do t "${target%:*}" "${target##*:}" BLOCKED; done; }

echo "--- kb-mcp (br-kb)"
t() { docker exec kb-mcp python -c "$probe" "$1" "$2" "$3"; }
t "$PROXY_IP" 80 OPEN         # Gitea via the proxy
t 140.82.112.3 443 OPEN       # github.com
blocked

echo "--- kb-sweep (br-kbsweep)"
t() { docker compose --profile sweep run --rm -T --no-deps kb-sweep python3 -c "$probe" "$1" "$2" "$3" 2>/dev/null; }
t "$PROXY_IP" 80 OPEN         # Gitea via the proxy
t 160.79.104.10 443 OPEN      # api.anthropic.com
t 140.82.112.3 443 BLOCKED    # github.com: no general internet
t 1.1.1.1 443 BLOCKED         # ditto
blocked
