# Community Chat

A small, self-hosted, **invite-only** chat app for a family or friend group.
Single Docker container (monolith) — no CORS, no microservices, no fuss.

- **Frontend:** Vue 3 + Vite + Pinia, installable as a PWA (offline shell)
- **Backend:** Python (FastAPI) + SQLite + WebSockets for real-time
- **Joining:** The first user is seeded as an admin on first boot. Everyone else
  joins with an **invite code** created by an existing member.

## Features

- Group chat + direct messages (real-time via WebSockets)
- Reactions, replies, edit & delete (own messages)
- Image / file attachments (≤10 MB) with a personal gallery + lightbox
- Typing indicators, read receipts (DMs), online status
- Profiles (avatar, name, bio, email, phone)
- Per-user notification settings (DND, mentions, replies)
- Invite management (create / copy / revoke, multi-use)
- PWA: install prompt, service worker, push-style notifications
- Dark theme, mobile responsive

## Quick start (Docker)

```bash
# 1. Configure (optional) — copy the example and tweak
cp .env.example .env    # set PORT, JWT_SECRET, ADMIN_PASSWORD

# 2. Build & run
docker compose up -d --build

# 3. Open the app (default port 8983)
open http://localhost:8983
```

Log in with the seeded admin (`admin` / `$ADMIN_PASSWORD`). Create an invite in
**Settings → Invite friends**, share the code (or the `/login?code=...` link)
with family, and they join from the "Join with invite" tab.

> Data (SQLite DB + uploaded files) persists in the `chat-data` and
> `chat-uploads` Docker volumes. Stop/start the container freely — nothing is lost.

> **Local email (dev):** the dev compose also runs a **Mailpit** email sink, so
> transactional email (invites, welcome, message notifications) is captured and
> viewable at **http://localhost:3025** instead of leaving the machine. It is not
> part of the production stack.

## Production deploy

The dev file (`docker-compose.yml`) is opinionated for local use (Mailpit sink,
localhost). **Production uses `docker-compose.prod.yml`**, which runs only the
`app` service against a real SMTP provider.

1. Put the required values in `/chat/.env` on the host (never commit them):
   ```
   JWT_SECRET=<openssl rand -hex 32>
   VAPID_PRIVATE_KEY=...      # raw 32-byte base64url
   VAPID_PUBLIC_KEY=...       # raw 65-byte uncompressed point, base64url
   VAPID_EMAIL=mailto:you@example.com
   APP_ORIGIN=https://your-domain

   # Real transactional SMTP provider (example: Resend)
   SMTP_HOST=smtp.resend.com
   SMTP_PORT=587
   SMTP_USERNAME=resend
   SMTP_PASSWORD=<your provider SMTP API key>
   SMTP_FROM_ADDRESS=noreply@your-domain
   SMTP_FROM_NAME=Community Chat
   ```
2. Start it:
   ```bash
   APP_GIT_HASH=$(git rev-parse --short HEAD) docker compose -f docker-compose.prod.yml up -d --build
   ```

The prod file uses `:?` defaults so it **refuses to start** if a core value is
missing (JWT secret, VAPID keys, `APP_ORIGIN`). The `SMTP_*` values are
**optional** — leave them empty to boot with email disabled, then add them and
recreate the container to turn it on.

## Local development (no Docker)

```bash
# Backend (listens on PORT, default 8983)
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ADMIN_PASSWORD=devpass123 .venv/bin/python run.py        # http://localhost:8983

# Frontend (separate terminal) — proxies /api and /ws to :8983
cd frontend
npm install
npm run dev                                             # http://localhost:5173
```

### Run the tests

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

Tests run against a throwaway **in-memory** SQLite database (`:memory:`), so no
real data file is ever touched.

## Configuration (env vars)

| Var              | Default           | Purpose                              |
| ---------------- | ----------------- | ------------------------------------ |
| `PORT`           | `8983`            | Port the server listens on           |
| `HOST`           | `0.0.0.0`         | Bind address                         |
| `ADMIN_HANDLE`   | `admin`           | Seeded admin username                |
| `ADMIN_PASSWORD` | *(generated)*     | Seeded admin password (printed once) |
| `ADMIN_NAME`     | `Admin`           | Seeded admin display name            |
| `JWT_SECRET`     | `dev-…-me`        | Token signing key — **set in prod**  |
| `DATABASE_URL`   | `sqlite:///…/data/chat.db` | DB location           |
| `DATA_DIR`       | `backend/data`    | Where the SQLite file lives          |
| `UPLOAD_DIR`     | `backend/uploads` | Where attachments are stored         |
| `FRONTEND_DIR`   | auto              | Where the built frontend lives       |

The seeder runs on startup and is **idempotent** — it only creates the admin if
no user with that handle exists, so it never overwrites an existing account.

## Architecture notes

- **Monolith:** FastAPI serves the built Vue app (`index.html` + assets) and the
  `/uploads` files, so the browser only ever talks to one origin — no CORS.
- **Real-time:** a WebSocket hub fans out `message.new`, `dm.new`,
  `reaction.changed`, `typing`, and `dm.read` events to connected clients.
- **Single instance by design:** there's intentionally no multi-tenancy. If you
  want a second community, run a second container with its own volumes.
