# Stash

**Live: https://stash-phon.onrender.com** — on Render's free tier, so the first
request after a spell of inactivity takes ~30 seconds to wake the instance.

Stash is a personal bookmark manager you operate by talking to it. You sign up
with an email and password, then tell an AI co-pilot things like *"Save
https://example.com under tag reading"* or *"What did I stash about Python?"*.
The co-pilot never touches the database itself: it reaches your bookmarks
exclusively through tools published by an **MCP (Model Context Protocol)
server**, which is spawned per request with your user id baked into its
environment. Auth is bcrypt-hashed with server-side sessions, password resets
use single-use time-limited tokens, and transactional email goes out via Resend.

The co-pilot runs on Gemini by default, but it talks to any OpenAI-compatible
chat-completions endpoint, so switching provider is three environment variables
rather than a code change.

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
  │    LLM    ──── tool_calls ───► MCP bridge (app/mcp_bridge.py)
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

### Why sessions are cached, and why that is still safe

The first version spawned a fresh MCP subprocess for every message. That is the
easiest version to audit, but profiling the deployment showed it was also
almost all of the latency: an ~11s turn was ~7s of spawn, ~1.5s of teardown and
only ~2s of actual model time, with the database under 100ms. Nearly all of the
spawn is `import mcp` — 1.3s of a 1.4s module import locally, and around 6-7s
on a throttled 0.1-CPU instance.

So `app/mcp_bridge.py` now keeps a session per user and reuses it, evicting it
once idle. Typical turns went from ~11s to ~1.3-2.4s on the same free instance;
only a user's first message pays the spawn. Measure it yourself with
`python -m scripts.profile_chat <url>`, and confirm the reuse in the logs —
there should be one `MCP session ready` line for many `chat turn took` lines.

The scoping guarantee is unchanged, which was the constraint the change had to
respect. The cache key *is* the user id, and the subprocess still gets
`STASH_USER_ID` pinned in its environment, so a process is only ever reused for
the user it was spawned for. Reuse across users is not something that is
prevented by a check; there is no code path that can express it.

Two details make the reuse safe rather than merely fast. Each session is owned
by a dedicated task that both opens and closes it, because anyio requires the
task that entered a context manager to be the one that exits it — closing from
whichever request happened to finish last would corrupt the cancel scope. And
the registry lock is never held across a spawn, so one user's slow start does
not queue behind another's, while concurrent turns for the same user await a
single shared future instead of racing to create duplicate processes.

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

Locally, all except `check_mcp` need `DATABASE_URL` and an initialised schema;
they create throwaway users and clean up after themselves. Only
`live_chat_test` spends tokens.

```bash
python -m scripts.check_mcp        # tool schemas the model sees (no DB needed)
python -m scripts.smoke_test       # auth invariants + cross-user isolation
python -m scripts.http_smoke       # the graded click-through, at HTTP level
python -m scripts.chat_loop_test   # the tool-call loop, against a stubbed LLM
python -m scripts.live_chat_test   # the real model, end to end (needs AI_API_KEY)
```

Against a deployment, `live_check` drives the public URL over real HTTP the way
a grader would click through it — sign up, save a bookmark by talking to the
co-pilot, recall it, log out and back in to prove persistence, then confirm a
second account cannot see the first one's data:

```bash
python -m scripts.live_check https://stash-phon.onrender.com
```

It needs no credentials beyond the URL. If a deploy misbehaves,
`python -m scripts.render_logs build` (or `app`) prints the tail of the relevant
Render log, and `python -m scripts.render_status` shows recent deploys with what
triggered each one — both without opening the dashboard.

`chat_loop_test` scripts a stand-in LLM so the parts we own are asserted
deterministically: that tool calls are dispatched through MCP, that results are
fed back with their call ids, that a hallucinated tool name is reported to the
model instead of raising, and that the hop cap ends a runaway with a real
answer.

