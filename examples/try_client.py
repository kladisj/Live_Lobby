from collections import Counter

from osu_cores.api.client import OsuClient

client = OsuClient()
m = client.get_match(122095688)

print("nombre:", m["match"]["name"])
print("eventos:", len(m["events"]))
print("first_event_id:", m["first_event_id"], "| latest_event_id:", m["latest_event_id"])
print("current_game_id:", m["current_game_id"])
print("tipos de evento:", Counter(e["detail"]["type"] for e in m["events"]))
