from django.contrib import admin

from .models import DownloadBatch, Fight, FightEvent, Person


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'age', 'rank', 'health', 'key', 'drafted_at')
    list_filter = ('rank',)
    search_fields = ('name',)


@admin.register(DownloadBatch)
class DownloadBatchAdmin(admin.ModelAdmin):
    list_display = ('kind', 'number', 'status', 'attempts', 'item_count', 'next_batch', 'is_last', 'updated_at')
    list_filter = ('kind', 'status')


class FightEventInline(admin.TabularInline):
    model = FightEvent
    extra = 0


@admin.register(Fight)
class FightAdmin(admin.ModelAdmin):
    list_display = ('id', 'batch', 'blue_soldier', 'red_soldier', 'status', 'blue_damage_dealt', 'red_damage_dealt', 'winner')
    list_filter = ('status', 'batch')
    inlines = [FightEventInline]
