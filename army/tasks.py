"""Background tasks: register people, draft them in batches, download fights, replay fights.

Every network task retries automatically (with exponential backoff) when the war API
times out or answers with a 5xx, because the server is slow on purpose.
"""
import math
import time

import requests
from celery import Task, group, shared_task
from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import F, Sum, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from .models import DownloadBatch, Fight, FightEvent, Person


class TransientAPIError(Exception):
    """The API failed in a way that is worth retrying (5xx, 429)."""


class BatchNotFound(Exception):
    """The API has no such batch (4xx): we are past the last batch."""


def fetch(path):
    url = f'{settings.WAR_API_BASE}{path}'
    response = requests.get(url, timeout=(10, settings.WAR_API_TIMEOUT))
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientAPIError(f'{url} -> HTTP {response.status_code}')
    if 400 <= response.status_code < 500:
        raise BatchNotFound(f'{url} -> HTTP {response.status_code}')
    body = response.json()
    # The API also reports errors inside the body with HTTP 200,
    # e.g. {"data": {"message": "Invalid batch id"}, "status": 400}
    status = body.get('status') if isinstance(body, dict) else None
    if isinstance(status, int) and status >= 500:
        raise TransientAPIError(f'{url} -> {body}')
    if isinstance(status, int) and status >= 400:
        raise BatchNotFound(f'{url} -> {body.get("data")}')
    return body


NETWORK_RETRY = dict(
    autoretry_for=(requests.Timeout, requests.ConnectionError, TransientAPIError, ValueError),
    retry_backoff=2,          # 2s, 4s, 8s, ...
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=10,
)
DB_RETRY = dict(autoretry_for=(OperationalError,), retry_backoff=1, retry_backoff_max=10, max_retries=10)


# --------------------------------------------------------------------------- batch bookkeeping

def mark_batch(kind, number, **fields):
    DownloadBatch.objects.update_or_create(kind=kind, number=number, defaults=fields)


def dispatch_batch(kind, number):
    """Queue the download of a batch once, even if several tasks ask for it at the same time."""
    _, created = DownloadBatch.objects.get_or_create(kind=kind, number=number)
    if created:
        task = download_draft_batch if kind == DownloadBatch.DRAFT else download_fight_batch
        task.delay(number)


def past_known_end(kind, number):
    return DownloadBatch.objects.filter(kind=kind, is_last=True, number__lt=number).exists()


class BatchDownloadTask(Task):
    """Keeps the DownloadBatch row in sync with retries and final failures."""

    kind = None

    def on_retry(self, exc, task_id, args, kwargs, einfo):
        DownloadBatch.objects.filter(kind=self.kind, number=args[0]).update(
            status=DownloadBatch.RETRYING, attempts=F('attempts') + 1, error=str(exc)[:500])

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        DownloadBatch.objects.filter(kind=self.kind, number=args[0]).update(
            status=DownloadBatch.FAILED, error=str(exc)[:500])


class DraftBatchTask(BatchDownloadTask):
    kind = DownloadBatch.DRAFT


class FightBatchTask(BatchDownloadTask):
    kind = DownloadBatch.FIGHT


# --------------------------------------------------------------------------- 1. register people

@shared_task(bind=True, **NETWORK_RETRY)
def register_people(self):
    people = fetch('/people')['data']
    with transaction.atomic():
        Person.objects.bulk_create(
            [Person(id=p['id'], name=p['name'], age=p['age'], key=p.get('key')) for p in people],
            update_conflicts=True, unique_fields=['id'], update_fields=['name', 'age', 'key'],
        )
    return len(people)


# --------------------------------------------------------------------------- 2. draft (batches)

def batches_in_progress(kind):
    return DownloadBatch.objects.filter(kind=kind).exclude(status__in=DownloadBatch.FINISHED).exists()


@shared_task
def start_draft():
    DownloadBatch.objects.filter(kind=DownloadBatch.DRAFT).delete()
    dispatch_batch(DownloadBatch.DRAFT, 0)


