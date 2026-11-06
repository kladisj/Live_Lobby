import sys
import time

from osu_cores.api.client import OsuClient
from osu_cores.service.watcher import MatchWatcher

match_id = int(sys.argv[1])
minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 10


def now():
    return time.strftime("%H:%M:%S")


client = OsuClient()
w = MatchWatcher(client, match_id)
finished = []


def on_start(g):
    print(f"[{now()}] empezó {g.id} | scores ya presentes: {len(g.scores)}")


def on_finish(g):
    finished.append(g.id)
    print(f"[{now()}] terminó {g.id} | scores: {len(g.scores)}")


w.on("game_started", on_start)
w.on("game_finished", on_finish)
w.on("match_ended", lambda e: print(f"[{now()}] partida cerrada"))

base = w.backfill()
done_at_start = {g.id for g in base.games if g.is_finished}
print(f"[{now()}] historial: {len(base.games)} mapas "
      f"({len(base.games) - len(done_at_start)} en juego) | mirando {minutes} min")

deadline = time.time() + minutes * 60
try:
    while time.time() < deadline and not w.ended:
        time.sleep(5)
        w.poll_once()
except KeyboardInterrupt:
    print("\ncortado con Ctrl+C")

w.poll_once()  # última vuelta antes de comparar

final = MatchWatcher(client, match_id).backfill()
expected = {g.id for g in final.games if g.is_finished} - done_at_start
missing = expected - set(finished)
extra = set(finished) - expected

print()
print("terminaron mientras miraba (según el historial final):", len(expected))
print("el watcher anunció como terminados:", len(finished))
print("repetidos:", len(finished) - len(set(finished)))
print("FALTARON:", sorted(missing) or "ninguno")
print("de más:", sorted(extra) or "ninguno")
