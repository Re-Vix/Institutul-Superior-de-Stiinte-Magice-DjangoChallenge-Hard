from django.db import models


class Person(models.Model):
    """A person registered from /people. Drafting (migration 0002) turns them into a soldier."""

    # Same id as the war API, so fights can reference soldiers directly.
    id = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=100)
    age = models.PositiveIntegerField()

    # Added by drafting (migration 0002_draft_soldiers)
    rank = models.CharField(max_length=50, null=True, blank=True)
    health = models.IntegerField(default=100)
    initial_health = models.IntegerField(default=100)  # health at draft time, used to reset a simulation
    key = models.CharField(max_length=100, null=True, blank=True)  # hint for the next step (Hellstorm Fireburn)
    drafted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['id']

    RANK_CODES = {'General': 'GEN', 'Lieutenant': 'LT', 'Sergeant': 'SGT', 'Private': 'PVT'}

    @property
    def is_drafted(self):
        return self.drafted_at is not None

    @property
    def rank_code(self):
        if not self.rank:
            return ''
        return self.RANK_CODES.get(self.rank, self.rank[:3].upper())

    @property
    def health_pct(self):
        if not self.initial_health:
            return 0
        return max(0, min(100, round(self.health * 100 / self.initial_health)))

    def __str__(self):
        return f'{self.name} (#{self.id})'


class DownloadBatch(models.Model):
    """Bookkeeping for every batch downloaded from the API, so each batch is fetched exactly once."""

    DRAFT = 'draft'
    FIGHT = 'fight'
    KIND_CHOICES = [(DRAFT, 'Draft'), (FIGHT, 'Fight')]

    QUEUED = 'queued'
    DOWNLOADING = 'downloading'
    RETRYING = 'retrying'
    DOWNLOADED = 'downloaded'
    PROCESSED = 'processed'
    MISSING = 'missing'    # the API has no such batch (past the end)
    SKIPPED = 'skipped'    # speculative batch past the known last batch
    FAILED = 'failed'
    STATUS_CHOICES = [(s, s) for s in (QUEUED, DOWNLOADING, RETRYING, DOWNLOADED, PROCESSED, MISSING, SKIPPED, FAILED)]
    FINISHED = (PROCESSED, MISSING, SKIPPED, FAILED)

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    item_count = models.PositiveIntegerField(null=True, blank=True)
    next_batch = models.IntegerField(null=True, blank=True)
    is_last = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['kind', 'number']
        constraints = [models.UniqueConstraint(fields=['kind', 'number'], name='unique_batch_per_kind')]

    def __str__(self):
        return f'{self.kind} batch {self.number}: {self.status}'


class Fight(models.Model):
    """A fight downloaded from /fight/<batch>: one blue soldier against one red soldier."""

    PENDING = 'pending'
    RUNNING = 'running'
    DONE = 'done'
    STATUS_CHOICES = [(PENDING, 'Pending'), (RUNNING, 'Running'), (DONE, 'Done')]

    id = models.IntegerField(primary_key=True)  # fight_id from the API
    batch = models.PositiveIntegerField()
    blue_soldier = models.ForeignKey(Person, on_delete=models.PROTECT, related_name='fights_as_blue')
    red_soldier = models.ForeignKey(Person, on_delete=models.PROTECT, related_name='fights_as_red')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    blue_damage_dealt = models.PositiveIntegerField(default=0)
    red_damage_dealt = models.PositiveIntegerField(default=0)
    winner = models.ForeignKey(Person, on_delete=models.PROTECT, null=True, blank=True, related_name='fights_won')
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['id']
        indexes = [models.Index(fields=['status'])]

    def __str__(self):
        return f'Fight #{self.id}: {self.blue_soldier_id} vs {self.red_soldier_id}'


class FightEvent(models.Model):
    """One step of a fight: attacker hits defender for `damage` points."""

    fight = models.ForeignKey(Fight, on_delete=models.CASCADE, related_name='events')
    step = models.PositiveIntegerField()  # position inside the fight, events are replayed in this order
    attacker = models.ForeignKey(Person, on_delete=models.PROTECT, related_name='attacks')
    defender = models.ForeignKey(Person, on_delete=models.PROTECT, related_name='defenses')
    damage = models.PositiveIntegerField()

    # Filled in when the event is replayed. `applied` makes replay idempotent:
    # a retried task never applies the same hit twice.
    applied = models.BooleanField(default=False)
    defender_health_after = models.IntegerField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['fight', 'step']
        constraints = [models.UniqueConstraint(fields=['fight', 'step'], name='unique_step_per_fight')]

    def __str__(self):
        return f'Fight #{self.fight_id} step {self.step}: {self.attacker_id} -> {self.defender_id} ({self.damage})'