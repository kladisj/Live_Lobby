import json

from osu_cores.api.client import OsuClient

m = OsuClient().get_match(122095688)
games = [e for e in m["events"] if e.get("game")]
print("mapas en estos 100 eventos:", len(games))

if games:
    ev = games[0]
    game = ev["game"]
    print("claves del evento:", list(ev.keys()))
    print("claves del game:", list(game.keys()))
    print("cantidad de scores:", len(game["scores"]))
    print("claves de un score:", list(game["scores"][0].keys()))

    with open("game_sample.json", "w") as f:
        json.dump(ev, f, indent=2)
