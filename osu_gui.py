"""Interfaz gráfica local para livelobby (osu_cores) y osuctools.

Uso:  python osu_gui.py        (se abre solo en http://127.0.0.1:8765)

Pone este archivo en una carpeta que contenga las dos carpetas de los repos
(Live_Lobby-main y o-tools-main), o en la raíz de uno de ellos.
Necesita: requests y python-dotenv (los de livelobby).
"""
import contextlib
import csv
import io
from dataclasses import asdict
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
PORT = 8765

# --- encontrar los paquetes ---------------------------------------------------
# Busca osu_cores y osuctools en: esta carpeta, sus subcarpetas (hasta 2 niveles),
# la carpeta de arriba y las hermanas de esta (también hasta 2 niveles). Así
# funciona tanto si el archivo está en una carpeta con los dos repos, como dentro
# de uno de ellos, o si el zip se descomprimió con una carpeta de más.
_SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", "tests", "examples"}


def _subdirs(folder):
    try:
        return [d for d in sorted(folder.iterdir())
                if d.is_dir() and not d.name.startswith(".") and d.name not in _SKIP]
    except OSError:
        return []


def _search_roots():
    seen, out = set(), []
    for top in (HERE, HERE.parent):
        level1 = _subdirs(top)
        for folder in [top, *level1, *(d for c in level1 for d in _subdirs(c))]:
            if folder not in seen:
                seen.add(folder)
                out.append(folder)
    return out


for base in _search_roots():
    if (base / "osu_cores").is_dir() or (base / "osuctools").is_dir():
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))

MISSING = []


def _hint(nombre, e):
    """Mensaje claro según la causa del fallo al importar."""
    faltante = getattr(e, "name", None) or ""
    if faltante in ("osu_cores", "osuctools"):
        return (f"{nombre}: no encontré la carpeta del repo. Poné este archivo en la misma "
                f"carpeta que Live_Lobby-main y o-tools-main (o dentro de una de las dos).")
    if faltante:
        return (f"{nombre}: falta instalar '{faltante}'. Cerrá esto y corré: "
                f"{Path(sys.executable).name} -m pip install requests python-dotenv")
    return f"{nombre}: {e}"


try:
    from osu_cores.__main__ import format_result, parse_match, title_of
    from osu_cores.api.client import OsuBlockedError, OsuClient
    from osu_cores.output.jsonl import JsonlWriter
    from osu_cores.service.watcher import STALE_GAME_SECONDS, MatchWatcher
except Exception as e:  # noqa: BLE001
    MISSING.append(_hint("livelobby (osu_cores)", e))
try:
    from osuctools.reader import load
    from osuctools.salidas.consola import format_clean, format_hardest, format_header, format_mvp, format_torneo
    from osuctools.torneo.resumen import summarize, team_labels, trim
    from osuctools.torneo.pscore import TOTAL, pools_from_rounds, pscore_report
    from osuctools.stats import clean_players, hardest_maps, match_costs, team_series
except Exception as e:  # noqa: BLE001
    MISSING.append(_hint("osuctools", e))

# --- seguimiento en vivo (hilo en segundo plano) -------------------------------
WATCH = {}  # id de la partida -> estado de su seguimiento (hilo, log, nombre, mapa en juego...)
_THREAD_MID = {}  # id del hilo -> id de la partida (para atribuir los avisos de livelobby)
LOG0 = []  # avisos que no pertenecen a ninguna partida


def log(msg, mid=None):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    (WATCH[mid]["log"] if mid in WATCH else LOG0).append(line)


class _GuiLog(logging.Handler):
    """Muestra en el registro de la interfaz los avisos de livelobby (por ejemplo,
    si no puede escribir el archivo), que si no quedarían ocultos."""

    def emit(self, record):
        msg = record.getMessage()
        if record.exc_info and record.exc_info[1]:
            msg += f": {record.exc_info[1]}"
        log(f"AVISO: {msg}", _THREAD_MID.get(threading.get_ident()))


logging.getLogger("osu_cores").addHandler(_GuiLog(level=logging.WARNING))


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


@contextlib.contextmanager
def _warnings_of(name="osuctools"):
    """Junta los avisos del lector (por ejemplo, una corrección que no se pudo aplicar)."""
    handler, logger = _Collect(), logging.getLogger(name)
    logger.addHandler(handler)
    try:
        yield handler.msgs
    finally:
        logger.removeHandler(handler)


