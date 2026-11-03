"""Print a Claude Code JSON result as the sweep's summary plus one `kb-sweep: cost:` line.

The cost is Claude Code's own client-side estimate at list prices, not a bill. On a subscription
token it's what the work would have cost on the API. Reporting never fails the run: anything
unexpected in the file is printed as is.
"""

import json
import sys


def main(path, pending):
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    try:
        data = json.loads(raw)
        # Some versions print the whole event list; the result is its last element.
        result = data[-1] if isinstance(data, list) else data
        if not isinstance(result, dict):
            raise ValueError
    except (ValueError, IndexError):
        print(raw)
        print("kb-sweep: cost: unavailable, no JSON result (timed out or killed?)")
        return
    if result.get("result"):
        print(result["result"])
    models = {}
    for name, usage in (result.get("modelUsage") or {}).items():
        models[name] = {
            "est_cost_usd": usage.get("costUSD"),
            "input": usage.get("inputTokens"),
            "output": usage.get("outputTokens"),
            "cache_read": usage.get("cacheReadInputTokens"),
            "cache_write": usage.get("cacheCreationInputTokens"),
        }
    print("kb-sweep: cost: " + json.dumps({
        "pending": pending,
        "est_cost_usd": result.get("total_cost_usd"),
        "turns": result.get("num_turns"),
        "duration_ms": result.get("duration_ms"),
        "subtype": result.get("subtype"),
        "is_error": result.get("is_error"),
        "models": models,
    }))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
