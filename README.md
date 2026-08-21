# Stash

Stash is a personal bookmark manager you operate by talking to it. You sign up
with an email and password, then tell an AI co-pilot things like *"Save
https://example.com under tag reading"* or *"What did I stash about Python?"*.
The co-pilot never touches the database itself: it reaches your bookmarks
exclusively through tools published by an **MCP (Model Context Protocol)
server**, which is spawned per request with your user id baked into its
environment. Auth is bcrypt-hashed with server-side sessions, password resets
use single-use time-limited tokens, and transactional email goes out via Resend.

## Architecture

```
Browser
  │  HTTPS
  ▼
FastAPI web service (Render)
  ├── Auth routes ──────────────► Postgres (users, sessions, reset tokens)
  ├── Chat handler (app/chat.py)
  │     │  conversation + tool definitions
  │     ▼
  │   OpenAI  ──── tool_calls ───► MCP bridge (app/mcp_bridge.py)
  │                                     │  stdio JSON-RPC
  │                                     ▼
  │                               MCP server subprocess (mcp_server/server.py)
  │                                     │  SQL always filtered by user_id
  │                                     ▼
  │                               Postgres (bookmarks)
  └── Mailer ──► Resend API ──► inbox
```

**The design rule:** `mcp_server/repository.py` holds every statement against
the `bookmarks` table, and nothing in `app/` imports it. The chat handler has no
database access at all — the only way it can read or write a bookmark is by
dispatching a tool call over MCP.

### How per-user scoping is enforced

The obvious approach — passing `user_id` as a tool argument — is exactly what
prompt injection defeats: if the model can name a user id, a crafted message can
persuade it to name a different one. Stash removes the possibility instead of
policing it:

1. The web app spawns the MCP server with `STASH_USER_ID` in its environment.
2. No tool signature accepts a user id, so the schemas the model sees contain no
   field in which another user could be named.
3. Every SQL statement in the repository takes `user_id` as its first filter,
   read from that pinned environment variable.

A user id is therefore not part of the model's vocabulary. You can verify the
tool schemas contain no user field yourself:

```bash
python -m scripts.check_mcp
```

## Local setup

Requires Python 3.11+ and a Postgres you can reach.

```bash
git clone <this repo>
cd stash

python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env             # then fill in the values below
python -m scripts.init_db        # creates the schema (idempotent)

uvicorn app.main:app --reload
```

Open http://localhost:8000.

Need a throwaway Postgres? `docker run -d --name stash-pg -e POSTGRES_PASSWORD=stash
-e POSTGRES_USER=stash -e POSTGRES_DB=stash -p 55432:5432 postgres:16-alpine`

The app runs without an OpenAI or Resend key — chat returns a clear "not
configured" error, and emails are written to the log instead of being sent — so
you can exercise the auth flows before wiring credentials.

## Verifying it works

Four scripts, none of which need an OpenAI key. All except `check_mcp` need
`DATABASE_URL` and an initialised schema; they create throwaway users and clean
up after themselves.

```bash
python -m scripts.check_mcp        # tool schemas the model sees (no DB needed)
python -m scripts.smoke_test       # auth invariants + cross-user isolation
python -m scripts.http_smoke       # the graded click-through, at HTTP level
python -m scripts.chat_loop_test   # the tool-call loop, against a stubbed LLM
```

`chat_loop_test` scripts a stand-in LLM so the parts we own are asserted
deterministically: that tool calls are dispatched through MCP, that results are
fed back with their call ids, that a hallucinated tool name is reported to the
model instead of raising, and that the hop cap ends a runaway with a real
answer. `smoke_test` is the one that proves the security claim — a second user
can neither list nor delete the first user's bookmarks, even when handed the id.

## Environment variables

| Variable | Required | Example | Notes |
|---|---|---|---|
| `DATABASE_URL` | yes | `postgresql://stash:stash@localhost:55432/stash` | Postgres connection string. Render injects this from the linked database. |
| `OPENAI_API_KEY` | for chat | `sk-proj-…` | Without it, chat returns a 503 with a clear message. |
| `OPENAI_MODEL` | no | `gpt-4o-mini` | Any tool-calling chat model. |
| `RESEND_API_KEY` | for email | `re_…` | Without it, emails are logged, not sent. |
| `EMAIL_FROM` | no | `Stash <onboarding@resend.dev>` | Resend's shared sandbox sender needs no verified domain. |
| `BASE_URL` | yes in prod | `https://stash.onrender.com` | Used to build reset links. No trailing slash. |
| `APP_ENV` | no | `production` | Setting `production` marks session cookies `Secure`. |

