from django.contrib import messages
from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import tasks
from .models import DownloadBatch, Fight, FightEvent, Person


def home(request):
    batches = DownloadBatch.objects.all()
    draft_batches = [b for b in batches if b.kind == DownloadBatch.DRAFT]
    fight_batches = [b for b in batches if b.kind == DownloadBatch.FIGHT]
    busy = any(b.status not in DownloadBatch.FINISHED for b in batches)
    context = {
        'people_count': Person.objects.count(),
        'drafted_count': Person.objects.filter(drafted_at__isnull=False).count(),
        'fight_count': Fight.objects.count(),
        'event_count': FightEvent.objects.count(),
        'key_holders': Person.objects.filter(key__isnull=False).exclude(key=''),
        'draft_batches': draft_batches,
        'fight_batches': fight_batches,
        'soldiers': Person.objects.all(),
        # ?watch=1 is added after pressing a button, so the page keeps refreshing
        # until the background tasks have created their first batches.
        'auto_refresh': busy or 'watch' in request.GET,
    }
    return render(request, 'army/home.html', context)


ACTIONS = {
    'register': (tasks.register_people, 'Registering people in the background.'),
    'draft': (tasks.start_draft, 'Drafting started: every batch is downloaded and drafted in its own task.'),
    'fights': (tasks.start_fight_download, 'Downloading fights in the background.'),
}


@require_POST
def run_action(request, action):
    task, message = ACTIONS[action]
    task.delay()
    messages.success(request, message)
    return redirect(reverse('home') + '?watch=1')


def simulate(request):
    if request.method == 'POST':
        if request.POST.get('action') == 'reset':
            tasks.reset_simulation()
            messages.success(request, 'Simulation reset: every soldier is back to the health from the draft.')
        else:
            tasks.start_simulation.delay()
            messages.success(request, 'Simulation started: one background task per fight, running in parallel.')
        return redirect(reverse('simulate') + '?watch=1')

    fights = (Fight.objects.select_related('blue_soldier', 'red_soldier', 'winner')
              .annotate(events_total=Count('events'),
                        events_applied=Count('events', filter=Q(events__applied=True))))
    by_status = dict(Fight.objects.values_list('status').annotate(n=Count('id')))
    running, done = by_status.get(Fight.RUNNING, 0), by_status.get(Fight.DONE, 0)
    pending = by_status.get(Fight.PENDING, 0)

    done_fights = Fight.objects.filter(status=Fight.DONE)
    team_stats = {
        'blue_wins': done_fights.filter(winner=F('blue_soldier')).count(),
        'red_wins': done_fights.filter(winner=F('red_soldier')).count(),
        'draws': done_fights.filter(winner__isnull=True).count(),
        'blue_damage': done_fights.aggregate(s=Sum('blue_damage_dealt'))['s'] or 0,
        'red_damage': done_fights.aggregate(s=Sum('red_damage_dealt'))['s'] or 0,
    }
    fighters = (Person.objects.filter(Q(fights_as_blue__isnull=False) | Q(fights_as_red__isnull=False))
                .distinct().order_by('health', 'id'))

    context = {
        'fights': fights,
        'pending': pending, 'running': running, 'done': done,
        'team_stats': team_stats,
        'fighters': fighters,
        'auto_refresh': running > 0 or (pending > 0 and (done > 0 or 'watch' in request.GET)),
    }
    return render(request, 'army/simulate.html', context)


def fight_detail(request, fight_id):
    fight = get_object_or_404(Fight.objects.select_related('blue_soldier', 'red_soldier', 'winner'), pk=fight_id)
    events = fight.events.select_related('attacker', 'defender')
    return render(request, 'army/fight_detail.html', {
        'fight': fight, 'events': events, 'auto_refresh': fight.status != Fight.DONE and fight.started_at,
    })