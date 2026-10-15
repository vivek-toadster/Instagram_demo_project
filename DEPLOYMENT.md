# Deployment and operations

PostgreSQL is the source of truth. Neo4j stores a rebuildable graph projection;
the graph outbox worker applies committed PostgreSQL events to Neo4j.

Personalization uses collaborative interest signals and normalized hashtags
extracted from captions. Users who liked the same posts or the same hashtags
can contribute candidates that the viewer has not liked yet. Hashtag extraction
is intentionally bounded and does not require a PostgreSQL schema migration.

## Configuration

Copy `.env.example` to `.env` and provide real values. Keep `.env` outside
version control. Generate a strong session secret with:

```bash
openssl rand -hex 32
```

Set that output as `SECRET_KEY`. Set `COOKIE_SECURE=true` only when the browser
reaches the application through HTTPS. `NEO4J_DATABASE` is the Neo4j database
name, not the Neo4j Desktop instance name; omit it only when the server default
database is intended. Leave `SQLALCHEMY_ECHO=false` in deployed environments.
Set `APP_ENV=production`; the application refuses to start if the production
session secret is missing, is the development placeholder, or is shorter than
32 characters.

Required services are PostgreSQL and the configured Neo4j database. Verify both
are running before starting the application. Do not put passwords, session
tokens, or private event payloads in logs.

## Install and migrate

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
```

Run migrations once per deployment against the intended PostgreSQL database.
The graph schema is initialized idempotently by the application when Neo4j is
available. A Neo4j outage does not prevent the web process from starting.

## Start the processes

For a local run:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
python -m app.graph_outbox_worker --interval 5
```

In production, supervise the web process with the selected ASGI process
manager (for example, Gunicorn with Uvicorn workers) and supervise exactly one
or more independent instances of `app.graph_outbox_worker` with systemd,
Supervisor, or the platform process manager. The worker claims PostgreSQL rows
with bounded batches and `SKIP LOCKED` on PostgreSQL, so multiple worker
instances can safely share the queue. Use `--once` for a scheduler-triggered
bounded run. Use `--replay-dead-letter` only after reviewing the underlying
failure.

Terminate workers normally so their `finally` block closes the Neo4j driver.
Events claimed by a process that stops are made pending again after the stale
processing timeout (currently five minutes).

## Probes and metrics

- `GET /health/live` checks only that the web process responds.
- `GET /health/ready` checks PostgreSQL and Neo4j separately and returns `503`
  until both are available. Dependency results are cached briefly to avoid
  doing a database round trip on every application request.
- `GET /health/neo4j` remains available for compatibility.
- `GET /metrics` exposes process-local Prometheus-style counters. They reset on
  process restart and are not a replacement for durable monitoring.

The application emits JSON event lines for HTTP requests and outbox batches.
Configure log collection at the process manager or platform layer. Counters
include request failures/volume, feed fallbacks, dependency failures, and
outbox claimed/processed/retried/dead-letter events.

## HTTPS, backups, and recovery

Terminate HTTPS at a trusted reverse proxy, forward only the required headers,
and set `COOKIE_SECURE=true`. Keep `SECRET_KEY` stable across restarts; rotating
it invalidates existing sessions. Restrict PostgreSQL and Neo4j network access
and credentials to the application and worker.

Back up PostgreSQL regularly and test restoring it. PostgreSQL backups contain
the authoritative users, posts, relationships, and outbox events. Neo4j can be
backed up separately, but its graph projection may be rebuilt by replaying
outbox events or by a dedicated projection rebuild procedure when one is added.
Do not test restore or replay commands against production without an isolated
copy and an explicit change window.

## Bounded load testing

The standard-library/`httpx` harness in `loadtest/feed_load.py` does not create
data. Start an isolated local stack, authenticate separately if testing
`/feed`, and pass a session cookie without printing it:

```bash
python loadtest/feed_load.py --url http://127.0.0.1:8000/health/live \
  --requests 100 --concurrency 10
python loadtest/feed_load.py --url http://127.0.0.1:8000/feed \
  --cookie 'session_token=REDACTED_LOCAL_TOKEN' \
  --requests 100 --concurrency 10
```

The script reports its request count, concurrency, status/error counts, and
measured mean/p50/p95/p99 latency. Begin with small runs such as 100 requests
and concurrency 10; increase only against isolated test services. No load test
was run against the configured databases as part of this change.

## Local personalized-feed demo data

After applying migrations and confirming the local PostgreSQL/Neo4j services
are running, create a safe, non-destructive demo dataset:

```bash
python scripts/seed_demo_data.py --confirm-local --users 20 --posts 350
python -m app.graph_outbox_worker --interval 1
```

The seed creates or reuses `demo_user` plus demo accounts, 350 posts using
the existing images in `uploads/`, follows, likes, and hashtag captions. It
does not delete existing records or call Neo4j directly. Log in with
`demo_user` and `DemoPass123!`; the viewer follows only three demo accounts
and likes several `#dogs` posts, so unliked dog posts should become candidates
after the worker processes the outbox. Re-running the command is idempotent
for the same target range. Use only on a local/test database.

To turn the actual files already present in `uploads/` into posts for the
existing users, use the separate importer:

```bash
python scripts/import_uploads_as_posts.py --confirm-local
python -m app.graph_outbox_worker --interval 1
```

Each supported image becomes one post and is assigned round-robin across the
existing users. The importer is idempotent by image path, does not delete or
copy image files, and supports JPG, JPEG, PNG, GIF, WEBP, AVIF, and JPGE
extensions.
