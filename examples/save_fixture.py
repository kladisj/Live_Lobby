import json
from pathlib import Path

from osu_cores.api.client import OsuClient

out = Path("tests/fixtures")
out.mkdir(parents=True, exist_ok=True)

raw = OsuClient().get_match(122095688)
with open(out / "match_finished.json", "w") as f:
    json.dump(raw, f, indent=2)

print("guardado:", out / "match_finished.json")