@shared_task(bind=True, base=DraftBatchTask, **NETWORK_RETRY)
def download_draft_batch(self, number):
    mark_batch(DownloadBatch.DRAFT, number, status=DownloadBatch.DOWNLOADING)
    try:
        payload = fetch(f'/draft/{number}')
    except BatchNotFound as exc:
        mark_batch(DownloadBatch.DRAFT, number, status=DownloadBatch.MISSING, error=str(exc))
        return 0

    soldiers, next_batch = payload.get('data') or [], payload.get('next_batch')
    if not isinstance(soldiers, list):
        mark_batch(DownloadBatch.DRAFT, number, status=DownloadBatch.MISSING, error=str(soldiers)[:500])
        return 0
    mark_batch(DownloadBatch.DRAFT, number, status=DownloadBatch.DOWNLOADED, item_count=len(soldiers),
               next_batch=next_batch, is_last=next_batch is None, error='')

    # A separate background task drafts this batch right away, while other batches still download.
    draft_batch.delay(number, soldiers)

    if next_batch is not None:
        # We know how many people exist and how big a batch is, so every remaining batch
        # can be downloaded in parallel instead of one after the other.
        last = next_batch
        if soldiers and Person.objects.exists():
            last = max(last, math.ceil(Person.objects.count() / len(soldiers)) - 1)
        for n in range(next_batch, last + 1):
            dispatch_batch(DownloadBatch.DRAFT, n)
    return len(soldiers)


@shared_task(bind=True, **DB_RETRY)
def draft_batch(self, number, soldiers):
    now = timezone.now()
    with transaction.atomic():
        for s in soldiers:
            health = s.get('health', 100)
            Person.objects.update_or_create(
                id=s['id'],
                defaults=dict(name=s['name'], age=s['age'], rank=s.get('rank'), health=health,
                              initial_health=health, key=s.get('key'), drafted_at=now),
            )
        mark_batch(DownloadBatch.DRAFT, number, status=DownloadBatch.PROCESSED)
    return len(soldiers)


# --------------------------------------------------------------------------- 3. fights (batches)

@shared_task
def start_fight_download():
    DownloadBatch.objects.filter(kind=DownloadBatch.FIGHT).delete()
    dispatch_batch(DownloadBatch.FIGHT, 0)


@shared_task(bind=True, base=FightBatchTask, **NETWORK_RETRY)
def download_fight_batch(self, number):
    if past_known_end(DownloadBatch.FIGHT, number):
        mark_batch(DownloadBatch.FIGHT, number, status=DownloadBatch.SKIPPED, error='')
        return 0

    mark_batch(DownloadBatch.FIGHT, number, status=DownloadBatch.DOWNLOADING)
    try:
        payload = fetch(f'/fight/{number}')
    except BatchNotFound as exc:
        mark_batch(DownloadBatch.FIGHT, number, status=DownloadBatch.MISSING, error=str(exc))
        return 0

    fights, next_batch = payload.get('data') or [], payload.get('next_batch')
    if not isinstance(fights, list):
        mark_batch(DownloadBatch.FIGHT, number, status=DownloadBatch.MISSING, error=str(fights)[:500])
        return 0
    mark_batch(DownloadBatch.FIGHT, number, status=DownloadBatch.DOWNLOADED, item_count=len(fights),
               next_batch=next_batch, is_last=next_batch is None or not fights, error='')
    save_fight_batch.delay(number, fights)

    if next_batch is not None and fights:
        # The total number of fight batches is unknown, so request a few batches ahead in parallel.
        for n in range(next_batch, next_batch + settings.FIGHT_BATCH_LOOKAHEAD):
            dispatch_batch(DownloadBatch.FIGHT, n)
    return len(fights)


