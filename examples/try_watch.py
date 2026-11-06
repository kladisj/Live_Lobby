from osu_cores.api.client import OsuClient
from osu_cores.service.watcher import MatchWatcher

w = MatchWatcher(OsuClient(), 122121639)
w.on("backfill", lambda m: print("historial:", len(m.events), "eventos,", len(m.games), "mapas"))
w.on("game_started", lambda g: print("empezó el mapa", g.id))
w.on("game_finished", lambda g: print("terminó el mapa", g.id, "| scores:", len(g.scores)))
w.on("match_ended", lambda e: print("partida cerrada"))
w.run(interval=5)
