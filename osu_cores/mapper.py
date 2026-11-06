from .models import Beatmap, Game, Match, MatchEvent, Player, Score


def map_player(u: dict) -> Player:
    return Player(id=u["id"], username=u["username"], country_code=u.get("country_code"))


def map_score(s: dict) -> Score:
    m = s.get("match") or {}
    return Score(
        user_id=s["user_id"],
        score=s["score"],
        accuracy=s["accuracy"],
        max_combo=s["max_combo"],
        passed=s["passed"],
        rank=s.get("rank"),
        mods=s.get("mods", []),
        team=m.get("team"),
        slot=m.get("slot"),
        statistics=s.get("statistics", {}),
    )


def map_beatmap(b: dict | None) -> Beatmap | None:
    if not b:
        return None
    bs = b.get("beatmapset") or {}
    return Beatmap(
        id=b["id"],
        beatmapset_id=b.get("beatmapset_id"),
        title=bs.get("title"),
        artist=bs.get("artist"),
        version=b.get("version"),
        difficulty_rating=b.get("difficulty_rating"),
        length_seconds=b.get("total_length"),
    )


def map_game(g: dict) -> Game:
    return Game(
        id=g["id"],
        match_id=g["match_id"],
        beatmap=map_beatmap(g.get("beatmap")),
        start_time=g["start_time"],
        end_time=g.get("end_time"),
        mode=g["mode"],
        scoring_type=g["scoring_type"],
        team_type=g["team_type"],
        mods=g.get("mods", []),
        scores=[map_score(s) for s in g.get("scores") or []],
    )


def map_event(e: dict) -> MatchEvent:
    detail = e.get("detail") or {}
    game = e.get("game")
    return MatchEvent(
        id=e["id"],
        # osu! llama "other" a los mapas jugados: acá lo normalizamos
        type="game" if game else detail.get("type", "unknown"),
        timestamp=e["timestamp"],
        user_id=e.get("user_id"),
        text=detail.get("text"),
        game=map_game(game) if game else None,
    )


def map_match(raw: dict) -> Match:
    m = raw["match"]
    return Match(
        match_id=m["id"],
        name=m["name"],
        start_time=m.get("start_time"),
        end_time=m.get("end_time"),
        first_event_id=raw["first_event_id"],
        latest_event_id=raw["latest_event_id"],
        current_game_id=raw.get("current_game_id"),
        players=[map_player(u) for u in raw.get("users", [])],
        events=[map_event(e) for e in raw.get("events", [])],
    )