@shared_task(bind=True, **DB_RETRY)
def save_fight_batch(self, number, fights):
    soldier_ids = {f['blue_soldier_id'] for f in fights} | {f['red_soldier_id'] for f in fights}
    soldier_ids |= {s[k] for f in fights for s in f['steps'] for k in ('attacker', 'defender')}
    known = set(Person.objects.filter(id__in=soldier_ids).values_list('id', flat=True))
    if soldier_ids - known:
        # People are still being registered: try again in a few seconds.
        raise self.retry(countdown=5, max_retries=60)

    with transaction.atomic():
        for f in fights:
            fight, _ = Fight.objects.update_or_create(
                id=f['fight_id'],
                defaults=dict(batch=number, blue_soldier_id=f['blue_soldier_id'], red_soldier_id=f['red_soldier_id']),
            )
            FightEvent.objects.bulk_create(
                [FightEvent(fight=fight, step=i, attacker_id=s['attacker'], defender_id=s['defender'],
                            damage=s['damage']) for i, s in enumerate(f['steps'])],
                ignore_conflicts=True,  # unique (fight, step): saving the same batch twice is harmless
            )
        mark_batch(DownloadBatch.FIGHT, number, status=DownloadBatch.PROCESSED)
    return len(fights)


# --------------------------------------------------------------------------- 4. simulation

@shared_task
def start_simulation():
    """Launch one background task per fight; the worker pool runs them in parallel.

    Each fight is claimed (pending -> running) with a conditional UPDATE before its task is queued,
    so pressing the button twice never queues the same fight twice.
    """
    queued = []
    for fight_id in Fight.objects.filter(status=Fight.PENDING).values_list('id', flat=True):
        if Fight.objects.filter(pk=fight_id, status=Fight.PENDING).update(status=Fight.RUNNING):
            queued.append(fight_id)
    if queued:
        group(simulate_fight.s(fid) for fid in queued).apply_async()
    return len(queued)


@shared_task(bind=True, **DB_RETRY)
def simulate_fight(self, fight_id):
    if Fight.objects.filter(pk=fight_id, status=Fight.DONE).exists():
        return fight_id
    Fight.objects.filter(pk=fight_id, started_at__isnull=True).update(started_at=timezone.now())

    events = list(FightEvent.objects.filter(fight_id=fight_id, applied=False).order_by('step'))
    for event in events:
        apply_event(event)
        if settings.SIMULATION_STEP_DELAY:
            time.sleep(settings.SIMULATION_STEP_DELAY)

    finish_fight(fight_id)
    return fight_id


def apply_event(event):
    """Apply one hit atomically.

    The same soldier can fight in several fights at once, so health is decreased with an
    UPDATE ... SET health = health - damage executed by the database (never read-modify-write
    in Python), and the event is marked as applied in the same transaction.
    """
    with transaction.atomic():
        claimed = FightEvent.objects.filter(pk=event.pk, applied=False).update(
            applied=True, applied_at=timezone.now())
        if not claimed:
            return  # already applied by a previous attempt
        Person.objects.filter(pk=event.defender_id).update(
            health=Greatest(F('health') - event.damage, Value(0)))
        health_after = Person.objects.values_list('health', flat=True).get(pk=event.defender_id)
        FightEvent.objects.filter(pk=event.pk).update(defender_health_after=health_after)


def finish_fight(fight_id):
    fight = Fight.objects.get(pk=fight_id)
    dealt = dict(FightEvent.objects.filter(fight_id=fight_id)
                 .values_list('attacker_id').annotate(total=Sum('damage')))
    blue, red = dealt.get(fight.blue_soldier_id, 0), dealt.get(fight.red_soldier_id, 0)
    winner = fight.blue_soldier_id if blue > red else fight.red_soldier_id if red > blue else None
    Fight.objects.filter(pk=fight_id).update(
        status=Fight.DONE, blue_damage_dealt=blue, red_damage_dealt=red,
        winner_id=winner, finished_at=timezone.now())


def simulation_in_progress():
    return Fight.objects.filter(status=Fight.RUNNING).exists()


def reset_simulation():
    with transaction.atomic():
        Person.objects.update(health=F('initial_health'))
        FightEvent.objects.update(applied=False, defender_health_after=None, applied_at=None)
        Fight.objects.update(status=Fight.PENDING, blue_damage_dealt=0, red_damage_dealt=0,
                             winner=None, started_at=None, finished_at=None)