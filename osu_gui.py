"""Interfaz gráfica local para livelobby (osu_cores) y osuctools.

Uso:  python osu_gui.py        (se abre solo en http://127.0.0.1:8765)

Pone este archivo en una carpeta que contenga las dos carpetas de los repos
(Live_Lobby-main y o-tools-main), o en la raíz de uno de ellos.
Necesita: requests y python-dotenv (los de livelobby).
"""
import contextlib
from dataclasses import asdict
import json
import logging
import os
import re
import sys
import threading
import time
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
    from osu_cores.api.client import OsuClient
    from osu_cores.output.jsonl import JsonlWriter
    from osu_cores.service.watcher import STALE_GAME_SECONDS, MatchWatcher
except Exception as e:  # noqa: BLE001
    MISSING.append(_hint("livelobby (osu_cores)", e))
try:
    from osuctools.reader import load
    from osuctools.salidas.consola import format_clean, format_hardest, format_header, format_mvp, format_torneo
    from osuctools.torneo.resumen import summarize, team_labels, trim
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


def watch_job(mid, out, interval, cid, secret):
    st = WATCH[mid]
    L = lambda m: log(m, mid)  # noqa: E731
    _THREAD_MID[threading.get_ident()] = mid
    try:
        w = MatchWatcher(OsuClient(cid or None, secret or None), mid)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
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
        L(f"siguiendo la partida {mid} -> {out}")
        m = w.backfill()
        cur = next((e.game for e in m.events if e.type == "game" and e.game.id == m.current_game_id
                    and not e.game.is_finished), None)  # si ya había un mapa en juego al empezar a seguir
        if cur:
            st["live"], st["playing"] = _live_of(cur), title_of(cur)
        w._emit("backfill", m)
        while not st["stop"].is_set() and not w.ended:
            if st["stop"].wait(interval):
                break
            w.poll_once()
        if w.ended:  # tanto si se cerró mientras se seguía como si ya estaba cerrada
            st["ended"], st["playing"] = True, None
            L("la partida está cerrada")
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
    ids, errors = [], []
    for raw in re.split(r"[\n,;]+", str(d.get("match", ""))):
        if raw.strip():
            try:
                ids.append(parse_match(raw.strip()))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{raw.strip()}: {e}")
    if not ids and not errors:
        errors.append("pegá el ID o el link de al menos una partida")
    interval = max(1.0, float(d.get("interval") or 5))
    folder = Path((d.get("folder") or "partidas").strip()).expanduser()
    started = []
    for mid in dict.fromkeys(ids):
        old = WATCH.get(mid)
        if old and old["thread"].is_alive():
            errors.append(f"{mid}: ya se está siguiendo")
            continue
        WATCH[mid] = {"id": mid, "stop": threading.Event(), "log": [], "name": None, "playing": None, "live": None,
                      "games": 0, "ended": False, "error": None, "interval": interval,
                      "out": str(folder / f"{mid}.jsonl")}
        t = threading.Thread(target=watch_job, daemon=True, args=(
            mid, WATCH[mid]["out"], interval, (d.get("cid") or "").strip(), (d.get("secret") or "").strip()))
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
              "playing": st["playing"], "live": _fresh_live(st), "games": st["games"], "error": st["error"], "out": st["out"]}
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
    out = {"gid": r.game_id, "title": r.title, "version": r.version, "red": r.red, "blue": r.blue,
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
        "id": m.match_id, "name": m.display_name, "closed": m.closed, "labels": labels,
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
                  "miss": h.avg_misses, "set": h.beatmapset_id} if h else None),
    }


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
        "top": [{"name": p.username, "cc": p.country, "matches": p.matches, "maps": p.maps,
                 "mvps": p.mvps, "cost": p.avg_cost, "ratio": p.avg_ratio, "score": p.avg_score,
                 "acc": p.avg_acc, "miss": p.misses_per_map if p.miss_n else None}
                for p in s.player_totals[:20]],
        "maps": [{"title": t.title, "version": t.version, "stars": t.stars, "plays": t.plays,
                  "score": t.avg_score, "acc": t.avg_acc, "miss": t.avg_misses if t.miss_n else None}
                 for t in s.map_totals[:12]],
    }
    return {"matches": [_dash_match(m, skip) for m in matches], "torneo": torneo,
            "warnings": _relevant(warns, {m.match_id for m in matches})}


ROUTES_POST = {"/api/watch/start": api_watch_start, "/api/watch/stop": api_watch_stop,
               "/api/stats": api_stats,
               "/api/mkdir": api_mkdir, "/api/dashboard": api_dashboard}


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
