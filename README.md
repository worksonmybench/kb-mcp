# kb-mcp

An MCP server that turns every Gitea repo tagged with the topic **`kb`** into a single
searchable knowledge base. It's read-only toward Gitea; the one write path is an inbox of
KB updates that a scheduled sweep files into the repos (see **KB inbox and sweep**). It's designed to be served publicly through a
Cloudflare Tunnel, so it works as a claude.ai custom connector (and therefore in the
Claude apps and Claude Code) from anywhere.

- **Choosing repos:** in Gitea, open the repo → **Manage topics** → add `kb`. It's picked up on
  the next sync (5 min, or call the `sync_now` tool). Removing the topic drops the repo. Code
  repos simply don't get the tag.
- **Auth:** OAuth through GitHub. Only the numeric GitHub user IDs in `ALLOWED_GITHUB_IDS` get
  in (IDs, because a username can be renamed and re-registered), and only the claude.ai/claude.com
  callback and localhost (Claude Code) may complete the flow. See **Hardening** below.
- **Tools:** `search` (ranked full-text by heading section), `grep` (regex, for IPs/entity
  IDs), `read_file`, `list_files`, `list_sources`, `recent_changes`, `sync_now`, plus the
  inbox tools `capture_update`, `list_updates`, `amend_update`. Nothing in this
  container writes to Gitea.

```
claude.ai ──HTTPS──► Cloudflare edge ──tunnel──► cloudflared
                                                    └─► reverse proxy :80 ──► Docker host :8765 (kb-mcp)
kb-mcp ──every 5 min──► Gitea: list repos with topic kb → git fetch → SQLite FTS5 reindex
```

## KB inbox and sweep

