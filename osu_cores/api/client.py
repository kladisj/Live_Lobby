import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://osu.ppy.sh/api/v2"
TOKEN_URL = "https://osu.ppy.sh/oauth/token"


class OsuApiError(Exception):
    """Error al hablar con la API de osu!."""


class OsuClient:
    def __init__(self, client_id=None, client_secret=None, max_retries=3, timeout=15):
        self.client_id = client_id or os.environ["OSU_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["OSU_CLIENT_SECRET"]
        self.max_retries = max_retries
        self.timeout = timeout
        self._token = None
        self._token_expires_at = 0.0

    # --- token ---------------------------------------------------------
    def _fetch_token(self):
        r = requests.post(
            TOKEN_URL,
            json={
                "client_id": int(self.client_id),
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
                "scope": "public",
            },
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise OsuApiError(f"No se pudo obtener el token ({r.status_code}): {r.text}")
        data = r.json()
        self._token = data["access_token"]
        # margen de 60 s para no usar un token justo antes de que venza
        self._token_expires_at = time.time() + data["expires_in"] - 60

    def _get_token(self):
        if self._token is None or time.time() >= self._token_expires_at:
            self._fetch_token()
        return self._token

    # --- requests ------------------------------------------------------
    def _request(self, path, params=None):
        url = f"{BASE_URL}{path}"
        refreshed = False

        for attempt in range(self.max_retries + 1):
            try:
                r = requests.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._get_token()}"},
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise OsuApiError(f"Error de red: {e}") from e
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 200:
                return r.json()

            if r.status_code == 401 and not refreshed:
                # token inválido o vencido: lo renovamos una sola vez
                refreshed = True
                self._token = None
                continue

            if r.status_code == 404:
                raise OsuApiError(f"No encontrado: {path}")

            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.max_retries:
                    raise OsuApiError(f"Falló tras reintentos ({r.status_code})")
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                time.sleep(wait)
                continue

            raise OsuApiError(f"Respuesta inesperada ({r.status_code}): {r.text}")

        raise OsuApiError("Se agotaron los reintentos")

    # --- endpoints -----------------------------------------------------
    def get_match(self, match_id, after=None, before=None, limit=None):
        """Devuelve el JSON crudo de /matches/{id}."""
        params = {"after": after, "before": before, "limit": limit}
        return self._request(f"/matches/{match_id}", params=params)
