"""Petit stockage JSON local (réglages du bot, pas des données LinkScale).

Les couleurs de dossier sont stockées ICI : la doc de l'API ne montre pas de champ
« couleur » sur les dossiers. Elles servent aux embeds et aux graphiques du bot.
"""
import json
import os
import re
from pathlib import Path

COLOR_NAMES = {
    "rouge": "#ED4245",
    "orange": "#F57C00",
    "jaune": "#FEE75C",
    "vert": "#3BA55D",
    "turquoise": "#1ABC9C",
    "bleu": "#3B82F6",
    "violet": "#9B59B6",
    "rose": "#EB459E",
    "gris": "#95A5A6",
}
DEFAULT_COLOR = "#5865F2"

_DOTS = {
    "🔴": (237, 66, 69),
    "🟠": (245, 124, 0),
    "🟡": (254, 231, 92),
    "🟢": (59, 165, 93),
    "🔵": (59, 130, 246),
    "🟣": (155, 89, 182),
    "🟤": (140, 90, 60),
    "⚫": (30, 30, 30),
    "⚪": (230, 230, 230),
}


def parse_color(value: str | None) -> str | None:
    """Accepte un nom (rouge, bleu…) ou un hexadécimal (#ff8800 / ff8800)."""
    if not value:
        return None
    v = value.strip().lower()
    if v in COLOR_NAMES:
        return COLOR_NAMES[v]
    m = re.fullmatch(r"#?([0-9a-f]{6})", v)
    return f"#{m.group(1).upper()}" if m else None


def color_dot(hex_color: str | None) -> str:
    if not hex_color:
        return "⚪"
    h = hex_color.lstrip("#")
    rgb = tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))
    return min(_DOTS, key=lambda k: sum((a - b) ** 2 for a, b in zip(_DOTS[k], rgb)))


class Store:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.getenv("BOT_DATA_FILE", "bot_data.json"))
        self.data = {"default_domain": None, "domains": [], "folder_colors": {}}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text("utf-8")))
            except (OSError, ValueError):
                pass

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.path)

    # domaines
    def set_default_domain(self, domain: str):
        self.data["default_domain"] = domain
        self._save()

    def add_domain(self, domain: str):
        if domain not in self.data["domains"]:
            self.data["domains"].append(domain)
            self._save()

    # couleurs de dossiers
    def folder_color(self, folder_id: str) -> str | None:
        return self.data["folder_colors"].get(str(folder_id))

    def set_folder_color(self, folder_id: str, hex_color: str):
        self.data["folder_colors"][str(folder_id)] = hex_color
        self._save()
