from django.conf import settings
from django.contrib import messages
from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import tasks
from .models import DownloadBatch, Fight, FightEvent, Person


def _batch_summary(batches):
    finished = sum(b.status in DownloadBatch.FINISHED for b in batches)
    useful = [b for b in batches if b.status not in (DownloadBatch.MISSING, DownloadBatch.SKIPPED)]
    return {
        'batches': batches,
        'total': len(useful),
        'processed': sum(b.status == DownloadBatch.PROCESSED for b in batches),
        'retries': sum(b.attempts for b in batches),
        'failed': sum(b.status == DownloadBatch.FAILED for b in batches),
        'in_progress': finished < len(batches),
        'complete': bool(batches) and finished == len(batches),
    }


def _step_state(in_progress, complete, failed=0):
    if failed:
        return 'error'
    if in_progress:
        return 'active'
    return 'done' if complete else 'idle'


def home(request):
    batches = list(DownloadBatch.objects.all())
    draft = _batch_summary([b for b in batches if b.kind == DownloadBatch.DRAFT])
    fight = _batch_summary([b for b in batches if b.kind == DownloadBatch.FIGHT])

    people = Person.objects.count()
    drafted = Person.objects.filter(drafted_at__isnull=False).count()
    fights = Fight.objects.count()
    fights_done = Fight.objects.filter(status=Fight.DONE).count()

    steps = [
        {
            'number': '01', 'title': 'Register', 'action': 'register',
            'description': 'Download every person from /people.',
            'value': people, 'unit': 'people',
            'state': 'done' if people else 'idle',
        },
        {
            'number': '02', 'title': 'Draft', 'action': 'draft',
            'description': 'Rank every person, one background task per batch.',
            'value': drafted, 'unit': f'of {people} drafted' if people else 'drafted',
            'state': _step_state(draft['in_progress'], draft['complete'] and drafted == people, draft['failed']),
            'busy': draft['in_progress'],
            'disabled': not people,
        },
        {
            'number': '03', 'title': 'Fight intel', 'action': 'fights',
            'description': 'Download every fight and save each event.',
            'value': fights, 'unit': 'fights',
            'state': _step_state(fight['in_progress'], fight['complete'] and fights > 0, fight['failed']),
            'busy': fight['in_progress'],
            'disabled': not people,
        },
        {
            'number': '04', 'title': 'Battle', 'link': reverse('simulate'),
            'description': 'Replay every fight in parallel.',
            'value': fights_done, 'unit': f'of {fights} fought' if fights else 'fought',
            'state': 'done' if fights and fights_done == fights else 'active' if fights_done else 'idle',
            'disabled': not fights,
        },
    ]

    soldiers = Person.objects.all()
    rank_counts = dict(Person.objects.exclude(rank__isnull=True).values_list('rank').annotate(n=Count('id')))
    context = {
        'nav': 'army',
        'steps': steps,
        'draft': draft,
        'fight': fight,
        'event_count': FightEvent.objects.count(),
        'key_holders': Person.objects.exclude(key__isnull=True).exclude(key=''),
        'api_base': settings.WAR_API_BASE,
        'soldiers': soldiers,
        'rank_counts': [(r, rank_counts.get(r, 0)) for r in ('General', 'Lieutenant', 'Sergeant', 'Private')],
        # ?watch=1 is added after pressing a button: keep refreshing until the tasks have started.
        'live': draft['in_progress'] or fight['in_progress'] or 'watch' in request.GET,
    }
    return render(request, 'army/home.html', context)


ACTIONS = {
    'register': (tasks.register_people, None, 'Registering people in the background.'),
    'draft': (tasks.start_draft, DownloadBatch.DRAFT,
              'Draft started: every batch is downloaded and drafted in its own background task.'),
    'fights': (tasks.start_fight_download, DownloadBatch.FIGHT, 'Downloading fight intel in the background.'),
}


@require_POST
def run_action(request, action):
    if action not in ACTIONS:
        return redirect('home')
    task, kind, message = ACTIONS[action]
    if kind and tasks.batches_in_progress(kind) and not request.POST.get('force'):
        messages.warning(request, f'The {kind} download is already running. Use “Restart” only if the worker was stopped.')
        return redirect(reverse('home') + '?watch=1')
    task.delay()
    messages.success(request, message)
    return redirect(reverse('home') + '?watch=1')