def _relevant(warns, match_ids):
    """Un archivo de correcciones suele cubrir varias partidas: se descartan los
    avisos de las que no se cargaron, que no son un problema."""
    keep = []
    for w in dict.fromkeys(warns):
        m = re.search(r"apunta a la partida (\d+)", w)
        if m and int(m.group(1)) not in match_ids:
            continue
        keep.append(w)
    return keep


def _with_warnings(text, warns):
    warns = list(dict.fromkeys(warns))
    if warns:
        text += f"\n\nAvisos ({len(warns)}):\n" + "\n".join(f"- {w}" for w in warns)
    return {"text": text}


def _live_of(g):
    """Datos del mapa que se está jugando (para mostrarlo como en la página de osu!)."""
    b = g.beatmap
    return {"gid": g.id, "start": g.start_time, "title": b.title if b else None,
            "artist": b.artist if b else None, "version": b.version if b else None,
            "stars": b.difficulty_rating if b else None, "set": b.beatmapset_id if b else None,
            "length": b.length_seconds if b else None}


def _fresh_live(st):
    """El mapa en juego, salvo que el seguimiento esté parado o el mapa sea tan viejo que se dé por abandonado."""
    lv = st["live"]
    if not lv or not st["thread"].is_alive():
        return None
    try:
        from datetime import datetime
        age = time.time() - datetime.fromisoformat(lv["start"].replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return lv
    return lv if age < STALE_GAME_SECONDS else None


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _safe_filename(name, limit=120):
    """Deja un texto usable como nombre de archivo en Windows, Mac y Linux (sin la extensión)."""
    name = re.sub(r"\.jsonl$", "", str(name or "").strip(), flags=re.I)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    name = name[:limit].rstrip(" .-")
    if name.upper() in _RESERVED:
        name += "_"
    return name


def _jsonl_match_id(path):
    """El match_id que dice la primera línea de un .jsonl (None si no se puede leer)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    return rec.get("match_id") if isinstance(rec, dict) else None
    except (OSError, ValueError):
        pass
    return None


def _existing_file(folder, mid):
    """Un .jsonl de esta partida que ya esté en la carpeta (con el nombre que tenga), para seguir en él."""
    try:
        for p in sorted(folder.glob("*.jsonl")):
            if _jsonl_match_id(p) == mid:
                return p
    except OSError:
        pass
    return None


def _out_path(folder, mid, mode, custom, lobby_name):
    """Dónde guardar la partida. Si ya hay un archivo de esa partida en la carpeta se sigue usando
    (así no se duplica). Si no: el nombre elegido, el nombre del lobby o, sin nada, el ID solo."""
    if (old := _existing_file(folder, mid)):
        return old
    base = ""
    if custom:
        base = _safe_filename(custom)
    elif mode == "lobby":
        base = _safe_filename(lobby_name)
    base = base or str(mid)
    path = folder / f"{base}.jsonl"
    other = _jsonl_match_id(path) if path.exists() else None
    if path.exists() and other != mid:  # otro lobby con el mismo nombre: se distingue con el ID
        path = folder / f"{base} ({mid}).jsonl"
    return path


class _Stop(Exception):
    """El usuario detuvo el seguimiento mientras se esperaba."""


def _retry_if_blocked(st, L, fn):
    """Corre fn(). Si Cloudflare bloqueó la IP, espera el enfriamiento (se puede cortar con
    Detener) y reintenta solo, en vez de matar el seguimiento o seguir golpeando la API."""
    while True:
        try:
            return fn()
        except OsuBlockedError as e:
            wait = max(5.0, e.retry_after) + random.uniform(0, 15)  # el azar evita que todas despierten juntas
            st["blocked"] = time.time() + wait
            L(f"BLOQUEO de Cloudflare: pauso {int(wait // 60)} min {int(wait % 60)} s y reintento solo")
            if st["stop"].wait(wait):
                raise _Stop from e
            st["blocked"] = None


def watch_job(mid, folder, mode, custom, interval, cid, secret, delay=0.0):
    st = WATCH[mid]
    L = lambda m: log(m, mid)  # noqa: E731
    _THREAD_MID[threading.get_ident()] = mid
    try:
        if delay and st["stop"].wait(delay):  # arranque escalonado: no largar todas las partidas juntas
            raise _Stop
        w = MatchWatcher(OsuClient(cid or None, secret or None), mid)
        # el historial se baja primero (no avisa a nadie todavía): de ahí sale el nombre del lobby
        m = _retry_if_blocked(st, L, w.backfill)
        existing = _existing_file(folder, mid)
        out = _out_path(folder, mid, mode, custom, getattr(m, "name", None))
        st["out"] = str(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        JsonlWriter(out, w)
        name_of = lambda uid: w.players[uid].username if uid in w.players else str(uid)  # noqa: E731

        def backfilled(m):
            st["name"] = getattr(m, "name", None) or st["name"]
            st["games"] = sum(1 for g in m.games if g.is_finished)  # el mapa en juego todavía no cuenta
            L(f"historial: {len(m.events)} eventos, {len(m.games)} mapas, {len(m.players)} jugadores")

        def started(g):
            if not g.is_finished:
                st["live"] = _live_of(g)
            st["playing"] = title_of(g)
            L(f"empezó {title_of(g)}")

        def finished(g):
            if st["live"] and st["live"]["gid"] == g.id:
                st["live"] = None
            st["playing"] = None
            st["games"] += 1
            L(f"terminó {title_of(g)} | {len(g.scores)} scores")
            st["log"].extend(format_result(g, name_of))

        w.on("backfill", backfilled)
        w.on("game_started", started)
        w.on("game_finished", finished)
        if existing:
            L(f"ya había un archivo de esta partida, sigo en {out.name}")
        L(f"siguiendo la partida {mid} -> {out}")
        cur = next((e.game for e in m.events if e.type == "game" and e.game.id == m.current_game_id
                    and not e.game.is_finished), None)  # si ya había un mapa en juego al empezar a seguir
        if cur:
            st["live"], st["playing"] = _live_of(cur), title_of(cur)
        w._emit("backfill", m)
        while not st["stop"].is_set() and not w.ended:
            if st["stop"].wait(interval * random.uniform(0.9, 1.1)):  # un poco de azar: que no salgan sincronizadas
                break
            _retry_if_blocked(st, L, w.poll_once)
        if w.ended:  # tanto si se cerró mientras se seguía como si ya estaba cerrada
            st["ended"], st["playing"] = True, None
            L("la partida está cerrada")
    except _Stop:
        pass
    except KeyError as e:
        st["error"] = f"falta {e.args[0]} (completá las credenciales o el .env)"
        L(f"ERROR: {st['error']}")
    except Exception as e:  # noqa: BLE001
        st["error"] = str(e)
        L(f"ERROR: {e}")
    st["live"] = None
    L("detenido")


def _falta(prefijo):
    """Si el módulo no se pudo cargar al arrancar, devuelve el motivo (para mostrarlo)."""
    for m in MISSING:
        if m.startswith(prefijo):
            return {"error": m}
    return None


def api_watch_start(d):
    """Empieza a seguir una o varias partidas (una por línea): un hilo y un .jsonl cada una."""
    if (r := _falta("livelobby")):
        return r
    ids, errors, names = [], [], {}
    for line in str(d.get("match", "")).splitlines():
        ref, _, custom = line.partition("|")  # "ID_O_LINK | nombre del archivo" (el nombre es opcional)
        for raw in re.split(r"[,;]+", ref):
            if raw.strip():
                try:
                    mid = parse_match(raw.strip())
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{raw.strip()}: {e}")
                    continue
                ids.append(mid)
                if custom.strip():
                    names[mid] = custom.strip()
    if not ids and not errors:
        errors.append("pegá el ID o el link de al menos una partida")
    mode = "lobby" if d.get("name_mode") == "lobby" else "id"
    interval = max(1.0, float(d.get("interval") or 5))
    folder = Path((d.get("folder") or "partidas").strip()).expanduser()
    started = []
    for mid in dict.fromkeys(ids):
        old = WATCH.get(mid)
        if old and old["thread"].is_alive():
            errors.append(f"{mid}: ya se está siguiendo")
            continue
        WATCH[mid] = {"id": mid, "stop": threading.Event(), "log": [], "name": None, "playing": None, "live": None,
                      "games": 0, "ended": False, "error": None, "interval": interval, "blocked": None,
                      "out": str(folder / f"{_safe_filename(names.get(mid)) or mid}.jsonl")}  # provisorio; se ajusta al arrancar
        t = threading.Thread(target=watch_job, daemon=True, args=(
            mid, folder, mode, names.get(mid), interval, (d.get("cid") or "").strip(), (d.get("secret") or "").strip(),
            2.0 * len(started)))  # cada partida arranca 2 s después de la anterior
        WATCH[mid]["thread"] = t
        t.start()
        started.append(mid)
    return {"started": started, "errors": errors, "folder": str(folder.resolve())}


def api_watch_stop(d):
    """Detiene una partida (si viene "id") o todas."""
    targets = [WATCH[d["id"]]] if d.get("id") in WATCH else list(WATCH.values())
    for st in targets:
        st["stop"].set()
    return {"ok": True}


def api_watchers(_):
    items = [{"id": st["id"], "name": st["name"], "running": st["thread"].is_alive(), "ended": st["ended"],
              "playing": st["playing"], "live": _fresh_live(st), "games": st["games"], "error": st["error"], "out": st["out"],
              "blocked": max(0, int(st["blocked"] - time.time())) if st.get("blocked") else 0}
             for st in WATCH.values()]
    per_min = sum(60 / st["interval"] for st in WATCH.values() if st["thread"].is_alive())
    return {"items": items, "per_min": round(per_min)}


def api_log(q):
    mid, since = int((q.get("id") or ["0"])[0]), int((q.get("since") or ["0"])[0])
    lines = WATCH[mid]["log"] if mid in WATCH else LOG0
    return {"lines": lines[since:], "next": len(lines)}


# --- estadísticas y gráfico ----------------------------------------------------
def _paths(d):
    return [p.strip() for p in (d.get("paths") or "").splitlines() if p.strip()]


def api_stats(d):
    if (r := _falta("osuctools")):
        return r
    paths = _paths(d)
    if not paths:
        return {"error": "indicá al menos un archivo o carpeta .jsonl"}
    try:
        with _warnings_of() as warns:
            matches = load(*paths)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    if not matches:
        return {"error": "no encontré partidas en esas rutas"}
    warns = _relevant(warns, {m.match_id for m in matches})
    top = int(d.get("top") or 0) or None
    skip = int(d.get("skip") or 0)
    if d.get("kind") == "torneo":
        summary = summarize(matches, skip_first=skip)
        return _with_warnings("\n".join(format_torneo(summary, top=top or 10)), warns)
    if d.get("kind") == "mvp":
        lines = []
        for m in matches:
            labels = team_labels(trim(m, skip))  # el país de cada equipo, si se puede saber
            lines += format_mvp(m, match_costs(m, skip_first=skip), top=top or 10,
                                labels=labels) + [""]
        return _with_warnings("\n".join(lines).rstrip(), warns)
    lines = format_header(matches)
    if d.get("kind") == "limpios":
        lines += format_clean(clean_players(matches, min_maps=int(d.get("min_maps") or 1)),
                              top=top or 10)
    else:
        lines += format_hardest(hardest_maps(matches, by=d.get("orden") or "auto"), top=top or 5)
    return _with_warnings("\n".join(lines), warns)


def api_files(_):
    found = [str(p) for p in Path.cwd().rglob("*.jsonl")
             if ".venv" not in p.parts][:200]
    return {"files": found, "cwd": str(Path.cwd())}


def _shortcuts():
    home = Path.home()
    cand = [("Inicio", home), ("Escritorio", home / "Desktop"), ("Escritorio", home / "Escritorio"),
            ("Descargas", home / "Downloads"), ("Descargas", home / "Descargas"),
            ("Documentos", home / "Documents"), ("Documentos", home / "Documentos"),
            ("Carpeta del programa", HERE), ("Carpeta de trabajo", Path.cwd())]
    if os.name == "nt":
        cand += [(f"Disco {c}:", Path(f"{c}:\\")) for c in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                 if os.path.exists(f"{c}:\\")]
    else:
        cand.append(("Raíz /", Path("/")))
    out, seen = [], set()
    for name, p in cand:
        try:
            if p.is_dir() and str(p) not in seen:
                seen.add(str(p))
                out.append({"name": name, "path": str(p)})
        except OSError:
            pass
    return out


def api_browse(q):
    """Lista las carpetas y los archivos de una ruta, para el explorador de la interfaz."""
    raw = (q.get("path") or [""])[0].strip().strip('"')
    ext = ((q.get("ext") or [".jsonl"])[0] or ".jsonl").lower()
    p = Path(raw).expanduser() if raw else Path.cwd()
    try:
        while not p.is_dir() and p != p.parent:  # si no existe (o es un archivo), sube hasta una carpeta
            p = p.parent
        p = p.resolve()
        entries, count = [], 0
        for c in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if c.name.startswith("."):
                continue
            try:
                if c.is_dir():
                    entries.append({"name": c.name, "dir": True, "path": str(c)})
                elif c.suffix.lower() == ext:
                    count += 1
                    entries.append({"name": c.name, "dir": False, "path": str(c),
                                    "size": c.stat().st_size})
            except OSError:
                continue
    except PermissionError:
        return {"error": "sin permiso para entrar a esa carpeta"}
    except OSError as e:
        return {"error": str(e)}
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "sep": os.sep, "entries": entries,
            "count": count, "shortcuts": _shortcuts()}


def api_mkdir(d):
    name = (d.get("name") or "").strip()
    if not name or any(ch in name for ch in '/\\:*?"<>|') or name in (".", ".."):
        return {"error": "nombre de carpeta no válido"}
    target = Path(d.get("path") or ".") / name
    target.mkdir(parents=False, exist_ok=True)
    return {"path": str(target.resolve())}


def _score_row(m, s):
    """Un score para el popup de detalle del mapa."""
    st = s.statistics or {}
    return {"uid": s.user_id, "name": m.username(s.user_id), "cc": m.countries.get(s.user_id),
            "team": s.team, "score": s.score, "acc": s.accuracy, "combo": s.max_combo,
            "rank": s.rank, "passed": s.passed, "manual": s.manual, "mods": list(s.mods),
            # great / ok / meh / miss; los scores cargados a mano no tienen estas estadísticas
            "st": ({"g": st.get("count_300", 0), "o": st.get("count_100", 0),
                    "m": st.get("count_50", 0), "x": st.get("count_miss", 0)}
                   if st and not s.manual else None)}


def _map_json(m, g, r):
    """Un mapa de la línea de tiempo (r) con el detalle de sus jugadores (g) para el popup."""
    out = {"gid": r.game_id, "bid": (g.beatmap_id if g is not None else None), "title": r.title, "version": r.version, "red": r.red, "blue": r.blue,
           "winner": r.winner, "set": r.beatmapset_id, "flipped": r.flipped}
    if g is not None:
        out["d"] = {"artist": g.artist, "stars": g.stars, "start": g.start_time, "end": g.end_time,
                    "scoring": g.scoring_type, "mods": list(g.mods),
                    "scores": [_score_row(m, s) for s in sorted(g.scores, key=lambda s: -s.score)
                               if s.score > 0]}
    return out


def _dash_match(m0, skip):
    m = trim(m0, skip)
    labels, series, costs = team_labels(m), team_series(m), match_costs(m)
    last = series[-1] if series else None
    side, rows = {}, {}
    for g in m.games:
        for s in g.scores:
            if s.score <= 0:
                continue
            side[s.user_id] = s.team or side.get(s.user_id)
            if s.passed:
                r = rows.setdefault(s.user_id, [0, 0, 0.0, 0, 0])
                r[0] += 1; r[1] += s.score; r[2] += s.accuracy
                if not s.manual:
                    r[3] += s.statistics.get("count_miss", 0); r[4] += 1
    hard = hardest_maps([m])
    h = hard[0] if hard else None
    games = {x.id: x for x in m.games}
    return {
        "id": m.match_id, "name": m.display_name, "closed": m.closed, "labels": labels, "start": m.start_time,
        "t0": (m.games[0].start_time if m.games else None),
        "t1": ((m.games[-1].end_time or m.games[-1].start_time) if m.games else None),
        "sources": [str(x) for x in m0.sources],
        "rm": last.red_wins if last else 0, "bm": last.blue_wins if last else 0,
        "maps": [_map_json(m, games.get(r.game_id), r) for r in series],
        "costs": [{"uid": c.user_id, "name": c.username, "team": c.team, "cc": m.countries.get(c.user_id),
                   "maps": c.maps, "total": c.total_maps, "cost": c.match_cost, "ratio": c.avg_ratio}
                  for c in costs],
        "players": [{"uid": u, "name": m.username(u), "team": side.get(u), "cc": m.countries.get(u),
                     "maps": r[0], "score": r[1] / r[0], "acc": r[2] / r[0],
                     "miss": r[3] / r[4] if r[4] else None} for u, r in rows.items()],
        "hard": ({"title": h.title, "version": h.version, "stars": h.stars, "acc": h.avg_accuracy,
                  "miss": h.avg_misses, "set": h.beatmapset_id, "bid": h.beatmap_id} if h else None),
    }


def _ptot(p):
    """Los totales de un jugador en el torneo (para la tabla y para su popup de estadísticas)."""
    return {"uid": p.user_id, "name": p.username, "cc": p.country, "matches": p.matches, "maps": p.maps,
            "mvps": p.mvps, "cost": p.avg_cost, "ratio": p.avg_ratio, "score": p.avg_score,
            "acc": p.avg_acc, "miss": p.misses_per_map if p.miss_n else None}


def _round_overrides(pools):
    """Rondas corregidas a mano en el panel (notas r:<id de partida>); solo las que existen en los pools."""
    notes, _ok = _notes_read()
    out = {}
    for k, v in notes.items():
        if k.startswith("r:") and isinstance(v, dict) and v.get("text") in pools:
            try:
                out[int(k[2:])] = v["text"]
            except ValueError:
                pass
    return out


def _pscore_json(matches):
    """Mejor del torneo: el ranking por pscore de cada ronda y el de todo el torneo (clave TOTAL).

    Va aparte del match cost (que sigue igual). Necesita los mappools: de ahí salen los mapas de cada
    ronda y los slots. Si falla, el resto del dashboard sigue funcionando."""
    try:
        pools = pools_from_rounds(api_pools_get()["rounds"])  # los mismos pools (sin duplicados) que ve el panel
        if not pools:
            return {"rounds": [], "tables": {}, "reason": "Cargá los mappools (abajo) para calcular el pscore."}
        report = pscore_report(matches, pools, overrides=_round_overrides(pools))
        if not report:
            return {"rounds": [], "tables": {}, "reason": "Ninguna partida tiene mapas de los pools cargados."}
        return {"rounds": [k for k in report if k != TOTAL],
                "tables": {k: [{"uid": p.user_id, "name": p.username, "cc": p.country, "maps": p.maps,
                                "tmaps": p.team_maps, "ps": p.pscore, "star": p.star, "score": p.avg_score,
                                "acc": p.avg_acc,
                                "best": ({"rank": p.best.rank, "slot": p.best.slot, "grade": p.best.grade,
                                          "score": p.best.score} if p.best else None)}
                               for p in rows] for k, rows in report.items()}}
    except Exception as e:  # noqa: BLE001
        log(f"pscore: {e}")
        return {"rounds": [], "tables": {}, "error": f"no se pudo calcular el pscore: {e}"}


def api_dashboard(d):
    if (r := _falta("osuctools")):
        return r
    paths = _paths(d)
    if not paths:
        return {"error": "indicá una carpeta o un archivo .jsonl"}
    skip = int(d.get("skip") or 0)
    try:
        with _warnings_of() as warns:
            matches = load(*paths)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    if not matches:
        return {"error": "no encontré partidas en esas rutas"}
    s = summarize(matches, skip_first=skip)
    torneo = {
        "n": s.matches, "games": s.games, "players": s.players, "beatmaps": s.beatmaps,
        "open": s.open_matches, "records": s.records, "skipped": s.skipped_standings,
        "standings": [{**asdict(t), "map_diff": t.map_diff} for t in s.standings],
        "results": [{**asdict(r), "winner": r.winner} for r in s.results],
        "top": [_ptot(p) for p in s.player_totals[:20]],
        "all": [_ptot(p) for p in s.player_totals],  # todos, en orden de cost promedio (puesto = posición + 1)
        "maps": [{"title": t.title, "version": t.version, "stars": t.stars, "plays": t.plays,
                  "score": t.avg_score, "acc": t.avg_acc, "miss": t.avg_misses if t.miss_n else None}
                 for t in s.map_totals[:12]],
    }
    return {"matches": [_dash_match(m, skip) for m in matches], "torneo": torneo,
            "pscore": _pscore_json(matches),  # sin recortar warmups: los mapas fuera del pool ya no cuentan
            "warnings": _relevant(warns, {m.match_id for m in matches})}


# --- notas de equipos y jugadores -----------------------------------------------
# Se guardan en un JSON al lado de este archivo: {"t:CN": {"text": ..., "name": ...}, "p:123": {...}}.
# "t:" = equipo (código de país), "p:" = jugador (ID de osu!). Una clave por nota: guardar una
# nota nunca pisa las demás.
NOTES_FILE = HERE / "notas_equipos.json"
_NOTES_LOCK = threading.Lock()
_NOTE_KEY = re.compile(r"^(t:[A-Z]{2}|p:\d{1,12}|r:\d{1,12})$")  # r: = ronda elegida a mano para una partida


def _notes_read():
    """(notas, legible). Si el archivo existe pero está roto, legible=False (no se pisa a ciegas)."""
    try:
        data = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError):
        return {}, False
    return (data, True) if isinstance(data, dict) else ({}, False)


def api_notes_get(_=None):
    notes, ok = _notes_read()
    return {"notes": notes, "ok": ok}


def api_notes_set(d):
    key = str(d.get("key") or "").strip()
    if not _NOTE_KEY.match(key):
        return {"error": "clave de nota no válida"}
    text = str(d.get("text") or "")[:20000]
    with _NOTES_LOCK:
        notes, ok = _notes_read()
        if not ok:  # archivo ilegible: se guarda una copia antes de empezar de nuevo
            try:
                NOTES_FILE.replace(NOTES_FILE.with_name(NOTES_FILE.name + ".bak"))
            except OSError:
                pass
        if text.strip():
            notes[key] = {"text": text, "name": str(d.get("name") or "")[:80]}
        else:
            notes.pop(key, None)
        tmp = NOTES_FILE.with_name(NOTES_FILE.name + ".tmp")
        try:
            tmp.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, NOTES_FILE)
        except OSError as e:
            return {"error": str(e)}
    return {"ok": True}


# --- mappools (hoja de cálculo del torneo) -----------------------------------------
# La hoja trae un bloque por ronda: fila con el nombre ("ROUND OF 16"), fila de encabezados
# (id, artist - title [difficulty], sr, ..., map id, featured artist, notes) y una fila por mapa
# (NM1, HD2, TB...). Se baja como CSV, se guarda en pools.json y el panel cruza por "map id".
# Los badges ORIGINAL / NEW son imágenes en la hoja y no salen en el CSV: se pueden cargar a mano en
# pools_extra.json  ->  {"5991834": {"tags": ["ORIGINAL", "NEW"]}}
POOLS_FILE = HERE / "pools.json"
POOLS_EXTRA = HERE / "pools_extra.json"
_SLOT = re.compile(r"^[A-Z]{2}\d*$")


def _num(txt):
    m = re.search(r"\d+(?:\.\d+)?", txt or "")
    return float(m.group()) if m else None


_ROUND_ORDER = [r"qualif", r"group|grupo", r"32", r"16", r"quarter|cuartos", r"semi", r"^(?!.*grand).*final", r"grand"]


def _round_rank(name):
    low = str(name).lower()
    return next((i for i, rx in enumerate(_ROUND_ORDER) if re.search(rx, low)), 99)


def dedupe_rounds(rounds):
    """Saca los bloques que son copia exacta de otro (mismos mapas): suelen ser plantillas de rondas futuras,
    a veces en filas ocultas de la hoja, que el CSV exporta igual. Se queda con la ronda más temprana del torneo.
    Cuando publiquen el pool real de una ronda, ya no será igual y aparece sola."""
    keep = {}
    for i, r in enumerate(rounds):
        sig = tuple(sorted(m["bid"] for m in r["maps"]))
        best = keep.get(sig)
        if best is None or (_round_rank(r["name"]), i) < (_round_rank(rounds[best]["name"]), best):
            keep[sig] = i
    return [r for i, r in enumerate(rounds) if i in keep.values()]


def parse_pools_csv(text):
    """CSV de la pestaña de mappools -> [{"name": "ROUND OF 16", "maps": [{...}]}]. Lee por contenido:
    encuentra cada fila de encabezados (la que tiene 'id' y 'map id'), así que no depende de las columnas."""
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text))]
    rounds = []
    for hi, row in enumerate(rows):
        low = [c.lower() for c in row]
        if "map id" not in low or "id" not in low:
            continue
        col = {}
        for i, c in enumerate(low):
            key = ("title" if c.startswith("artist") else c)
            col.setdefault(key, i)
        get = lambda r, k: (r[col[k]] if k in col and col[k] < len(r) else "")  # noqa: E731
        name = ""
        for j in range(hi - 1, -1, -1):  # el nombre de la ronda: primera fila con texto hacia arriba
            name = next((c for c in rows[j] if c), "")
            if name:
                break
        maps = []
        for r in rows[hi + 1:]:
            if not any(r):
                if maps:
                    break
                continue
            slot = get(r, "id").upper()
            bid = _num(get(r, "map id"))
            if not _SLOT.match(slot) or bid is None:
                if maps:
                    break
                continue
            maps.append({"slot": slot, "title": get(r, "title"), "sr": _num(get(r, "sr")), "bpm": _num(get(r, "bpm")),
                         "length": get(r, "length"), "cs": _num(get(r, "cs")), "ar": _num(get(r, "ar")),
                         "od": _num(get(r, "od")), "mapper": get(r, "mapper"), "bid": int(bid),
                         "fa": get(r, "featured artist"), "notes": get(r, "notes")})
        if maps:
            rounds.append({"name": name or f"Pool {len(rounds) + 1}", "maps": maps})
    return dedupe_rounds(rounds)


def _json_file(path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def api_pools_get(_=None):
    data = _json_file(POOLS_FILE, {})
    extra = _json_file(POOLS_EXTRA, {})
    rounds = dedupe_rounds(data.get("rounds") or [])
    for r in rounds:
        for m in r["maps"]:
            m["tags"] = [str(t) for t in (extra.get(str(m["bid"])) or {}).get("tags", [])]
    return {"url": data.get("url", ""), "updated": data.get("updated"), "rounds": rounds}


def _sheet_text(src):
    """CSV de una hoja de Google (pública) o de un archivo .csv local."""
    local = Path(src).expanduser()
    if len(src) < 400 and local.is_file():
        return local.read_text(encoding="utf-8-sig")
    m = re.search(r"/spreadsheets/d/([\w-]+)", src)
    if not m:
        raise ValueError("pegá el link de la hoja de Google (o la ruta de un .csv)")
    gid = re.search(r"[#?&]gid=(\d+)", src)
    url = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={gid.group(1) if gid else 0}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 osu-lobby-tools"})
        text = urllib.request.urlopen(req, timeout=20).read().decode("utf-8-sig")
    except urllib.error.HTTPError as e:
        raise ValueError(f"Google respondió {e.code}: la hoja tiene que estar compartida como «cualquiera con el enlace»") from e
    except (urllib.error.URLError, OSError) as e:
        raise ValueError(f"no pude bajar la hoja: {getattr(e, 'reason', e)}") from e
    if text.lstrip().lower().startswith("<"):  # Google devolvió una página de login en vez del CSV
        raise ValueError("la hoja no es pública: compartila como «cualquiera con el enlace»")
    return text


def api_pools_update(d):
    src = str(d.get("url") or "").strip()
    if not src:
        return {"error": "pegá el link de la hoja de mappools"}
    try:
        rounds = parse_pools_csv(_sheet_text(src))
    except (ValueError, OSError) as e:
        return {"error": str(e)}
    if not rounds:
        return {"error": "no encontré mappools en esa hoja (¿es la pestaña de Mappools?)"}
    tmp = POOLS_FILE.with_name(POOLS_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"url": src, "updated": time.strftime("%Y-%m-%d %H:%M"), "rounds": rounds},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, POOLS_FILE)
    except OSError as e:
        return {"error": str(e)}
    return api_pools_get()


ROUTES_POST = {"/api/watch/start": api_watch_start, "/api/watch/stop": api_watch_stop,
               "/api/stats": api_stats,
               "/api/mkdir": api_mkdir, "/api/dashboard": api_dashboard,
               "/api/notes/set": api_notes_set, "/api/pools/update": api_pools_update}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/":
            try:
                page = PAGE_FILE.read_text(encoding="utf-8").replace("__MISSING__", json.dumps(MISSING))
            except OSError:
                return self._send(500, "Falta osu_panel.html: ponelo junto a osu_gui.py.", "text/plain")
            return self._send(200, page, "text/html")
        if u.path == "/api/watchers":
            return self._send(200, json.dumps(api_watchers(None), ensure_ascii=False))
        if u.path == "/api/log":
            return self._send(200, json.dumps(api_log(parse_qs(u.query))))
        if u.path == "/api/browse":
            return self._send(200, json.dumps(api_browse(parse_qs(u.query)), ensure_ascii=False))
        if u.path == "/api/notes":
            return self._send(200, json.dumps(api_notes_get(), ensure_ascii=False))
        if u.path == "/api/pools":
            return self._send(200, json.dumps(api_pools_get(), ensure_ascii=False))
        if u.path == "/api/files":
            return self._send(200, json.dumps(api_files(None)))
        self._send(404, "{}")

    def do_POST(self):  # noqa: N802
        fn = ROUTES_POST.get(urlparse(self.path).path)
        if not fn:
            return self._send(404, "{}")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            res = fn(json.loads(self.rfile.read(n) or b"{}"))
        except Exception as e:  # noqa: BLE001
            res = {"error": str(e)}
        self._send(200, json.dumps(res, ensure_ascii=False))

    def log_message(self, *a):
        pass


PAGE_FILE = HERE / "osu_panel.html"


def main():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"El puerto {PORT} está ocupado. Cerrá la otra instancia e intentá de nuevo.")
        return 1
    url = f"http://127.0.0.1:{PORT}"
    print(f"osu! Lobby Tools en {url}  (Ctrl+C para salir)")
    if MISSING:
        print("AVISO:", *MISSING, sep="\n  ")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        for st in WATCH.values():
            st["stop"].set()
        print("\nchau")
    return 0


if __name__ == "__main__":
    sys.exit(main())
