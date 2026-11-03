#!/bin/sh
# KB-inbox sweep, run every 15 min by kb-sweep.timer on the Docker host (see README.md).
# Output goes to the journal: journalctl -t kb-sweep
#  1. Nothing pending -> no containers start at all.
#  2. kb-sweep (Claude Code, no Gitea write access) files the updates and commits locally.
#  3. kb-sweep-push pushes those commits with the bot's token.
#  4. Push monitor to Uptime Kuma: DOWN while any entry needs review or a step failed, and,
#     being a push monitor, also DOWN if this timer stops running.
set -u
PATH=/snap/bin:/usr/local/bin:/usr/bin:/bin
cd "$(dirname "$0")/.."
KUMA_PUSH_URL=$(sed -n 's/^KUMA_PUSH_URL=//p' .env.push 2>/dev/null)
compose() { docker compose --profile sweep "$@"; }
count() { docker exec kb-mcp kb-inbox count --status "$1"; }
pending_ids() { docker exec kb-mcp kb-inbox list --status pending --limit 200 \
    | python3 -c 'import json,sys; print(" ".join(e["id"] for e in json.load(sys.stdin)))'; }

problem=""
if ! pending=$(count pending); then
    problem="inbox unreadable (is kb-mcp running?)"; pending=0
fi
if [ "$pending" -gt 0 ]; then
    before=$(pending_ids)
    compose run --rm -T kb-sweep || problem="sweep failed"
    compose run --rm -T kb-sweep-push || problem="${problem:+$problem, }push failed"
    # Every entry the sweep saw should now be done or needs-review. One still pending means the
    # run stalled on it (e.g. denied tools on 2026-10-22/27, which left Kuma green for an hour).
    after=" $(pending_ids) "
    stuck=0
    for id in $before; do case "$after" in *" $id "*) stuck=$((stuck + 1)) ;; esac; done
    [ "$stuck" -gt 0 ] && problem="${problem:+$problem, }$stuck entry(s) still pending after a run"
fi
review=$(count needs-review) || review="?"
left=$(count pending) || left="?"

status=up
msg="pending $left, needs-review $review"
if [ "$review" != 0 ] || [ -n "$problem" ]; then
    status=down
    msg="$msg${problem:+; $problem (journalctl -t kb-sweep)}"
fi
echo "kb-sweep: $status: $msg"
if [ -n "$KUMA_PUSH_URL" ]; then
    enc=$(printf '%s' "$msg" | sed 's/%/%25/g; s/ /%20/g; s/;/%3B/g; s/,/%2C/g; s/(/%28/g; s/)/%29/g; s/?/%3F/g; s/&/%26/g')
    curl -fsS -m 10 -o /dev/null "$KUMA_PUSH_URL?status=$status&msg=$enc&ping=" \
        || echo "kb-sweep: Uptime Kuma push failed" >&2
fi
[ -z "$problem" ]
