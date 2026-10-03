"""Client async pour l'API LinkScale (v1) — doc : https://docs.linkscale.to/

Auth : `Authorization: Bearer lk_xxx`. L'API limite à ~2 requêtes/s par clé :
le client espace lui-même les requêtes (LINKSCALE_MIN_INTERVAL) et retente les 429.
"""
import asyncio
import os
import time
from datetime import datetime, timezone

import aiohttp

BASE_URL = os.getenv("LINKSCALE_BASE_URL", "https://dashboard.linkscale.to/api/v1")
MIN_INTERVAL = float(os.getenv("LINKSCALE_MIN_INTERVAL", "0.55"))


class LinkScaleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def items(data, *keys):
    """Extrait une liste d'une réponse, quelle que soit l'enveloppe."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in (*keys, "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class LinkScaleClient:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._gate = asyncio.Lock()
        self._last = 0.0

    async def start(self):
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=aiohttp.ClientTimeout(total=25),
        )

    async def close(self):
        if self._session:
            await self._session.close()

    async def _throttle(self):
        async with self._gate:
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def _request(self, method: str, path: str, *, params=None, json=None):
        for _ in range(4):
            await self._throttle()
            async with self._session.request(
                method, f"{BASE_URL}{path}", params=params, json=json
            ) as resp:
                if resp.status == 429:
                    await asyncio.sleep(float(resp.headers.get("Retry-After", "1")))
                    continue
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {}
                if resp.status >= 400:
                    msg = (
                        (data.get("message") or data.get("error") or str(data))
                        if isinstance(data, dict)
                        else str(data)
                    )
                    raise LinkScaleError(resp.status, str(msg)[:300])
                return data
        raise LinkScaleError(429, "Limite de requêtes dépassée, réessaie dans un instant.")

    # ---- Liens -------------------------------------------------------------
    async def create_link(self, payload: dict):
        """PUT /links — payload déjà construit (type l_p / d_l, u, domain, url, folder_id…)."""
        return await self._request("PUT", "/links", json=payload)

    async def list_links(self, page=1, limit=10, search=None, folder_id=None):
        params = {"page": page, "limit": limit}
        if search:
            params["search"] = search
        if folder_id:
            params["folder_id"] = folder_id
        return await self._request("GET", "/links", params=params)

    async def all_links(self, max_links=int(os.getenv("LINKS_CAP", "3000"))):
        out, page = [], 1
        while len(out) < max_links:
            res = await self.list_links(page=page, limit=100)
            rows = items(res, "links")
            out.extend(rows)
            pages = (res.get("pagination", {}) if isinstance(res, dict) else {}).get("pages") or 1
            if not rows or page >= pages:
                break
            page += 1
        return out[:max_links]

    async def get_link(self, link_id: str):
        return await self._request("GET", f"/links/{link_id}")

    async def update_link(self, link_id: str, **fields):
        return await self._request("PATCH", f"/links/{link_id}", json=fields)

    async def delete_link(self, link_id: str):
        return await self._request("DELETE", f"/links/{link_id}")

    # ---- Dossiers ----------------------------------------------------------
    async def list_folders(self):
        return await self._request("GET", "/folders")

    async def create_folder(self, name: str):
        return await self._request("POST", "/folders", json={"name": name})

    # ---- Logs de visites (base des stats) ----------------------------------
    async def logs(self, scope: str, scope_id: str | None, since: datetime, until: datetime, max_pages=30):
        """Visites entre `since` et `until`, pagination par curseur (100/page).

        scope : "project" | "link" | "folder". Retourne (lignes, tronqué?).
        L'API ne remonte que 30 jours et limite from/to à 31 jours.
        """
        path = {
            "project": "/logs",
            "link": f"/links/{scope_id}/logs",
            "folder": f"/folders/{scope_id}/logs",
        }[scope]
        rows, cursor = [], None
        for _ in range(max_pages):
            params = {"limit": 100, "from": iso(since), "to": iso(until)}
            if cursor:
                params["last_timestamp"] = cursor
            res = await self._request("GET", path, params=params)
            page = items(res)
            rows.extend(page)
            cursor = res.get("next_cursor") if isinstance(res, dict) else None
            if not page or not cursor or not (isinstance(res, dict) and res.get("has_more")):
                return rows, False
        return rows, True

    async def link_logs(self, link_id: str, limit=10):
        return await self._request("GET", f"/links/{link_id}/logs", params={"limit": limit})

    async def trending_links(self):
        return await self._request("GET", "/trending-links")
