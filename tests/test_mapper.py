import json
from pathlib import Path

import pytest

from osu_cores.mapper import map_game, map_match
from osu_cores.models import to_dict

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw():
    with open(FIXTURES / "match_finished.json") as f:
        return json.load(f)


def test_match_header(raw):
    match = map_match(raw)
    assert match.match_id == 122095688
    assert match.name == "OWC2026: (China) VS (France)"
    assert match.latest_event_id == raw["latest_event_id"]
    assert match.current_game_id is None


def test_players_mapped(raw):
    match = map_match(raw)
    assert len(match.players) == 15
    assert all(p.username for p in match.players)


def test_game_events_normalized(raw):
    types = {e.type for e in map_match(raw).events}
    assert "game" in types
    assert "other" not in types  # el mapper lo renombra


def test_games_count_matches_raw(raw):
    expected = sum(1 for e in raw["events"] if e.get("game"))
    assert len(map_match(raw).games) == expected == 5


def test_scores_have_team(raw):
    for game in map_match(raw).games:
        assert game.team_type == "team-vs"
        assert all(s.team in ("red", "blue") for s in game.scores)


def test_accuracy_is_fraction(raw):
    for game in map_match(raw).games:
        assert all(0 <= s.accuracy <= 1 for s in game.scores)


def test_game_in_progress(raw):
    # ATENCIÓN: fixture sintético. Simulo un mapa en curso a partir de uno
    # terminado; el formato real de la API en ese caso no está verificado.
    last = max((e for e in raw["events"] if e.get("game")), key=lambda e: e["id"])
    last["game"]["end_time"] = None
    last["game"]["scores"] = []
    raw["current_game_id"] = last["game"]["id"]

    match = map_match(raw)
    game = next(g for g in match.games if g.id == last["game"]["id"])
    assert not game.is_finished
    assert game.scores == []
    assert match.current_game_id == game.id


def test_game_without_beatmap(raw):
    g = next(e["game"] for e in raw["events"] if e.get("game"))
    g["beatmap"] = None
    assert map_game(g).beatmap is None


def test_to_dict_is_json_serializable(raw):
    json.dumps(to_dict(map_match(raw)))


def test_score_users_exist_in_players(raw):
    # Escrito sin saber si se cumple en toda la partida.
    match = map_match(raw)
    ids = {p.id for p in match.players}
    for game in match.games:
        assert all(s.user_id in ids for s in game.scores)
