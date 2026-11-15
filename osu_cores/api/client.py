import os
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://osu.ppy.sh/api/v2"
TOKEN_URL = "https://osu.ppy.sh/oauth/token"

# Cloudflare (el firewall de osu.ppy.sh) bloquea las IP que mandan ráfagas de
# requests o que se anuncian como un script anónimo. Por eso:
#  - nos identificamos con un User-Agent propio;
#  - TODOS los clientes/hilos del proceso comparten un mismo ritmo máximo;
#  - todos comparten el token (un solo POST a /oauth/token);
#  - si llega un bloqueo, todos frenan a la vez en vez de seguir golpeando.
USER_AGENT = "osu-lobby-tools/1.0 (seguimiento personal de lobbies)"
MIN_GAP = float(os.environ.get("OSU_MIN_GAP", "0.5"))  # segundos entre requests (0.5 = máx. 120/min)
BLOCK_COOLDOWN = float(os.environ.get("OSU_BLOCK_COOLDOWN", "600"))  # 1er bloqueo: 10 min
BLOCK_COOLDOWN_MAX = 3600.0  # tope: 1 hora


class OsuApiError(Exception):
    """Error al hablar con la API de osu!."""


class OsuBlockedError(OsuApiError):
    """Cloudflare/la API nos bloqueó. `retry_after` son los segundos que conviene esperar."""

    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


def _short(text, n=200):
    """Recorta el cuerpo de una respuesta para no volcar páginas HTML enteras en un log."""
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "…"


def _is_html(r):
    """La API contesta siempre JSON; un HTML es la página de Cloudflare, no un error de la API."""
    return "text/html" in r.headers.get("content-type", "").lower()


class _Throttle:
    """Estado compartido por todos los clientes del proceso: ritmo, bloqueo y token."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._blocked_until = 0.0
        self._strikes = 0
        self.tokens = {}  # client_id -> (token, vence_en)
        self.token_lock = threading.Lock()

    def check_blocked(self):
        with self._lock:
            remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            raise OsuBlockedError(
                f"En pausa por bloqueo de Cloudflare (faltan {int(remaining)} s)", remaining)

    def wait_turn(self):
        """Reserva el próximo hueco: dos requests nunca salen a menos de MIN_GAP entre sí."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + MIN_GAP
        if slot > now:
            time.sleep(slot - now)

    def block(self, seconds=None):
        """Registra un bloqueo; cada bloqueo seguido duplica la espera (hasta 1 hora)."""
        with self._lock:
            wait = seconds if seconds is not None else min(
                BLOCK_COOLDOWN * (2 ** self._strikes), BLOCK_COOLDOWN_MAX)
            self._strikes += 1
            self._blocked_until = max(self._blocked_until, time.monotonic() + wait)
        return wait

    def ok(self):
        with self._lock:
            self._strikes = 0


THROTTLE = _Throttle()


class OsuClient:
    def __init__(self, client_id=None, client_secret=None, max_retries=3, timeout=15):
        self.client_id = client_id or os.environ["OSU_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["OSU_CLIENT_SECRET"]
        self.max_retries = max_retries
        self.timeout = timeout

    # --- token ---------------------------------------------------------
    def _fetch_token(self):
        THROTTLE.check_blocked()
        THROTTLE.wait_turn()
        r = requests.post(
            TOKEN_URL,
            json={
                "client_id": int(self.client_id),
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
                "scope": "public",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout,
        )
        if r.status_code in (403, 429) and _is_html(r):
            wait = THROTTLE.block()
            raise OsuBlockedError(f"Cloudflare bloqueó la IP ({r.status_code}); pauso {int(wait)} s", wait)
        if r.status_code != 200:
            raise OsuApiError(f"No se pudo obtener el token ({r.status_code}): {_short(r.text)}")
        data = r.json()
        # margen de 60 s para no usar un token justo antes de que venza
        THROTTLE.tokens[self.client_id] = (data["access_token"], time.time() + data["expires_in"] - 60)

    def _get_token(self):
        # con lock: si 10 hilos arrancan juntos, solo uno pide el token y los demás lo reutilizan
        with THROTTLE.token_lock:
            tok = THROTTLE.tokens.get(self.client_id)
            if tok is None or time.time() >= tok[1]:
                self._fetch_token()
                tok = THROTTLE.tokens[self.client_id]
            return tok[0]

    def _drop_token(self):
        with THROTTLE.token_lock:
            THROTTLE.tokens.pop(self.client_id, None)

    # --- requests ------------------------------------------------------
    def _request(self, path, params=None):
        url = f"{BASE_URL}{path}"
        refreshed = False

        for attempt in range(self.max_retries + 1):
            THROTTLE.check_blocked()
            token = self._get_token()
            THROTTLE.wait_turn()
            try:
                r = requests.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise OsuApiError(f"Error de red: {e}") from e
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 200:
                THROTTLE.ok()
                return r.json()

            # Una página HTML con 403/429 es Cloudflare cortándonos: NO se reintenta
            # (reintentar solo alarga el bloqueo). Se frena todo el proceso.
            if r.status_code in (403, 429) and _is_html(r):
                wait = THROTTLE.block()
                raise OsuBlockedError(
                    f"Cloudflare bloqueó la IP ({r.status_code}); pauso {int(wait)} s", wait)

            if r.status_code == 401 and not refreshed:
                # token inválido o vencido: lo renovamos una sola vez
                refreshed = True
                self._drop_token()
                continue

            if r.status_code == 404:
                raise OsuApiError(f"No encontrado: {path}")

            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.max_retries:
                    raise OsuApiError(f"Falló tras reintentos ({r.status_code})")
                wait = min(float(r.headers.get("Retry-After", 2 ** attempt)), 60.0)
                time.sleep(wait)
                continue

            raise OsuApiError(f"Respuesta inesperada ({r.status_code}): {_short(r.text)}")

        raise OsuApiError("Se agotaron los reintentos")

    # --- endpoints -----------------------------------------------------
    def get_match(self, match_id, after=None, before=None, limit=None):
        """Devuelve el JSON crudo de /matches/{id}."""
        params = {"after": after, "before": before, "limit": limit}
        return self._request(f"/matches/{match_id}", params=params)
