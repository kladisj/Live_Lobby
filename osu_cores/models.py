from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Player:
    id: int
    username: str
    country_code: Optional[str] = None


@dataclass
class Score:
    user_id: int
    score: int
    accuracy: float  # fracción entre 0 y 1 (0.996 = 99.6 %)
    max_combo: int
    passed: bool
    rank: Optional[str] = None
    mods: list[str] = field(default_factory=list)
    team: Optional[str] = None  # "red" / "blue"
    slot: Optional[int] = None
    statistics: dict = field(default_factory=dict)


@dataclass
class Beatmap:
    id: int
    beatmapset_id: Optional[int] = None
    title: Optional[str] = None
    artist: Optional[str] = None
    version: Optional[str] = None  # nombre de la dificultad
    difficulty_rating: Optional[float] = None
    length_seconds: Optional[int] = None


@dataclass
class Game:
    id: int
    match_id: int
    beatmap: Optional[Beatmap]
    start_time: str  # ISO 8601, UTC
    end_time: Optional[str]  # None mientras el mapa se está jugando
    mode: str
    scoring_type: str
    team_type: str
    mods: list[str] = field(default_factory=list)
    scores: list[Score] = field(default_factory=list)

    @property
    def is_finished(self) -> bool:
        return self.end_time is not None


@dataclass
class MatchEvent:
    id: int
    type: str  # "game", "player-joined", "player-left", "match-disbanded", ...
    timestamp: str
    user_id: Optional[int] = None
    text: Optional[str] = None
    game: Optional[Game] = None  # solo si type == "game"


@dataclass
class Match:
    match_id: int
    name: str
    start_time: Optional[str]
    end_time: Optional[str]
    first_event_id: int
    latest_event_id: int
    current_game_id: Optional[int]
    players: list[Player] = field(default_factory=list)
    events: list[MatchEvent] = field(default_factory=list)

    @property
    def games(self) -> list[Game]:
        return [e.game for e in self.events if e.game is not None]


def to_dict(obj) -> dict:
    """Convierte cualquier modelo a un dict listo para JSON."""
    return asdict(obj)
