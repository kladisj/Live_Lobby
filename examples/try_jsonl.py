import sys

from osu_cores.api.client import OsuClient
from osu_cores.output.jsonl import JsonlWriter
from osu_cores.service.watcher import MatchWatcher

match_id = int(sys.argv[1])
out = sys.argv[2] if len(sys.argv) > 2 else "partida.jsonl"

w = MatchWatcher(OsuClient(), match_id)
JsonlWriter(out, w)
print("escribiendo en", out, "(Ctrl+C para cortar)")
w.run(interval=5)
print("listo")
