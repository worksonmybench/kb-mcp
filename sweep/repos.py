"""Print `full_name<TAB>default_branch` for every Gitea repo carrying the KB topic.

The same discovery rule as kb-mcp's store (exact topic match, not archived, not empty), so the
sweep writes to exactly the repos the KB serves.
"""

import json
import os
import urllib.parse
import urllib.request

base = os.environ["GITEA_URL"].rstrip("/")
topic = os.environ.get("KB_TOPIC", "kb")


def get(path, **params):
    url = f"{base}/api/v1{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


page = 1
while True:
    batch = get("/repos/search", q=topic, topic="true", limit=50, page=page).get("data", [])
    for repo in batch:
        topics = repo.get("topics")
        if topics is None:
            topics = get(f"/repos/{repo['full_name']}/topics").get("topics", [])
        if topic in topics and not repo.get("archived") and not repo.get("empty"):
            print(f"{repo['full_name']}\t{repo.get('default_branch') or 'main'}")
    if len(batch) < 50:
        break
    page += 1
