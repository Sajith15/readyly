"""Print recent Render logs for the Stash service.

    python -m scripts.render_logs build     # build output (default)
    python -m scripts.render_logs app       # runtime output

Finds the service by name, so it keeps working across redeploys. Needs
RENDER_API_KEY in the environment or .env.
"""
from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

API_ROOT = "https://api.render.com/v1"
SERVICE_NAME = "stash"
DEFAULT_LIMIT = 150


def main(argv: list[str]) -> int:
    load_dotenv()
    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    if not api_key:
        print("RENDER_API_KEY is not set.", file=sys.stderr)
        return 1

    log_type = argv[1] if len(argv) > 1 else "build"
    limit = int(argv[2]) if len(argv) > 2 else DEFAULT_LIMIT

    client = httpx.Client(
        base_url=API_ROOT,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60.0,
    )
    try:
        owners = client.get("/owners").json()
        owner_id = owners[0]["owner"]["id"]

        services = client.get(
            "/services", params={"name": SERVICE_NAME, "ownerId": owner_id, "limit": 20}
        ).json()
        service = next(
            (
                item.get("service", item)
                for item in services
                if item.get("service", item).get("name") == SERVICE_NAME
            ),
            None,
        )
        if service is None:
            print(f"No service named {SERVICE_NAME!r} in this workspace.", file=sys.stderr)
            return 1

        response = client.get(
            "/logs",
            params={
                "ownerId": owner_id,
                "resource": service["id"],
                "type": log_type,
                "limit": limit,
                "direction": "backward",
            },
        )
        if response.status_code >= 400:
            print(f"{response.status_code}: {response.text[:500]}", file=sys.stderr)
            return 1

        entries = response.json().get("logs", [])
        # 'backward' returns newest first; flip so it reads like a terminal.
        for entry in reversed(entries):
            timestamp = (entry.get("timestamp") or "")[:19]
            # Windows consoles are cp1252; drop anything they cannot render
            # rather than dying halfway through a stack trace.
            message = (entry.get("message") or "").encode(
                sys.stdout.encoding or "utf-8", errors="replace"
            ).decode(sys.stdout.encoding or "utf-8", errors="replace")
            print(f"{timestamp} {message}")

        print(f"\n[{len(entries)} {log_type} log lines for {service['id']}]")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
