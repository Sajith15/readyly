"""Show the Stash service's recent deploys and its auto-deploy setting.

    python -m scripts.render_status

Useful for confirming that a push to main actually triggered a build.
"""
from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

API_ROOT = "https://api.render.com/v1"
SERVICE_NAME = "stash"


def main() -> int:
    load_dotenv()
    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    if not api_key:
        print("RENDER_API_KEY is not set.", file=sys.stderr)
        return 1

    client = httpx.Client(
        base_url=API_ROOT,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60.0,
    )
    try:
        owner_id = client.get("/owners").json()[0]["owner"]["id"]
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
            print(f"No service named {SERVICE_NAME!r}.", file=sys.stderr)
            return 1

        print(f"service:    {service['id']}")
        print(f"url:        {service.get('serviceDetails', {}).get('url')}")
        print(f"repo:       {service.get('repo')} ({service.get('branch')})")
        print(f"autoDeploy: {service.get('autoDeploy')}")
        print("\nrecent deploys (newest first):")
        deploys = client.get(
            f"/services/{service['id']}/deploys", params={"limit": 5}
        ).json()
        for item in deploys:
            deploy = item.get("deploy", item)
            commit = (deploy.get("commit") or {}).get("id", "")[:8] or "-"
            trigger = deploy.get("trigger") or "-"
            print(
                f"  {deploy['id']}  {deploy['status']:<18} {commit}  "
                f"trigger={trigger}  {deploy.get('createdAt', '')[:19]}"
            )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
