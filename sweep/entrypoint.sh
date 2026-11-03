#!/bin/sh
# kb-sweep container entrypoint.
#   sweep: fresh clones of the KB repos into /work, then Claude Code files the pending updates
#          (edits + local commits + `kb-inbox mark`). No Gitea write credential in here.
#   push:  pushes what the sweep committed. Has the bot's Gitea token; runs no model.
set -eu
RUN=/work/.run
TAB=$(printf '\t')

sweep() {
    pending=$(kb-inbox count --status pending)
    if [ "$pending" -eq 0 ]; then
        exit 0
    fi
    [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ] || {
        echo "kb-sweep: no CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY (.env.sweep)" >&2; exit 2; }
    [ -n "${GITEA_URL:-}" ] || { echo "kb-sweep: no GITEA_URL (.env.sweep)" >&2; exit 2; }
    echo "kb-sweep: $pending pending"
    find /work -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    mkdir -p "$RUN"
    python3 /opt/kb-sweep/repos.py > "$RUN/repos.tsv"
    while IFS="$TAB" read -r full branch; do
        git clone -q --depth 50 --branch "$branch" "$GITEA_URL/$full.git" "/work/${full#*/}"
    done < "$RUN/repos.tsv"
    cd /work
    # The hard cap keeps a wedged run from overlapping the next timer tick by much.
    # --tools pins the tool set: without it, 2.1.286 started this session with no Grep/Glob,
    # the model searched with Bash grep/find (not allowlisted), got denied in dontAsk mode and
    # gave up thinking Bash was off (2026-10-22 and 10-27). It also drops tools the sweep never
    # needs (Task, Cron*, RemoteTrigger, Skill, ...).
    # JSON output so report.py can log the run's estimated cost next to its summary.
    rc=0
    timeout 1800 claude -p "$(cat /opt/kb-sweep/prompt.md)" \
        --settings /opt/kb-sweep/settings.json \
        --tools "Bash,Read,Edit,Write,Grep,Glob" \
        --max-turns 300 \
        --output-format json \
        ${SWEEP_MODEL:+--model "$SWEEP_MODEL"} > "$RUN/result.json" || rc=$?
    python3 /opt/kb-sweep/report.py "$RUN/result.json" "$pending" || true
    return "$rc"
}

push() {
    [ -f "$RUN/repos.tsv" ] || exit 0
    [ -n "${GITEA_TOKEN:-}" ] || { echo "kb-sweep-push: no GITEA_TOKEN (.env.push)" >&2; exit 2; }
    rc=0
    while IFS="$TAB" read -r full branch; do
        dir=/work/${full#*/}
        [ -d "$dir/.git" ] || continue
        ahead=$(git -C "$dir" rev-list --count "origin/$branch..HEAD")
        [ "$ahead" -gt 0 ] || continue
        # Belt and braces: the model's container holds an Anthropic token. Never publish one.
        if git -C "$dir" log -p "origin/$branch..HEAD" | grep -q 'sk-ant-'; then
            echo "kb-sweep-push: $full: commits contain an Anthropic token; NOT pushing" >&2
            python3 /opt/kb-sweep/requeue.py "$dir" "$branch" "a commit contained a credential, so nothing was pushed" || true
            rc=1; continue
        fi
        if git -C "$dir" push -q origin "HEAD:$branch" 2>/dev/null \
           || { git -C "$dir" pull -q --rebase origin "$branch" && git -C "$dir" push -q origin "HEAD:$branch"; }; then
            echo "kb-sweep-push: $full: pushed $ahead commit(s), now $(git -C "$dir" rev-parse --short HEAD)"
        else
            git -C "$dir" rebase --abort 2>/dev/null || true
            echo "kb-sweep-push: $full: push failed" >&2
            python3 /opt/kb-sweep/requeue.py "$dir" "$branch" "the push to Gitea failed (likely a conflicting edit)" || true
            rc=1
        fi
    done < "$RUN/repos.tsv"
    kb-inbox archive --days 30 >/dev/null
    exit $rc
}

case "${1:-sweep}" in
    sweep) sweep ;;
    push) push ;;
    *) exec "$@" ;;
esac