FILTERS = {
    'all': ('All', Q()),
    'combat': ('In combat', Q(status=Fight.RUNNING, started_at__isnull=False)),
    'queued': ('Queued', Q(status=Fight.RUNNING, started_at__isnull=True)),
    'pending': ('Pending', Q(status=Fight.PENDING)),
    'done': ('Finished', Q(status=Fight.DONE)),
}


def simulate(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'reset':
            if tasks.simulation_in_progress() and not request.POST.get('force'):
                messages.warning(request, 'Fights are still running. Wait for them to finish before resetting.')
            else:
                tasks.reset_simulation()
                messages.success(request, 'Battlefield reset: every soldier is back to the health from the draft.')
        elif not Fight.objects.exists():
            messages.warning(request, 'No fight intel yet. Download the fights first.')
        elif not Fight.objects.filter(status=Fight.PENDING).exists():
            messages.info(request, 'Every fight has already been launched. Reset the battlefield to replay them.')
        else:
            tasks.start_simulation.delay()
            messages.success(request, 'Simulation launched: one background task per fight, all running in parallel.')
        return redirect(reverse('simulate') + '?watch=1')

    fights_qs = Fight.objects.all()
    counts = fights_qs.aggregate(
        total=Count('id'),
        pending=Count('id', filter=Q(status=Fight.PENDING)),
        queued=Count('id', filter=Q(status=Fight.RUNNING, started_at__isnull=True)),
        combat=Count('id', filter=Q(status=Fight.RUNNING, started_at__isnull=False)),
        done=Count('id', filter=Q(status=Fight.DONE)),
        blue_wins=Count('id', filter=Q(status=Fight.DONE, winner=F('blue_soldier'))),
        red_wins=Count('id', filter=Q(status=Fight.DONE, winner=F('red_soldier'))),
        draws=Count('id', filter=Q(status=Fight.DONE, winner__isnull=True)),
        blue_damage=Sum('blue_damage_dealt'),
        red_damage=Sum('red_damage_dealt'),
    )
    events = FightEvent.objects.aggregate(total=Count('id'), applied=Count('id', filter=Q(applied=True)))
    events['pct'] = round(events['applied'] * 100 / events['total']) if events['total'] else 0
    decided = (counts['blue_wins'] or 0) + (counts['red_wins'] or 0)
    counts['blue_share'] = round(counts['blue_wins'] * 100 / decided) if decided else 50

    current = request.GET.get('status', 'all')
    if current not in FILTERS:
        current = 'all'
    fights = (fights_qs.filter(FILTERS[current][1])
              .select_related('blue_soldier', 'red_soldier', 'winner')
              .annotate(events_total=Count('events'),
                        events_applied=Count('events', filter=Q(events__applied=True))))

    fighters = Person.objects.filter(Q(fights_as_blue__isnull=False) | Q(fights_as_red__isnull=False)).distinct()
    context = {
        'nav': 'simulate',
        'fights': fights,
        'counts': counts,
        'events': events,
        'filters': [(key, label) for key, (label, _) in FILTERS.items()],
        'current': current,
        'running': counts['queued'] + counts['combat'],
        'wounded': fighters.order_by('health', 'id')[:8],
        'fallen': fighters.filter(health=0).count(),
        'top_winners': (Person.objects.annotate(wins=Count('fights_won')).filter(wins__gt=0)
                        .order_by('-wins', 'id')[:5]),
        'live': counts['queued'] + counts['combat'] > 0
                or (counts['pending'] > 0 and 'watch' in request.GET and counts['done'] > 0),
        'watching': 'watch' in request.GET,
    }
    if context['watching'] and counts['pending'] and not counts['done'] and not context['running']:
        context['live'] = True  # the start task has not claimed the fights yet
    return render(request, 'army/simulate.html', context)


def fight_detail(request, fight_id):
    fight = get_object_or_404(Fight.objects.select_related('blue_soldier', 'red_soldier', 'winner'), pk=fight_id)
    events = list(fight.events.select_related('attacker', 'defender'))
    applied = sum(e.applied for e in events)
    return render(request, 'army/fight_detail.html', {
        'nav': 'simulate',
        'fight': fight,
        'events': events,
        'applied': applied,
        'progress': round(applied * 100 / len(events)) if events else 0,
        'live': fight.status != Fight.DONE and fight.started_at is not None,
    })