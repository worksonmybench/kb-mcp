#!/bin/sh
# Confine the kb-mcp container (bridge br-kb, see docker-compose.yml) on the Docker host.
#  - inbound:  only the reverse proxy may open connections to it
#  - outbound: Gitea via the proxy (port 80), DNS to DNS_SERVERS, and the internet
#              (GitHub OAuth). Every other private/LAN address is dropped, as are the
#              host's own services (sshd, other containers' host ports).
# Also confines the sweep containers (bridge br-kbsweep):
#  - inbound:  nothing
#  - outbound: Gitea via the proxy, DNS to DNS_SERVERS, and Anthropic's API range only. No general
#              internet: the model there reads untrusted text, so it gets no exfiltration path.
# Idempotent: rebuilds its own chains each run. Run at boot after docker (see kb-mcp-firewall.service).
set -eu
cd "$(dirname "$0")/.."
. ./.env.firewall
: "${PROXY_IP:?set PROXY_IP in .env.firewall}"
: "${DNS_SERVERS:?set DNS_SERVERS in .env.firewall}"
BR=br-kb
SWEEP=br-kbsweep
ANTHROPIC=160.79.104.0/21   # api.anthropic.com; https://platform.claude.com/docs/en/api/ip-addresses

ipt() { iptables -w "$@"; }

for c in KB-FWD KB-IN; do ipt -N "$c" 2>/dev/null || ipt -F "$c"; done
ipt -N DOCKER-USER 2>/dev/null || true

# --- forwarded traffic (container <-> anything not on this host) ---
ipt -A KB-FWD -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
# inbound to the container: the proxy only
ipt -A KB-FWD -o "$BR" ! -s "$PROXY_IP" -m conntrack --ctstate NEW -j DROP
ipt -A KB-FWD -o "$BR" -j RETURN
# outbound from the container
ipt -A KB-FWD -i "$BR" -d "$PROXY_IP" -p tcp --dport 80 -j RETURN
for ns in $DNS_SERVERS; do
  ipt -A KB-FWD -i "$BR" -d "$ns" -p udp --dport 53 -j RETURN
  ipt -A KB-FWD -i "$BR" -d "$ns" -p tcp --dport 53 -j RETURN
done
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
  ipt -A KB-FWD -i "$BR" -d "$net" -j DROP
done
# sweep containers
ipt -A KB-FWD -o "$SWEEP" -m conntrack --ctstate NEW -j DROP
ipt -A KB-FWD -i "$SWEEP" -d "$PROXY_IP" -p tcp --dport 80 -j RETURN
for ns in $DNS_SERVERS; do
  ipt -A KB-FWD -i "$SWEEP" -d "$ns" -p udp --dport 53 -j RETURN
  ipt -A KB-FWD -i "$SWEEP" -d "$ns" -p tcp --dport 53 -j RETURN
done
ipt -A KB-FWD -i "$SWEEP" -d "$ANTHROPIC" -p tcp --dport 443 -j RETURN
ipt -A KB-FWD -i "$SWEEP" -j DROP
ipt -A KB-FWD -j RETURN   # public internet allowed (kb-mcp: GitHub OAuth)

# --- traffic to the Docker host itself (INPUT): DNS only, for a resolver running on the host ---
ipt -A KB-IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ipt -A KB-IN -p udp --dport 53 -j ACCEPT
ipt -A KB-IN -p tcp --dport 53 -j ACCEPT
ipt -A KB-IN -j DROP

ipt -C DOCKER-USER -j KB-FWD 2>/dev/null || ipt -I DOCKER-USER 1 -j KB-FWD
for br in "$BR" "$SWEEP"; do
  ipt -C INPUT -i "$br" -j KB-IN 2>/dev/null || ipt -I INPUT 1 -i "$br" -j KB-IN
done
echo "kb-mcp firewall applied"
