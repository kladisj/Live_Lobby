from osu_cores.api.client import OsuClient
from osu_cores.service.watcher import MatchWatcher

m = MatchWatcher(OsuClient(), 122123807).backfill()
print("current_game_id:", m.current_game_id)
for g in m.games:
    if not g.is_finished:
        print(g.id, g.start_time, "scores:", len(g.scores))
