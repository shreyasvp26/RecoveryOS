# RecoveryOS Deployment & Readiness

## Architecture

RecoveryOS is a deliberately small, single-machine system:

```
ONE backend (FastAPI)
+ ONE frontend (React + Vite)
+ ONE SQLite database
```

There is **no** distributed infrastructure: no message broker, no cache, no
cluster, no container orchestration requirement. This is a design constraint,
not an accident — it keeps the system auditable and reproducible.

## Local development (recommended)

See the README "Local Setup" for exact commands. In short, from a clean clone:

```bash
# 1. Backend
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # edit as needed (never commit .env)

# 2. Initialize + seed + benchmark (deterministic)
python -m app.populate --seed 42 --count 500         # initialize + seed synthetic data
python -m app.benchmark_store --seed 42 --count 500   # persist the Phase 17 benchmark summary

# 3. Run the API
uvicorn app.main:app                                 # http://127.0.0.1:8000

# 4. Frontend in another terminal
cd ../frontend
npm install
npm run dev                                          # http://localhost:5173 (proxies /api -> :8000)
```

Readiness: `GET /health/ready` reports database usability and whether
Razorpay Test Mode / webhook / OmniRoute are configured, as booleans only
(never credential values).

## Demo / public deployment

The simplest realistic approach is to run the same single-machine layout on a
small always-on host (a VM, a Render/Railway/Heroku-style app, or your own
server) with both processes running:

- Backend: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
- Frontend: `npm run build` produces `frontend/dist`, which can be served by
  any static file server (e.g. `vite preview` or a CDN). In production the
  frontend must reach the backend: either serve both behind one origin with a
  reverse proxy, or set `VITE_API_BASE` to the backend's public base URL
  (with CORS enabled on the backend for that origin).

### Environment variables

All configuration is read from the environment (the `DATABASE_URL`, the
Razorpay Test Mode key id/secret, the webhook secret, the OmniRoute key, and
the policy overrides). Document them per the README's table. Secrets are never
hardcoded, never committed, and never stored in SQLite.

### SQLite persistence assumptions

- The SQLite database is a **single file**. It lives wherever `DATABASE_URL`
  points (default `sqlite:///./recoveryos.db`).
- For any durable deployment the file must be on **persistent storage** (not
  an ephemeral filesystem that resets on restart).
- SQLite is a file-based, single-writer database. It is well suited to this
  build/demo/control-plane workload, but it is **not** high-scale production
  payment infrastructure. If that scale is ever required, the persistence
  boundary (`app/db.py`) is the single place to change — but the frozen
  architecture does not include distributed infrastructure, and this
  repository does not claim it.

### Webhook URL requirements

For the **live** Razorpay Test Mode loop, the backend must be reachable from
Razorpay's servers at a public HTTPS URL:

- Register `https://<your-public-host>/webhook/razorpay` in the Razorpay
  Dashboard webhook settings.
- Set `RAZORPAY_WEBHOOK_SECRET` to the shared secret chosen there.
- A public tunnel (e.g. Cloudflare quick tunnel) suffices for a demo but its
  hostname is ephemeral: point the webhook URL at the currently active tunnel.

### Important honesty note

RecoveryOS uses **Test Mode credentials only**. No live-mode key is ever
accepted (`rzp_live_` is rejected structurally at the client boundary), and no
production payment processing is performed or implied. Benchmark revenue is
simulated. If SQLite persistence or Test Mode capacity makes a given platform
unsuitable for a durable production deployment, the honest answer is to state
that and run a reproducible local/demo deployment rather than pretend it is
production-scale.

## Verifying a deployment

1. `GET /health` returns `{"status": "ok"}` (backend alive).
2. `GET /health/ready` reports `status: "ready"` and `database_usable: true`,
   plus the configured/not-configured state of each external integration.
3. Load the frontend and confirm Revenue Health, Recovery Operations, and the
   benchmark panel render real persisted backend data (not fabricated numbers).
