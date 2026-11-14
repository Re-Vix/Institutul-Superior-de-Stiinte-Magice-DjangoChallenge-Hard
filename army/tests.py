from threading import Thread
from unittest import mock

from django.test import TestCase, TransactionTestCase, override_settings

from . import tasks
from .models import DownloadBatch, Fight, FightEvent, Person


def make_people(n=4):
    Person.objects.bulk_create([Person(id=i, name=f'Soldier {i}', age=30, health=100, initial_health=100) for i in range(n)])


def fake_response(status_code=200, body=None):
    response = mock.Mock(status_code=status_code)
    response.json.return_value = body
    return response


class FetchTests(TestCase):
    def test_error_hidden_in_a_200_body_means_batch_not_found(self):
        body = {'data': {'message': 'Invalid batch id'}, 'status': 400}
        with mock.patch('army.tasks.requests.get', return_value=fake_response(200, body)):
            with self.assertRaises(tasks.BatchNotFound):
                tasks.fetch('/fight/10')

    def test_server_errors_are_retried(self):
        with mock.patch('army.tasks.requests.get', return_value=fake_response(502, None)):
            with self.assertRaises(tasks.TransientAPIError):
                tasks.fetch('/draft/0')

    def test_valid_body_is_returned(self):
        body = {'data': [], 'next_batch': None, 'status': 200}
        with mock.patch('army.tasks.requests.get', return_value=fake_response(200, body)):
            self.assertEqual(tasks.fetch('/draft/9'), body)


class DraftTests(TestCase):
    def test_draft_batch_adds_rank_and_key(self):
        make_people(2)
        tasks.draft_batch.run(0, [
            {'id': 0, 'name': 'Soldier 0', 'age': 30, 'health': 100, 'rank': 'General'},
            {'id': 1, 'name': 'Hellstorm Fireburn', 'age': 24, 'health': 100, 'rank': 'Private', 'key': 'fight-info'},
        ])
        self.assertEqual(Person.objects.get(id=0).rank, 'General')
        self.assertEqual(Person.objects.get(id=1).key, 'fight-info')
        self.assertTrue(Person.objects.get(id=1).is_drafted)

    def test_a_batch_is_queued_only_once(self):
        with mock.patch.object(tasks.download_draft_batch, 'delay') as delay:
            tasks.dispatch_batch(DownloadBatch.DRAFT, 3)
            tasks.dispatch_batch(DownloadBatch.DRAFT, 3)
        delay.assert_called_once_with(3)


class FightTests(TestCase):
    def setUp(self):
        make_people(2)
        self.fight_data = [{'fight_id': 7, 'blue_soldier_id': 0, 'red_soldier_id': 1, 'steps': [
            {'attacker': 0, 'defender': 1, 'damage': 8},
            {'attacker': 1, 'defender': 0, 'damage': 5},
            {'attacker': 0, 'defender': 1, 'damage': 9},
        ]}]

    def test_saving_the_same_batch_twice_does_not_duplicate_events(self):
        tasks.save_fight_batch.run(0, self.fight_data)
        tasks.save_fight_batch.run(0, self.fight_data)
        self.assertEqual(Fight.objects.count(), 1)
        self.assertEqual(FightEvent.objects.count(), 3)

    @override_settings(SIMULATION_STEP_DELAY=0)
    def test_replaying_a_fight_twice_applies_each_hit_once(self):
        tasks.save_fight_batch.run(0, self.fight_data)
        tasks.simulate_fight.run(7)
        tasks.simulate_fight.run(7)
        self.assertEqual(Person.objects.get(id=1).health, 100 - 8 - 9)
        self.assertEqual(Person.objects.get(id=0).health, 100 - 5)
        fight = Fight.objects.get(id=7)
        self.assertEqual((fight.blue_damage_dealt, fight.red_damage_dealt, fight.winner_id), (17, 5, 0))

    def test_health_never_goes_below_zero(self):
        Person.objects.filter(id=1).update(health=3)
        tasks.save_fight_batch.run(0, self.fight_data)
        tasks.apply_event(FightEvent.objects.get(fight_id=7, step=0))
        self.assertEqual(Person.objects.get(id=1).health, 0)

    def test_start_simulation_claims_each_fight_once(self):
        tasks.save_fight_batch.run(0, self.fight_data)
        with mock.patch('army.tasks.group') as group:
            self.assertEqual(tasks.start_simulation.run(), 1)
            self.assertEqual(tasks.start_simulation.run(), 0)  # second click: nothing left to claim
        self.assertEqual(group.call_count, 1)

    @override_settings(SIMULATION_STEP_DELAY=0)
    def test_reset_restores_health_and_fights(self):
        tasks.save_fight_batch.run(0, self.fight_data)
        tasks.simulate_fight.run(7)
        tasks.reset_simulation()
        self.assertEqual(Person.objects.get(id=1).health, 100)
        self.assertEqual(Fight.objects.get(id=7).status, Fight.PENDING)
        self.assertFalse(FightEvent.objects.filter(applied=True).exists())


@override_settings(SIMULATION_STEP_DELAY=0)
class ConcurrencyTests(TransactionTestCase):
    """The same soldier defends in many fights replayed by parallel threads: no hit may be lost."""

    def test_parallel_fights_do_not_lose_hits(self):
        make_people(11)
        fights = [{'fight_id': i, 'blue_soldier_id': i, 'red_soldier_id': 0,
                   'steps': [{'attacker': i, 'defender': 0, 'damage': 1} for _ in range(5)]}
                  for i in range(1, 11)]
        tasks.save_fight_batch.run(0, fights)
        Person.objects.filter(id=0).update(health=1000, initial_health=1000)

        threads = [Thread(target=tasks.simulate_fight.run, args=(i,)) for i in range(1, 11)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(Person.objects.get(id=0).health, 1000 - 50)
        self.assertEqual(Fight.objects.filter(status=Fight.DONE).count(), 10)


class PageTests(TestCase):
    def test_pages_render(self):
        make_people(2)
        for url in ['/', '/simulate', '/simulate/', '/simulate?status=done', '/simulate?status=nope']:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_fight_report_renders(self):
        make_people(2)
        tasks.save_fight_batch.run(0, [{'fight_id': 3, 'blue_soldier_id': 0, 'red_soldier_id': 1,
                                        'steps': [{'attacker': 0, 'defender': 1, 'damage': 4}]}])
        self.assertEqual(self.client.get('/simulate/3').status_code, 200)
        self.assertEqual(self.client.get('/simulate/999').status_code, 404)

    def test_draft_cannot_be_started_twice_while_running(self):
        DownloadBatch.objects.create(kind=DownloadBatch.DRAFT, number=0, status=DownloadBatch.DOWNLOADING)
        with mock.patch.object(tasks.start_draft, 'delay') as delay:
            self.client.post('/action/draft/')
            delay.assert_not_called()
            self.client.post('/action/draft/', {'force': '1'})
            delay.assert_called_once()