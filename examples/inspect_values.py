from osu_cores.api.client import OsuClient

m = OsuClient().get_match(122095688)
ev = next(e for e in m["events"] if e.get("game"))
game = ev["game"]
sc = game["scores"][0]

print("detail:", ev["detail"], "| user_id:", ev["user_id"])
print("game:", {k: v for k, v in game.items() if k not in ("beatmap", "scores")})
print("beatmap keys:", list(game["beatmap"].keys()))
print("score:", {k: v for k, v in sc.items() if k not in ("current_user_attributes", "replay")})

users = m["users"]
sample = users[0] if isinstance(users, list) else next(iter(users.values()))
print("users es:", type(users).__name__, "| ejemplo:", sample)
