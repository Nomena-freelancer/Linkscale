"""Graphiques PNG (matplotlib, thème sombre) — à appeler via asyncio.to_thread."""
import io
import math
import os
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

BG, PANEL, FG, GRID = "#1e1f22", "#2b2d31", "#e3e5e8", "#3f4147"
BOT_COLOR = "#ED4245"
PALETTE = ["#5865F2", "#3BA55D", "#FEE75C", "#EB459E", "#F57C00", "#1ABC9C", "#9B59B6", "#95A5A6"]
TZ = ZoneInfo(os.getenv("BOT_TZ", "UTC"))


def _style(ax, title=None):
    ax.set_facecolor(PANEL)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=FG, labelsize=8)
    ax.grid(axis="y", color=GRID, alpha=0.5, linewidth=0.6)
    ax.set_axisbelow(True)
    if title:
        ax.set_title(title, color=FG, fontsize=10, loc="left", pad=8)


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _empty(ax, text="Aucune donnée"):
    ax.text(0.5, 0.5, text, ha="center", va="center", color=FG, alpha=0.6, transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])


def dashboard_png(stats, title: str, color: str, bucket_label: str, period_label: str) -> bytes:
    fig = plt.figure(figsize=(11, 6.4), dpi=110, facecolor=BG)
    gs = fig.add_gridspec(2, 3, height_ratios=[1.4, 1], hspace=0.45, wspace=0.3)
    fig.suptitle(f"{title} — {period_label}", color=FG, fontsize=13, x=0.01, ha="left")

    # 1) visites dans le temps (humains + bots empilés)
    ax = fig.add_subplot(gs[0, :])
    _style(ax, f"Visites par tranche de {bucket_label}")
    if stats.visits:
        n = len(stats.series)
        xs = list(range(n))
        h = [p[1] for p in stats.series]
        b = [p[2] for p in stats.series]
        ax.bar(xs, h, color=color, label="Humains", width=0.8)
        ax.bar(xs, b, bottom=h, color=BOT_COLOR, label="Bots", width=0.8)
        step = max(1, math.ceil(n / 8))
        fmt = "%H:%M" if "min" in bucket_label or bucket_label == "1 h" else "%d/%m %Hh"
        if bucket_label == "1 jour":
            fmt = "%d/%m"
        ax.set_xticks(xs[::step])
        ax.set_xticklabels(
            [stats.series[i][0].astimezone(TZ).strftime(fmt) for i in xs[::step]], rotation=0
        )
        leg = ax.legend(facecolor=PANEL, edgecolor=GRID, labelcolor=FG, fontsize=8, loc="upper left")
        leg.get_frame().set_alpha(0.8)
    else:
        _empty(ax, "Aucune visite sur la période")

    # 2) top pays
    ax = fig.add_subplot(gs[1, 0:2])
    _style(ax, "Top pays (visites humaines)")
    ax.grid(axis="x", color=GRID, alpha=0.5, linewidth=0.6)
    ax.grid(axis="y", visible=False)
    top = stats.countries.most_common(6)
    if top:
        labels = [c for c, _ in top][::-1]
        vals = [v for _, v in top][::-1]
        bars = ax.barh(labels, vals, color=color)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, f" {v}", va="center", color=FG, fontsize=8)
        ax.set_xlim(0, max(vals) * 1.15)
    else:
        _empty(ax)

    # 3) appareils
    ax = fig.add_subplot(gs[1, 2])
    ax.set_facecolor(BG)
    ax.set_title("Appareils", color=FG, fontsize=10, loc="left", pad=8)
    if stats.devices:
        labels, vals = zip(*stats.devices.most_common(6))
        ax.pie(
            vals,
            labels=[f"{l} ({v})" for l, v in zip(labels, vals)],
            colors=[color] + PALETTE[1:],
            wedgeprops=dict(width=0.45, edgecolor=BG),
            textprops=dict(color=FG, fontsize=8),
            startangle=90,
        )
    else:
        _empty(ax)
    return _png(fig)


def ranking_png(title: str, entries, unit: str = "") -> bytes:
    """entries : [(label, valeur, couleur_hex)] déjà triés du meilleur au moins bon."""
    h = max(2.4, 0.5 * len(entries) + 1.2)
    fig, ax = plt.subplots(figsize=(10, h), dpi=110, facecolor=BG)
    _style(ax, title)
    ax.grid(axis="x", color=GRID, alpha=0.5, linewidth=0.6)
    ax.grid(axis="y", visible=False)
    if entries:
        labels = [e[0][:38] for e in entries][::-1]
        vals = [e[1] for e in entries][::-1]
        cols = [e[2] for e in entries][::-1]
        bars = ax.barh(labels, vals, color=cols)
        top = max(vals) or 1
        for bar, v in zip(bars, vals):
            ax.text(bar.get_width() + top * 0.01, bar.get_y() + bar.get_height() / 2,
                    f"{v:,}{unit}".replace(",", " "), va="center", color=FG, fontsize=9)
        ax.set_xlim(0, top * 1.15)
    else:
        _empty(ax)
    return _png(fig)
