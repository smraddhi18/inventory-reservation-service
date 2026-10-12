# Inventory Reservation Service

A backend service for reserving scarce inventory during checkout. It demonstrates transactional state machines, PostgreSQL row locking, idempotent writes, expiring holds, an append-only stock ledger, tenant isolation, and reliable event delivery. The service uses Django ORM throughout; it contains no raw SQL.

The central invariant is `0 <= reserved <= on_hand`. Available stock is `on_hand - reserved`. Every reserve, commit, cancel, expiry, and adjustment changes the item and its ledger entry in one database transaction. A reservation can move from `held` to exactly one terminal state: `committed`, `canceled`, or `expired`.

```text
API key -> tenant -> item row lock -> reservation transition
                                  -> stock movement
                                  -> outbox event (same transaction)
                                                        |
                                              leased webhook worker
```

## Why this project

An order workflow can receive duplicate requests, lose a worker between a database commit and a network call, or process two buyers for the last unit at once. This service makes those situations visible and handles them explicitly. It is a separate backend domain from the Subscription Reliability Lab: inventory concurrency and fulfillment state, rather than payment processing.

## Run with PostgreSQL

Requires Docker Compose. Copy `.env.example` to `.env`, replace all three placeholder secrets, then start the stack:

```bash
cp .env.example .env
docker compose up --build -d
docker compose exec api python manage.py provision_tenant demo "Demo Store"
```

The provisioning command displays the API key once. Store it securely. The API is bound to `127.0.0.1:8000`; put a TLS reverse proxy and external authentication controls in front of it for deployment. The Compose stack starts PostgreSQL, migration, API, outbox worker, and reservation sweeper services.

For signed webhook delivery, provision a tenant with `--webhook-url https://your-receiver.example/events`. The URL is set by an operator, not through the public API. The receiver should deduplicate on `X-Event-Id` because webhook delivery is at least once. It can verify `X-Event-Signature` as HMAC-SHA256 of `X-Event-Timestamp + "." + raw_body` using `WEBHOOK_SIGNING_SECRET`. Redirects are disabled, HTTPS is required, and network calls occur outside database transactions.

## Local API development

Requires Python 3.12 or newer. SQLite is supported for single-process development and tests; PostgreSQL is required to demonstrate row-lock concurrency.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
mkdir -p data
export DEBUG=true
python manage.py migrate
python manage.py provision_tenant demo "Demo Store"
python manage.py runserver 127.0.0.1:8000
```

Use the key printed by `provision_tenant` in the examples below:

```bash
export API_KEY='inv_replace_with_provisioned_key'

curl -sS -X POST http://127.0.0.1:8000/v1/items \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"sku":"concert-seat-a1","initial_stock":1}'

curl -sS -X POST http://127.0.0.1:8000/v1/reservations \
  -H "X-API-Key: $API_KEY" -H 'Idempotency-Key: checkout-123' \
  -H 'Content-Type: application/json' \
  -d '{"sku":"concert-seat-a1","quantity":1,"hold_seconds":300}'
```

The reservation response contains its `id`. Send the same request and key again to get the original reservation with `replayed: true`. Change the quantity with the same key to receive `409 idempotency_conflict`. A second checkout receives `409 insufficient_stock` until the first hold is canceled or expires. Confirm an order with `POST /v1/reservations/{id}/commit` and body `{}`; release a hold with `/cancel`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health/live` | Process health |
| `GET` | `/health/ready` | Database readiness |
| `POST` | `/v1/items` | Create a SKU with initial stock |
| `GET` | `/v1/items/{sku}` | Get on-hand, reserved, and available units |
| `GET` | `/v1/items/{sku}/movements?limit=50` | Inspect the stock ledger |
| `POST` | `/v1/items/{sku}/adjustments` | Adjust stock; requires `Idempotency-Key` |
| `POST` | `/v1/reservations` | Hold units; requires `Idempotency-Key` |
| `GET` | `/v1/reservations/{id}` | Read a tenant's reservation |
| `POST` | `/v1/reservations/{id}/commit` | Commit a live hold to an order |
| `POST` | `/v1/reservations/{id}/cancel` | Release a live hold |

All `/v1` calls require `X-API-Key`. Keys are stored as SHA-256 digests, can be revoked in the database, and select one tenant. Every query that accepts a client identifier is scoped to that tenant. Error responses have a stable `error.code` and a request ID in `X-Request-ID`. Malformed input is rejected before entering a transaction.

## Operational behavior

- **Concurrent buyers:** reserve and transition operations lock the item row first, then the reservation row, so stock cannot be oversold on PostgreSQL. Database constraints provide a second boundary. Lock ordering is consistent to reduce deadlocks.
- **Idempotency:** reservation client tokens and adjustment request tokens are unique per tenant. Replays with the same input return the same reservation or the recorded adjustment result; conflicting reuse returns 409.
- **Expiry:** the sweeper releases due holds. A commit request also checks expiry while holding the item lock, so an overdue hold cannot be committed even if the sweeper is late.
- **Outbox:** events are inserted in the same transaction as inventory changes. Workers claim due rows using `FOR UPDATE SKIP LOCKED`, deliver outside the transaction, and finalize only if they still own the lease. Failed deliveries use capped exponential backoff and eventually move to `dead`. An operator can inspect a dead event and requeue it with `python manage.py requeue_dead_event <event-id>`.
- **Reconciliation:** `python manage.py reconcile_inventory` compares current stock with movement totals and held reservations, and exits nonzero when it finds a mismatch.
- **Observability:** logs are JSON; health endpoints separate liveness from database readiness; each API response includes a request ID.

The PostgreSQL locking choice follows the [PostgreSQL row locking documentation](https://www.postgresql.org/docs/current/explicit-locking.html), and the queue claim uses the [documented `SKIP LOCKED` behavior](https://www.postgresql.org/docs/current/sql-select.html). Django's [transaction and `select_for_update` documentation](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update) explains the ORM calls used here.

## Quality checks

```bash
ruff format --check .
ruff check .
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test inventory --verbosity 2
```

The tests cover lifecycle transitions, oversell prevention, idempotency conflicts, expiry, tenant isolation, stock reconciliation, and outbox retry. A two-buyer concurrent hold test runs when the suite is pointed at PostgreSQL. CI runs the suite against a PostgreSQL service and checks formatting, lint, and migration drift.

## Deployment notes

This is a deployable reference project, not a live production system. For an internet-facing rollout, use managed PostgreSQL and backups, TLS termination, a secret manager, network egress rules for operator-configured webhook destinations, metrics/alerts, and a rate limit at the edge. The demo uses one global signing secret; a multi-customer platform should rotate and scope webhook secrets per tenant.
