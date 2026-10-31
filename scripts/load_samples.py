"""Posts the 40 brief samples to a running deployment, exactly as the email gateway would.

    INGEST_KEY=... uv run python scripts/load_samples.py https://riverbend-api-xxxx.run.app

Re-running is safe: every message keeps its message_id, so repeats are acknowledged as
duplicates and change nothing. Follow-up questions are left for real customers to answer
(their reply tokens are never exposed through the staff API).
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

import httpx

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples.json"


def main() -> int:
    if len(sys.argv) != 2 or not os.environ.get("INGEST_KEY"):
        print(__doc__)
        return 2
    base = sys.argv[1].rstrip("/")
    data = json.loads(SAMPLES.read_text())
    statuses: Counter[int] = Counter()
    with httpx.Client(timeout=30) as client:
        for m in data["messages"]:
            r = client.post(
                f"{base}/v1/shops/{m['shop_id']}/inbound-messages",
                headers={"X-Ingest-Key": os.environ["INGEST_KEY"]},
                json={
                    "message_id": m["message_id"],
                    "channel": m["channel"],
                    "received_at": f"{data['date']}T{m['received']}:00",
                    "body": m["body"],
                },
            )
            statuses[r.status_code] += 1
            if r.status_code >= 300 and r.status_code != 409:
                print(f"{m['message_id']}: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
    print(f"posted {sum(statuses.values())} messages: {dict(sorted(statuses.items()))}")
    return 0 if set(statuses) <= {200, 202} else 1


if __name__ == "__main__":
    sys.exit(main())
