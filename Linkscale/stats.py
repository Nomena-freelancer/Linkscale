"""Calcul des statistiques à partir des lignes de logs renvoyées par l'API.

Chaque ligne = une visite : timestamp, country, device_type, bot (0/1), id (= id du lien),
clicks[] (clics de la visite). Les pays/appareils ne comptent que les visites humaines.
"""
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# période -> (durée, taille d'un point du graphique, libellé du point)
PERIODS = {
    "1h": (timedelta(hours=1), timedelta(minutes=5), "5 min"),
    "24h": (timedelta(hours=24), timedelta(hours=1), "1 h"),
    "7d": (timedelta(days=7), timedelta(hours=6), "6 h"),
    # l'API plafonne l'historique à 30 jours : on reste juste en dessous
    "30d": (timedelta(days=29, hours=22), timedelta(days=1), "1 jour"),
}
PERIOD_LABELS = {"1h": "1 heure", "24h": "24 heures", "7d": "7 jours", "30d": "30 jours"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Stats:
    visits: int = 0
    humans: int = 0
    bots: int = 0
    clicks: int = 0
    converted: int = 0  # visites humaines avec au moins 1 clic
    countries: Counter = field(default_factory=Counter)
    devices: Counter = field(default_factory=Counter)
    series: list = field(default_factory=list)  # [(début_bucket, humains, bots)]
    link_humans: Counter = field(default_factory=Counter)
    link_clicks: Counter = field(default_factory=Counter)
    last_visit: datetime | None = None

    @property
    def bot_rate(self) -> float:
        return self.bots / self.visits if self.visits else 0.0

    @property
    def ctr(self) -> float:
        return self.converted / self.humans if self.humans else 0.0


def compute(rows, since: datetime, until: datetime, bucket: timedelta) -> Stats:
    n = max(1, math.ceil((until - since) / bucket))
    humans, bots = [0] * n, [0] * n
    s = Stats()
    for r in rows:
        ts = parse_ts(r.get("timestamp"))
        if ts is None or ts < since:
            continue
        idx = min(n - 1, int((ts - since) / bucket))
        clicks = r.get("clicks") or []
        s.visits += 1
        if s.last_visit is None or ts > s.last_visit:
            s.last_visit = ts
        if r.get("bot"):
            s.bots += 1
            bots[idx] += 1
            continue
        s.humans += 1
        humans[idx] += 1
        s.clicks += len(clicks)
        if clicks:
            s.converted += 1
        s.countries[r.get("country") or "??"] += 1
        s.devices[r.get("device_type") or "inconnu"] += 1
        link = str(r.get("id") or "")
        if link:
            s.link_humans[link] += 1
            s.link_clicks[link] += len(clicks)
    s.series = [(since + i * bucket, humans[i], bots[i]) for i in range(n)]
    return s
