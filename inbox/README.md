# WorkPulse Feedback Inbox

The **"mother" receiver**. External WorkPulse instances push feedback (and, later,
usage/error reports) *to* this service. It never connects back to them — the flow
is strictly one-way: `external app → inbox`. You read what lands here on a simple
web page.

- `POST /api/push` — receive one message. Requires header `X-WorkPulse-Key: <write key>`.
- `GET /` — the inbox, newest first. Browser prompts for a password → your **admin key**.
- `GET /api/messages` — same list as JSON (admin key).
- `GET /health` — public liveness check.

Two keys, both supplied as environment variables (nothing secret is in the code):

| Env var | Who holds it | What it grants |
|---|---|---|
| `INBOX_WRITE_KEY` | baked into pilot builds | permission to **push** a message (write-only) |
| `INBOX_ADMIN_KEY` | only you | permission to **read** the inbox |

Generate strong values:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"   # run twice
```

---

## Option B — deploy to Fly.io (no machine to run) — recommended for now

One-time setup (free tier; scales to zero when idle):

```bash
# 1. Install flyctl and sign in
brew install flyctl            # or: curl -L https://fly.io/install.sh | sh
fly auth login

# 2. From this inbox/ directory — create the app WITHOUT deploying yet
fly launch --no-deploy         # accept the name or edit `app` in fly.toml first

# 3. Persistent storage so messages survive restarts/redeploys
fly volumes create inbox_data --size 1     # 1 GB is plenty

# 4. Set your two keys (paste the values you generated above)
fly secrets set INBOX_WRITE_KEY=<write-key> INBOX_ADMIN_KEY=<admin-key>

# 5. Ship it
fly deploy
```

Your inbox is now at `https://<app-name>.fly.dev`. Open it, enter the **admin key**
as the password. That URL + the **write key** are what the WorkPulse pilot builds
get configured with (see the client-side `feedback` settings).

## Option A — run on your own spare machine (later)

The service is featherweight (any old laptop or a Raspberry Pi is overkill). To
make it reachable without touching your router, pair it with a free Cloudflare
Tunnel:

```bash
# On the spare machine, from this directory:
pip install -r requirements.txt
INBOX_WRITE_KEY=<write-key> INBOX_ADMIN_KEY=<admin-key> INBOX_DB=./inbox.db \
  uvicorn app:app --host 127.0.0.1 --port 8080

# In another terminal, expose it with a stable public URL:
cloudflared tunnel --url http://127.0.0.1:8080
```

Same code, same keys — only the hosting differs. Moving from B to A later is just
a redeploy; no rewrite.

## Run locally (to try it)

```bash
INBOX_WRITE_KEY=devwrite INBOX_ADMIN_KEY=devadmin INBOX_DB=./inbox.db \
  uvicorn app:app --host 127.0.0.1 --port 8080
# push a test message:
curl -X POST http://127.0.0.1:8080/api/push \
  -H 'X-WorkPulse-Key: devwrite' -H 'Content-Type: application/json' \
  -d '{"kind":"feedback","message":"hello from a pilot","user_label":"me"}'
# read it: open http://127.0.0.1:8080 → password devadmin
```

## Notes

- Fails closed: if the keys aren't set, pushes are rejected and the inbox shows a
  setup page instead of data.
- Messages are capped at 64 KB; oversized pushes get `413`.
- This directory is independently deployable — it does not import WorkPulse.
