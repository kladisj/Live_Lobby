from collections import Counter

from osu_cores.api.client import OsuClient
from osu_cores.service.watcher import MatchWatcher

match = MatchWatcher(OsuClient(), 122095688).backfill()

print(match.name)
print("eventos:", len(match.events), "| mapas:", len(match.games),
      "| jugadores:", len(match.players))
print("primer evento:", match.events[0].id, match.events[0].type)
print("último evento:", match.events[-1].id, match.events[-1].type)
print("first_event_id del match:", match.first_event_id)
print("tipos:", Counter(e.type for e in match.events))

ids = [e.id for e in match.events]
print("ordenado y sin duplicados:", ids == sorted(set(ids)))
