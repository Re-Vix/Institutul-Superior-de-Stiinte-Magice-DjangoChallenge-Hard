# Django War Simulator — Hard

CodeRun 2026 Django challenge, Hard track. Built on `coderun2026-starter` (Django + Celery).

## Flow

1. **Register people** — a background task downloads `/people` and saves every person.
2. **Draft soldiers** — `/draft-info`: drafting adds a `rank` (and `health`) to every person
   (migration `0002_draft_soldiers`). Every draft batch `/draft/<n>` is downloaded by its own task and,
   as soon as it arrives, drafted by another task. After the first batch the total number of batches is
   known, so the rest are downloaded in parallel. Tasks retry with exponential backoff when the
   server times out or returns a 5xx. Hellstorm Fireburn's `key` points to the next step (`/fight-info`).
3. **Download fights** — every `/fight/<n>` batch is downloaded and saved in its own task
   (`Fight` + one `FightEvent` per step). The total is unknown, so a few batches are requested ahead
   in parallel; the chain stops at the batch whose `next_batch` is `null`.
4. **Simulate** — `/simulate` launches one Celery task per fight; all of them run in parallel.
   Each event decreases the defender's health.

## Concurrency

- Health is decreased with a single `UPDATE ... SET health = MAX(health - damage, 0)` done by the database
  (`F()` expression), never read-modify-write in Python, so a soldier in several parallel fights never loses a hit.
- Every event has an `applied` flag set in the same transaction as the health update: a retried task never
  applies the same hit twice.
- Each batch has a `DownloadBatch` row (unique per kind and number), so a batch is queued exactly once.
- SQLite runs with a 30 s lock timeout and `IMMEDIATE` transactions; DB tasks retry on `OperationalError`.

## Database design

- `Person` — id from the API, name, age; drafting adds rank, health, initial_health, key, drafted_at.
- `DownloadBatch` — status of every downloaded batch (queued / downloading / retrying / processed / ...).
- `Fight` — blue and red soldier, status, damage dealt by each side, winner.
- `FightEvent` — fight, step order, attacker, defender, damage, health of the defender after the hit.

## Run (Windows)

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Second terminal (same venv):

```
celery -A coderun2026_starter worker -l info
```

Open http://127.0.0.1:8000/ and press the buttons in order (1 → 2 → 3), then go to
http://127.0.0.1:8000/simulate and press **Simulate all fights**. Pages refresh by themselves while work runs.
