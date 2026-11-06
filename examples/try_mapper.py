from osu_cores.api.client import OsuClient
from osu_cores.mapper import map_match

match = map_match(OsuClient().get_match(122095688))
names = {p.id: p.username for p in match.players}

print(match.name, "| jugadores:", len(match.players), "| mapas:", len(match.games))

last = match.games[-1]
b = last.beatmap
print("último mapa:", b.title if b else None, "-", b.version if b else None,
      "| terminado:", last.is_finished)

for s in sorted(last.scores, key=lambda s: s.score, reverse=True):
    print(f"{names.get(s.user_id, s.user_id)!s:<20} {s.team!s:<5} {s.score:>9,}  {s.accuracy * 100:5.2f}%")