Copy `.env.example` to `.env` to get the full list. `.env` is gitignored and
must never be committed.

## Deploying to Render

The repo includes `render.yaml`, so **New → Blueprint** provisions the web
service and its Postgres together and wires `DATABASE_URL` for you. To set it up
by hand instead:

- **Build command:** `pip install -r requirements.txt && python -m scripts.init_db`
- **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Health check path:** `/healthz`

Set `OPENAI_API_KEY`, `RESEND_API_KEY` and `BASE_URL` in the dashboard (they are
marked `sync: false` in the blueprint precisely so secrets stay out of Git).
`BASE_URL` must match the service's public URL or reset links will point at the
wrong host. Auto-deploy on push to `main` is enabled in the blueprint.

The schema is created by the build command. `scripts/schema.sql` is entirely
`CREATE ... IF NOT EXISTS`, so re-running it on every deploy is safe.

## Data model

| Table | Purpose |
|---|---|
| `users` | Email (unique, lowercased) and bcrypt hash. |
| `bookmarks` | `url`, `title`, `tags text[]`, `notes`, owned via `user_id` with `ON DELETE CASCADE`. |
| `password_reset_tokens` | SHA-256 digest, `expires_at`, `used_at` for single use. |
| `sessions` | SHA-256 digest of the cookie value plus `expires_at`. |

## Security notes

- Passwords are bcrypt-hashed. bcrypt rejects inputs over 72 bytes rather than
  truncating them silently, so the app validates length up front.
- Session and reset tokens are random 32-byte URL-safe strings, stored only as
  SHA-256 digests. A database leak yields no usable cookie or reset link.
- Reset tokens are spent with a single atomic
  `UPDATE … WHERE used_at IS NULL AND expires_at > now() RETURNING user_id`, so
  a link cannot be replayed even under concurrent submission. A successful reset
  also invalidates that user's other outstanding tokens and all their sessions.
- Login runs a bcrypt comparison against a dummy hash when the email is unknown,
  and the forgot-password endpoint responds identically for known and unknown
  addresses, so neither confirms whether an account exists.
- The MCP subprocess receives only `DATABASE_URL` and `STASH_USER_ID` — not the
  app's OpenAI or Resend credentials.
- `add_bookmark` fetches the page to fill in a missing title, which means the
  server makes requests to user-supplied URLs. Each hop is restricted to
  http/https and to hostnames that resolve exclusively to public addresses, and
  redirects are followed manually so a redirect to `169.254.169.254` is
  re-checked rather than trusted. The body is streamed and capped at 64 KB.

## Known limitations / next steps

Things I would fix first, in order of how much they matter:

1. **A subprocess per chat turn.** `toolbox_for_user` spawns a fresh MCP server
   for every message, which costs roughly a second of startup and a database
   connection. I chose it because it makes the scoping guarantee trivial to
   audit — a process only ever knows one user. The fix is a pooled, keyed
   session cache with idle eviction, which is genuinely fiddly to get right
   around async context-manager lifetimes, so it was the wrong thing to attempt
   against the clock.
2. **Chat history is in-process memory.** `app/conversations.py` keeps the last
   20 turns per user in a dict. It resets on restart and would not be shared
   across multiple Render instances. A `messages` table would fix both; I left
   it out because transcript persistence was not a requirement and it would have
   cost a table and a migration.
3. **No rate limiting.** Login, signup and password-reset endpoints are
   unthrottled. A fixed-window counter in Postgres keyed by IP and email would
   be enough to blunt credential stuffing and reset-link spam.
4. **No email verification on signup.** Accounts are usable immediately, so
   somebody can sign up with an address they do not own.
5. **Title fetching is synchronous inside the tool call.** `add_bookmark` blocks
   for up to five seconds on a slow page before saving. It should move to a
   background job that backfills the title after the bookmark is stored.
6. **The checks are scripts, not a test framework.** They assert real
   behaviour and clean up after themselves, but they are not wired into CI and
   there is no coverage measurement. Porting them to pytest and running them on
   push would be the obvious next step.