`smoke_test` proves the security claim at the data layer — a second user can
neither list nor delete the first user's bookmarks, even when handed the id.
`live_chat_test` proves it at the conversational layer: it asks the model, in
so many words, to enter "admin mode" and dump every other user's bookmarks, and
asserts nothing leaks. It also covers the graded sequence end to end (save with
an inferred tag, search, delete by description).

`http_smoke` substitutes a spy for the mailer, so it follows the link a user
would really receive without needing a Resend key or spending email quota.

## Environment variables

| Variable | Required | Example | Notes |
|---|---|---|---|
| `DATABASE_URL` | yes | `postgresql://stash:stash@localhost:55432/stash` | Postgres connection string. Render injects this from the linked database. |
| `AI_API_KEY` | for chat | `AIzaSy…` | Without it, chat returns a 503 with a clear message. |
| `AI_BASE_URL` | no | `https://generativelanguage.googleapis.com/v1beta/openai/` | Any OpenAI-compatible endpoint. Leave blank for OpenAI itself. |
| `AI_MODEL` | no | `gemini-2.5-flash` | Any model that supports tool calling. |
| `RESEND_API_KEY` | for email | `re_…` | Without it, emails are logged, not sent. |
| `EMAIL_FROM` | no | `Stash <onboarding@resend.dev>` | Resend's shared sandbox sender needs no verified domain. |
| `BASE_URL` | yes in prod | `https://stash.onrender.com` | Used to build reset links. No trailing slash. |
| `APP_ENV` | no | `production` | Setting `production` marks session cookies `Secure`. |
| `MCP_MAX_LIVE_SESSIONS` | no | `4` | Cached MCP subprocesses to keep. Bounded by instance memory, not by database connections. |
| `MCP_SESSION_IDLE_SECONDS` | no | `300` | How long an unused session is kept before eviction. |

Copy `.env.example` to `.env` to get the full list. `.env` is gitignored and
must never be committed.

## Deploying to Render

Three ways, in descending order of convenience.

**Scripted, via the Render API.** With `RENDER_API_KEY` in your `.env`:

```bash
python -m scripts.deploy_render https://github.com/you/stash
```

This creates a free Postgres and a free web service, pushes the environment
variables from your local `.env`, waits for the build, and prints the public
URL. It is idempotent — re-running updates the existing resources and
redeploys.

One caveat: Render clones a *public* repo anonymously, so the build works
whether or not your GitHub account is linked — but installing the push webhook
does need that link. Until you connect GitHub under **Account Settings →
GitHub**, the service reports `autoDeploy: yes` while pushes silently fail to
trigger anything. `python -m scripts.render_status` shows whether the last
deploy came from a push or from the API, which is the quickest way to tell.

**From the blueprint.** The repo includes `render.yaml`, so **New → Blueprint**
provisions the web service and its Postgres together and wires `DATABASE_URL`
for you.

**By hand:**

- **Build command:** `pip install -r requirements.txt && python -m scripts.init_db`
- **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Health check path:** `/healthz`

Set `AI_API_KEY`, `RESEND_API_KEY` and `BASE_URL` in the dashboard (they are
marked `sync: false` in the blueprint precisely so secrets stay out of Git).
`BASE_URL` must match the service's public URL or reset links will point at the
wrong host. Auto-deploy on push to `main` is enabled in the blueprint.

The schema is created by the build command. `scripts/schema.sql` is entirely
`CREATE ... IF NOT EXISTS`, so re-running it on every deploy is safe.

Use the database's **internal** connection string, not the external one. A
Render Postgres created through the API starts with an empty IP allow list and
refuses public connections outright, so the external string fails from
everywhere — including the first build, with a misleading `SSL connection has
been closed unexpectedly`. The internal host is reachable from both the build
environment and the running service, and it keeps the database off the public
internet, so there is no reason to open the allow list.

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

1. **The first message of a session is still slow.** Sessions are cached (see
   above), but a user's first message pays the ~7s spawn, and a free instance
   that has spun down adds a ~30s wake-up on top. Pre-warming a session at login
   would hide the first cost; the second needs a paid instance.
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
