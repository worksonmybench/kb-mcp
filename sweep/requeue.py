"""After a failed push: send the entries behind the unpublished commits back to needs-review.

Usage: requeue.py <repo dir> <branch> <reason>. The sweep's commits carry a `KB-Update: <id>`
line in their message, so the unpushed range names the affected entries.
"""

import re
import subprocess
import sys

sys.path.insert(0, "/opt/kb-sweep")
from inbox import Inbox, InboxError, default_path  # noqa: E402

repo, branch, reason = sys.argv[1:4]
messages = subprocess.run(
    ["git", "-C", repo, "log", "--format=%B", f"origin/{branch}..HEAD"],
    capture_output=True, text=True, check=True,
).stdout
inbox = Inbox(default_path())
for entry_id in sorted(set(re.findall(r"KB-Update: (u-\d{8}-[0-9a-f]{6})", messages))):
    try:
        e = inbox.get(entry_id)
        inbox.mark(entry_id, "needs-review",
                   f"Commits for this in {repo.rsplit('/', 1)[-1]} were not pushed: {reason}. "
                   "Nothing from this run reached Gitea for that repo; resolve to retry.", e["rev"])
        print(f"requeued {entry_id}")
    except InboxError as err:
        print(f"could not requeue {entry_id}: {err}", file=sys.stderr)
