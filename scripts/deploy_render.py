"""Provision Stash on Render via the public API.

Creates (or reuses) a free Postgres instance and a free web service wired to
this repo, sets the environment variables from the local .env, waits for the
first deploy, and prints the public URL.

    python -m scripts.deploy_render https://github.com/you/stash

Idempotent: re-running finds the existing database and service by name, updates
their environment variables, and redeploys rather than creating duplicates.

Requires RENDER_API_KEY in the environment or .env. The repo must be reachable
by the Render workspace: link your GitHub account in the Render dashboard first,
otherwise auto-deploy on push will not be configured.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx
from dotenv import load_dotenv

API_ROOT = "https://api.render.com/v1"

DB_NAME = "stash-db"
SERVICE_NAME = "stash"
REGION = "oregon"
POSTGRES_VERSION = "16"
PYTHON_VERSION = "3.12.7"

BUILD_COMMAND = "pip install -r requirements.txt && python -m scripts.init_db"
START_COMMAND = "uvicorn app.main:app --host 0.0.0.0 --port $PORT"

DEPLOY_SETTLED = {"live", "build_failed", "update_failed", "canceled", "deactivated"}


class RenderError(RuntimeError):
    pass


class Render:
    def __init__(self, api_key: str) -> None:
        self._client = httpx.Client(
            base_url=API_ROOT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=60.0,
        )

    def request(self, method: str, path: str, **kwargs) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RenderError(
                f"{method} {path} -> {response.status_code}: {response.text[:600]}"
            )
        if not response.content:
            return None
        return response.json()

    def close(self) -> None:
        self._client.close()


def step(message: str) -> None:
    print(f"\n==> {message}")


def detail(message: str) -> None:
    print(f"    {message}")


# --- workspace ------------------------------------------------------------


def resolve_owner(render: Render) -> str:
    owners = render.request("GET", "/owners")
    entries = [item["owner"] for item in owners]
    if not entries:
        raise RenderError("This API key has no workspaces.")
    if len(entries) > 1:
        detail("Multiple workspaces found; using the first:")
        for entry in entries:
            detail(f"  - {entry['name']} ({entry['id']})")
    owner = entries[0]
    detail(f"Workspace: {owner['name']} ({owner['id']})")
    return owner["id"]


# --- database -------------------------------------------------------------


def find_postgres(render: Render, owner_id: str) -> dict | None:
    results = render.request(
        "GET", "/postgres", params={"name": DB_NAME, "ownerId": owner_id, "limit": 20}
    )
    for item in results or []:
        record = item.get("postgres", item)
        if record.get("name") == DB_NAME:
            return record
    return None


def ensure_postgres(render: Render, owner_id: str) -> dict:
    existing = find_postgres(render, owner_id)
    if existing:
        detail(f"Reusing existing database {DB_NAME} ({existing['id']})")
        return existing

    detail(f"Creating free Postgres {DB_NAME} in {REGION}...")
    return render.request(
        "POST",
        "/postgres",
        json={
            "name": DB_NAME,
            "ownerId": owner_id,
            "plan": "free",
            "version": POSTGRES_VERSION,
            "region": REGION,
            "databaseName": "stash",
            "databaseUser": "stash",
        },
    )


def wait_for_database(render: Render, postgres_id: str, timeout: int = 600) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        record = render.request("GET", f"/postgres/{postgres_id}")
        status = record.get("status")
        if status != last:
            detail(f"Database status: {status}")
            last = status
        if status == "available":
            return record
        if status in {"unavailable", "recovery_failed"}:
            raise RenderError(f"Database entered {status!r}.")
        time.sleep(10)
    raise RenderError("Timed out waiting for the database to become available.")


# --- service --------------------------------------------------------------


def find_service(render: Render, owner_id: str) -> dict | None:
    results = render.request(
        "GET",
        "/services",
        params={"name": SERVICE_NAME, "ownerId": owner_id, "limit": 20},
    )
    for item in results or []:
        record = item.get("service", item)
        if record.get("name") == SERVICE_NAME:
            return record
    return None


def build_env_vars(database_url: str, base_url: str | None) -> list[dict[str, str]]:
    """Assemble the service environment from the local .env.

    Secrets are read from the developer's .env and pushed straight to Render;
    they are never written to a file in the repo.
    """
    required = {
        "AI_API_KEY": os.environ.get("AI_API_KEY", "").strip(),
        "RESEND_API_KEY": os.environ.get("RESEND_API_KEY", "").strip(),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RenderError(
            f"Missing {', '.join(missing)} locally. Add them to .env before deploying."
        )

    env = {
        "DATABASE_URL": database_url,
        "APP_ENV": "production",
        "PYTHON_VERSION": PYTHON_VERSION,
        "AI_API_KEY": required["AI_API_KEY"],
        "AI_BASE_URL": os.environ.get(
            "AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
        ).strip(),
        "AI_MODEL": os.environ.get("AI_MODEL", "gemini-2.5-flash").strip(),
        "RESEND_API_KEY": required["RESEND_API_KEY"],
        "EMAIL_FROM": os.environ.get(
            "EMAIL_FROM", "Stash <onboarding@resend.dev>"
        ).strip(),
    }
    if base_url:
        env["BASE_URL"] = base_url
    return [{"key": key, "value": value} for key, value in env.items()]


def create_service(
    render: Render, owner_id: str, repo_url: str, database_url: str
) -> dict:
    detail(f"Creating free web service {SERVICE_NAME} from {repo_url}...")
    created = render.request(
        "POST",
        "/services",
        json={
            "type": "web_service",
            "name": SERVICE_NAME,
            "ownerId": owner_id,
            "repo": repo_url,
            "branch": "main",
            "autoDeploy": "yes",
            "envVars": build_env_vars(database_url, base_url=None),
            "serviceDetails": {
                "runtime": "python",
                "plan": "free",
                "region": REGION,
                "healthCheckPath": "/healthz",
                "envSpecificDetails": {
                    "buildCommand": BUILD_COMMAND,
                    "startCommand": START_COMMAND,
                },
            },
        },
    )
    return created.get("service", created)


def service_url(service: dict) -> str | None:
    return (service.get("serviceDetails") or {}).get("url")


def trigger_deploy(render: Render, service_id: str) -> str:
    """Kick off a deploy explicitly and return its id.

    Updating environment variables through the API does not reliably start a
    build, and polling 'the latest deploy' races against Render creating it, so
    we ask for one and then track that exact id.
    """
    deploy = render.request(
        "POST", f"/services/{service_id}/deploys", json={"clearCache": "do_not_clear"}
    )
    deploy_id = deploy.get("id") or deploy.get("deploy", {}).get("id")
    if not deploy_id:
        raise RenderError(f"Could not read a deploy id from: {deploy}")
    return deploy_id


def wait_for_deploy(
    render: Render, service_id: str, deploy_id: str, timeout: int = 1800
) -> str:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        deploy = render.request("GET", f"/services/{service_id}/deploys/{deploy_id}")
        status = deploy.get("status")
        if status != last:
            detail(f"Deploy status: {status}")
            last = status
        if status in DEPLOY_SETTLED:
            return status
        time.sleep(15)
    raise RenderError("Timed out waiting for the deploy to finish.")


# --- entry point ----------------------------------------------------------


def main(argv: list[str]) -> int:
    load_dotenv()

    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    if not api_key:
        print(
            "RENDER_API_KEY is not set. Add it to .env "
            "(Render Dashboard -> Account Settings -> API Keys).",
            file=sys.stderr,
        )
        return 1

    repo_url = (
        argv[1].strip()
        if len(argv) > 1
        else os.environ.get("GITHUB_REPO_URL", "").strip()
    )
    if not repo_url:
        print(
            "Usage: python -m scripts.deploy_render <github-repo-url>", file=sys.stderr
        )
        return 1
    repo_url = repo_url.removesuffix(".git").rstrip("/")

    render = Render(api_key)
    try:
        step("Resolving workspace")
        owner_id = resolve_owner(render)

        step("Provisioning Postgres")
        database = ensure_postgres(render, owner_id)
        database = wait_for_database(render, database["id"])
        if database.get("expiresAt"):
            detail(f"Note: free database expires {database['expiresAt']}")

        connection = render.request(
            "GET", f"/postgres/{database['id']}/connection-info"
        )
        # Internal, not external: a Render database created via the API has an
        # empty IP allow list, so it refuses public connections outright. The
        # internal host is reachable from both the build and the running
        # service, and keeps the database off the internet entirely.
        database_url = connection["internalConnectionString"]
        detail("Retrieved internal connection string.")

        step("Provisioning web service")
        service = find_service(render, owner_id)
        if service:
            detail(f"Reusing existing service {SERVICE_NAME} ({service['id']})")
        else:
            service = create_service(render, owner_id, repo_url, database_url)
            detail(f"Created service {service['id']}")

        service_id = service["id"]
        url = service_url(service) or f"https://{SERVICE_NAME}.onrender.com"
        detail(f"Public URL: {url}")

        step("Setting environment variables")
        # BASE_URL is only knowable once the service exists, so it is written in
        # a second pass. Updating env vars also triggers a fresh deploy.
        render.request(
            "PUT",
            f"/services/{service_id}/env-vars",
            json=build_env_vars(database_url, base_url=url),
        )
        detail("Environment updated.")

        step("Deploying (the first build takes a few minutes)")
        deploy_id = trigger_deploy(render, service_id)
        detail(f"Deploy {deploy_id} started.")
        status = wait_for_deploy(render, service_id, deploy_id)

        dashboard = f"https://dashboard.render.com/web/{service_id}"
        if status != "live":
            print(f"\nDeploy finished with status {status!r}.")
            print(f"Check the build log: {dashboard}")
            return 1

        print("\n" + "=" * 60)
        print(f"  Live:      {url}")
        print(f"  Dashboard: {dashboard}")
        print("=" * 60)
        return 0
    except RenderError as exc:
        print(f"\nDeploy failed: {exc}", file=sys.stderr)
        return 1
    finally:
        render.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