Things said in a chat (web, mobile, desktop) get into the docs without copy-paste. An
"update" is anything the docs should reflect: a decision or plan, an event ("replaced the RO
filter"), a purchase or install ("new fridge in the garage"), a config change, a correction.

```
chat ──capture_update──► kb-inbox volume: updates.jsonl ◄── amend_update (resolutions)
                                   │
   kb-sweep.timer (15 min) ── nothing pending? stop.
                                   ▼
   kb-sweep container: fresh clones → Claude Code edits + commits (no push credential)
                                   ▼
   kb-sweep-push container: git push as the kb-sweep bot (no model)
                                   ▼
   Uptime Kuma push monitor: DOWN while anything is needs-review, a step failed, or the timer stopped
```

- **Chat side** (MCP tools): `capture_update(topic, content, context)` appends an entry
  with a generated id; `list_updates(status)` reads; `amend_update(id, resolution, note,
  status)` attaches a resolution (which re-queues it as `pending`), appends a note, moves it
  between `pending` and `needs-review`, or sets `discarded`, which closes it at once (no sweep
  run, no doc change; the note is the reason). Chat can't edit an entry's content, delete,
  mark `done`, or name a repo or file.
- **Entry fields:** `id`, `rev`, `timestamp`, `updated`, `topic` (search hint), `content`,
  `context`, `status` (`pending` | `needs-review` | `done` | `discarded`), `resolution`, `notes[]`, `log[]`
  (what each sweep did: commits, or why it got stuck).
- **Storage:** `kb_mcp/inbox.py`, standard library only. JSONL on its own named volume
  `kb-inbox` (mounted at `/data/inbox` in kb-mcp and `/inbox` in the sweep, so the sweep never
  sees `kb-data`'s OAuth store). Every change takes an `flock` and rewrites the file atomically.
  Each entry's `rev` bumps on change; the sweep's `mark` must quote the rev it worked from, so
  a resolution that lands mid-run is never overwritten. Limits: content 20,000 chars, context 8,000, 200 open
  entries, 8 MB file. Done and discarded entries older than 30 days move to `updates-archive.jsonl`.
- **CLI** (both images): `kb-inbox list|show|mark|count|archive`, e.g.
  `docker exec kb-mcp kb-inbox list --status needs-review`. `mark` is sweep-only and is never
  exposed over MCP.

**Security posture.** kb-mcp gains exactly one write path: the inbox file on a local volume.
No new credentials, ports or egress. The sweep is where the risk sits, because a model with
commit rights reads text that came from chat and from the repos. So:

| Control | Where |
|---|---|
| The model's container has no Gitea write credential; a separate model-free container pushes | compose: `kb-sweep` / `kb-sweep-push` |
| Egress from the sweep: Gitea via the proxy, DNS, Anthropic's API range. No other internet, no LAN | `deploy/kb-mcp-firewall.sh` (`br-kbsweep`) |
| Claude Code tools: read/edit inside `/work` (Edit rules cover Write too), `git status/diff/log/show/add/commit/restore/reset`, `kb-inbox list/show/mark`. No web, no push, no `.git/` edits, no reading `/proc` | `sweep/settings.json` |
| Git hooks disabled; read-only rootfs; all caps dropped; non-root | `sweep/gitconfig`, compose |
| The push step refuses commits containing `sk-ant-` and requeues their entries | `sweep/entrypoint.sh` |
| The bot account can write only to the repos it's a collaborator on; branch protection (no force-push) on `main` keeps history recoverable | Gitea |
| The prompt treats entries and files as data and marks anything odd `needs-review` | `sweep/prompt.md` |

**Sweep setup (Docker host, once):**

1. **Gitea bot:** Site Administration → User Accounts → create `kb-sweep` (no admin). On each
   `kb`-tagged repo: Settings → Collaborators → add `kb-sweep` with **Write**. Also on each repo:
   Settings → Branches → protect `main`, with force-push disabled and pushes allowed. Then log in
   as `kb-sweep` → Settings → Applications → token with scope `write:repository`.
2. **Claude token:** `claude setup-token` on a machine with Claude Code. It prints a token,
   valid for about a year, that bills the subscription. Alternatively use an `ANTHROPIC_API_KEY`.
3. **Uptime Kuma:** add a **Push** monitor "KB inbox sweep" with a heartbeat interval of 1200 s
   (15 min plus slack). Copy its URL **without** the `?status=...` query; from the host,
   `http://127.0.0.1:3001/api/push/<token>` works.
4. On the Docker host:
   ```bash
   cd /root/kb-mcp && git pull
   cp .env.sweep.example .env.sweep && cp .env.push.example .env.push && chmod 600 .env.sweep .env.push
   # fill in CLAUDE_CODE_OAUTH_TOKEN, GITEA_URL, GITEA_TOKEN, KUMA_PUSH_URL
   docker compose up -d --build                  # kb-mcp with the inbox volume
   docker compose --profile sweep build kb-sweep
   systemctl restart kb-mcp-firewall             # adds the br-kbsweep rules
   sh deploy/verify-firewall.sh
   cp deploy/kb-sweep.service deploy/kb-sweep.timer /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now kb-sweep.timer
   ```
5. **Test:** capture an update from a chat, then `systemctl start kb-sweep && journalctl -t kb-sweep -n 50`.

**Measuring cost:** each run logs one line with Claude Code's estimate, turn count and per-model
tokens. It's a client-side figure at list prices, not a bill: on a subscription token, it's what
the work would have cost on the API. For a billed figure, use a dedicated `ANTHROPIC_API_KEY` in
its own Console workspace. To total the runs:

```bash
journalctl -t kb-sweep -o cat --grep '^kb-sweep: cost: \{' | sed 's/^kb-sweep: cost: //' \
  | jq -s '{runs: length, entries: (map(.pending) | add), est_cost_usd: (map(.est_cost_usd // 0) | add)}'
```

**Updates:** after `git pull`, also run `docker compose --profile sweep build kb-sweep`. The
`up -d --build` step doesn't rebuild profile-only services. Bump `CLAUDE_CODE_VERSION` in
`sweep/Dockerfile` the same deliberate way as the fastmcp pin. If Anthropic's API range
changes, update `ANTHROPIC` in the firewall script together with the WAF rule.

## How long a login lasts

GitHub OAuth-app tokens don't expire, so after one login the server issues a token valid for
**1 year**. You log in again only if:

- `JWT_SIGNING_KEY` changes, or the `kb-data` volume is deleted (it holds the token store);
- you revoke the app at github.com → Settings → Applications → Authorized OAuth Apps; or
- the client discards its own token, e.g. you remove and re-add the connector.

Rebuilding or restarting the container is fine.

## Deploy

This assumes a Docker host, a reverse proxy in front of it (these steps use Nginx Proxy Manager)
that also proxies Gitea on port 80, and a Cloudflare Tunnel. It only talks to Gitea's API, so other
Git hosts need changes in `kb_mcp/store.py` and `sweep/repos.py`. The units expect the repo at
`/root/kb-mcp`; edit `deploy/kb-mcp-firewall.service` and `deploy/kb-sweep.service` if you put it
elsewhere.

1. **GitHub OAuth app:** github.com → Settings → Developer settings → OAuth Apps → New.
   Homepage `https://kb.example.com`, callback `https://kb.example.com/auth/callback`.
   Copy the client ID and generate a secret.
2. **On the Docker host:**
   ```bash
   git clone <this repo> /root/kb-mcp
   cd /root/kb-mcp && cp .env.example .env && cp .env.firewall.example .env.firewall
   # .env: PUBLIC_BASE_URL, GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET, ALLOWED_GITHUB_IDS,
   #   GITEA_URL, and JWT_SIGNING_KEY=$(openssl rand -hex 32)
   # .env.firewall: PROXY_IP, DNS_SERVERS, BLOCKED_TARGETS
   docker compose up -d --build
   cp deploy/kb-mcp-firewall.service /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now kb-mcp-firewall
   sh deploy/verify-firewall.sh       # every line should match its "expect"
   curl -s localhost:8765/healthz
   ```
3. **Reverse proxy:** proxy `kb.example.com` to the Docker host on port `8765`, Websockets on.
   Leave "Force SSL" off, or the Tunnel's plain HTTP redirects in a loop.
   In **Advanced**, paste the following so streamed MCP responses aren't buffered:
   ```nginx
   proxy_buffering off;
   proxy_read_timeout 3600s;
   ```
4. **Cloudflare Zero Trust** → Networks → Tunnels → the tunnel → Published application
   routes → add `kb.example.com` → `http://<proxy IP>:80`.
   No Cloudflare Access on it: claude.ai's servers can't get past an Access login. Add the
   WAF rules under **Hardening** instead.
5. **Gitea:** add the `kb` topic to your doc repos. Press Enter, then Save, or it isn't stored.

## Hardening

The endpoint is public, so it is layered:

| Layer | What | Where |
|---|---|---|
| Edge IP filter | Only Anthropic's outbound range can reach the API paths | Cloudflare WAF |
| Login | GitHub OAuth, allowlisted user ID, consent page every authorization, redirect-URI allowlist | app |
| Errors | `mask_error_details`; `/healthz` returns only `{"ok": ...}` | app |
| Container | read-only rootfs, non-root, all caps dropped, mem/pid limits (no-new-privileges breaks exec under snap Docker) | compose |
| Network | Container can reach only the proxy on port 80, your DNS servers and the internet; only the proxy can reach it | `deploy/kb-mcp-firewall.sh` |
| Account | GitHub 2FA (passkey or TOTP); the app requests only `read:user` | github.com |

**Cloudflare WAF custom rule** (Security → Security rules → Custom rules), action **Block**:

```
(http.host eq "kb.example.com"
 and not ip.src in {160.79.104.0/21}
 and not http.request.uri.path in {"/authorize" "/consent" "/auth/callback"})
```

- `160.79.104.0/21` is Anthropic's published outbound range
  (https://platform.claude.com/docs/en/api/ip-addresses). claude.ai's servers, and so web, desktop
  and mobile, call `/mcp`, `/token`, `/register` and `/.well-known/*` from it. If Anthropic
  changes the range, the connector breaks until this rule is updated.
- `/authorize`, `/consent` and `/auth/callback` stay open because your own browser hits them
  during login, from wherever you are.
- The range is shared by every Anthropic customer, so it filters out scanners, not other
  claude.ai users. OAuth plus the ID allowlist does that.
- Claude Code calls from your own machine, so it gets blocked here. Use split-horizon DNS
  instead: a local DNS entry pointing `kb.example.com` at the proxy, plus a Let's Encrypt
  DNS-01 certificate on the proxy host. On the
  LAN or VPN, Claude Code then reaches the proxy directly and never touches Cloudflare.
- Bot Fight Mode / "Block AI bots" must not apply to this hostname. If they're on for the zone,
  add a **Skip** rule for `ip.src in {160.79.104.0/21}` above them.

**Rate limiting:** Cloudflare's Free plan allows one rule, and this setup doesn't need it:
the WAF rule already keeps the internet off `/token` and `/register`, and `/authorize` only
redirects to GitHub, which protects its own login.

**Monitoring:** rejected logins are logged as `rejected GitHub user id=...` (`docker logs kb-mcp`). Point an Uptime Kuma HTTP monitor at
`https://kb.example.com/healthz` and use a keyword check for `"ok":true`, because a failed sync
still returns HTTP 200. With the split-horizon entry, that resolves to the proxy inside the LAN.
Don't monitor the Docker host's port 8765 directly: the firewall only admits the proxy, so it
would read as down.

**Updates:** `fastmcp` is pinned in `pyproject.toml`. Watch its releases for auth fixes and
rebuild with `docker compose up -d --build` after bumping the pin. Rebuilding also picks up
base-image patches.

## Connect

- **claude.ai** (applies to web, desktop, and mobile): Settings → Connectors → Add custom
  connector → `https://kb.example.com/mcp` → log in with GitHub.
- **Claude Code** (needs the split-horizon DNS entry above; works on the LAN or VPN):
  ```bash
  claude mcp add --transport http --scope user kb https://kb.example.com/mcp
  ```
  then run `/mcp` in a session to complete the login.

## Update

```bash
cd /root/kb-mcp && git pull && docker compose up -d --build
```

## Local test

`KB_AUTH_DISABLED=1` skips OAuth (never set it on the deployed instance):

```bash
pip install -e . && GITEA_URL=http://gitea.example.internal DATA_DIR=./data KB_AUTH_DISABLED=1 kb-mcp
```

## License

MIT. See `LICENSE`.
