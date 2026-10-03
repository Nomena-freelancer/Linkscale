"""Logique indépendante de Discord (testable seule)."""
import json
import os
import random
import re
import string
from collections import Counter
from urllib.parse import urlparse

import stats as st
from linkscale_client import LinkScaleError

ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
URL_RE = re.compile(r"^https?://\S+$", re.I)
SUFFIX_LEN = max(2, int(os.getenv("SUFFIX_LEN", "3")))

# clé -> (libellé, description courte)
LINK_TYPES = {
    "landing": ("Landing page", "Page de liens avec boutons"),
    "direct": ("Lien direct", "Redirection immédiate vers l'URL"),
    "verification": ("Lien de vérification", "Page de confirmation avant la destination"),
}

# Le « lien de vérification » = lien direct + porte 1-step activée. La doc de l'API cite le
# champ `1-step-verification-page.enable` mais n'en détaille pas le format : si besoin,
# ajuste-le sans toucher au code via LINKSCALE_VERIFICATION_FIELDS (JSON).
_DEFAULT_VERIF = {"1-step-verification-page": {"enable": True}}
try:
    VERIFICATION_FIELDS = json.loads(os.getenv("LINKSCALE_VERIFICATION_FIELDS") or "") or _DEFAULT_VERIF
except ValueError:
    VERIFICATION_FIELDS = _DEFAULT_VERIF


# ---- Helpers liens -----------------------------------------------------------
def lid(link: dict) -> str:
    return str(link.get("id") or link.get("_id") or "")


def link_label(link: dict) -> str:
    s = re.sub(r"^https?://", "", link.get("short_url") or "")
    return s or str(link.get("u") or lid(link))


def link_folder_ids(link: dict) -> list[str]:
    v = link.get("folders")
    return [str(x) for x in v] if isinstance(v, list) else []


# ---- Noms / suffixes ---------------------------------------------------------
def clean_base(name: str) -> str:
    """« Jenna Tyu! » -> « Jenna_Tyu » (lettres, chiffres, - et _ ; 40 car. max)."""
    s = re.sub(r"\s+", "_", (name or "").strip())
    s = re.sub(r"[^A-Za-z0-9_-]", "", s)
    return s.strip("_-")[:40]


def existing_aliases(links) -> set[str]:
    """Tous les alias déjà pris sur le projet (tous domaines, tous dossiers), en minuscules."""
    out = set()
    for l in links:
        for v in (l.get("u"), urlparse(l.get("short_url") or "").path.strip("/")):
            if v:
                out.add(str(v).lower())
    return out


def generate_aliases(base: str, count: int, taken, suffix_len: int | None = None) -> list[str]:
    """`base_xyz` : suffixe aléatoire (lettres minuscules) jamais présent dans `taken`
    ni dans le lot en cours. Si l'espace de suffixes se sature, le suffixe s'allonge."""
    length = suffix_len or SUFFIX_LEN
    used = {str(t).lower() for t in taken}
    out, collisions = [], 0
    while len(out) < count:
        alias = f"{base}_{''.join(random.choices(string.ascii_lowercase, k=length))}"
        if alias.lower() in used:
            collisions += 1
            if collisions >= 50:
                length, collisions = length + 1, 0
            continue
        used.add(alias.lower())
        out.append(alias)
        collisions = 0
    return out


# ---- Création ----------------------------------------------------------------
def build_payload(kind: str, alias: str, domain: str, url: str | None, folder_id: str | None) -> dict:
    body = {"u": alias, "domain": domain}
    if kind == "landing":
        body["type"] = "l_p"
        if url:
            body["url"] = url
    else:
        body["type"] = "d_l"
        body["url"] = url
        if kind == "verification":
            body.update(VERIFICATION_FIELDS)
    if folder_id:
        body["folder_id"] = folder_id
    return body


CONFLICT_RE = re.compile(r"exist|taken|already|in use|used|duplicate|déjà|conflict|unique|reserved", re.I)


def is_conflict(e: LinkScaleError) -> bool:
    return e.status == 409 or (e.status == 400 and bool(CONFLICT_RE.search(e.message or "")))


async def create_links(client, *, aliases, base, kind, domain, url, folder_id, taken=(), progress=None):
    """Crée les liens un par un (l'API limite le débit). Si l'API signale un alias déjà pris
    (créé entre-temps par quelqu'un d'autre), un nouveau suffixe est tiré, 3 fois max.
    Retourne (réussis, échecs, interrompu?)."""
    used = {a.lower() for a in aliases} | {str(t).lower() for t in taken}
    ok, failed, aborted = [], [], False
    for i, alias in enumerate(aliases, 1):
        if aborted:
            failed.append({"alias": alias, "url": url or "", "error": "non tenté (accès refusé)"})
            continue
        for attempt in range(4):
            try:
                res = await client.create_link(build_payload(kind, alias, domain, url, folder_id))
                data = res.get("data", res) if isinstance(res, dict) else {}
                ok.append({
                    "alias": alias,
                    "short_url": data.get("short_url") or f"https://{domain}/{alias}",
                    "url": url or "",
                    "id": data.get("id", ""),
                })
                break
            except LinkScaleError as e:
                if is_conflict(e) and attempt < 3:
                    used.add(alias.lower())
                    alias = generate_aliases(base, 1, used)[0]
                    used.add(alias.lower())
                    continue
                failed.append({"alias": alias, "url": url or "", "error": e.message})
                aborted = e.status in (401, 403)  # clé invalide / permission manquante
                break
        if progress and (i % 10 == 0 or i == len(aliases)):
            await progress(i, len(aliases))
    return ok, failed, aborted


# ---- Stats -------------------------------------------------------------------
async def load_window(client, scope, scope_id, period, max_pages, now=None):
    delta, bucket, _ = st.PERIODS[period]
    until = now or st.utcnow()
    since = until - delta
    rows, truncated = await client.logs(scope, scope_id, since, until, max_pages)
    return st.compute(rows, since, until, bucket), truncated


# ---- Classements -------------------------------------------------------------
async def link_scores(client, links, period, metric, folder_id, max_pages):
    """([(lien, score)] trié, tronqué?). period == "total" : clics cumulés (champ `clicks`
    de la liste des liens) ; sinon calcul sur les logs de la période."""
    if folder_id:
        links = [l for l in links if folder_id in link_folder_ids(l)]
    if period == "total":
        scored = [(l, int(l.get("clicks") or 0)) for l in links]
        return sorted(scored, key=lambda x: x[1], reverse=True), False
    scope, sid = ("folder", folder_id) if folder_id else ("project", None)
    s, truncated = await load_window(client, scope, sid, period, max_pages)
    counter = s.link_clicks if metric == "clics" else s.link_humans
    by_id = {lid(l): l for l in links}
    scored = [(by_id.get(k, {"id": k}), v) for k, v in counter.items()]
    return sorted(scored, key=lambda x: x[1], reverse=True), truncated


async def folder_scores(client, links, folders, period, metric, max_pages):
    """([(dossier, score)] trié, tronqué?). Un lien dans plusieurs dossiers compte pour chacun."""
    truncated = False
    if period == "total":
        per_link = {lid(l): int(l.get("clicks") or 0) for l in links}
    else:
        s, truncated = await load_window(client, "project", None, period, max_pages)
        per_link = s.link_clicks if metric == "clics" else s.link_humans
    totals = Counter()
    for l in links:
        for f in link_folder_ids(l):
            totals[f] += per_link.get(lid(l), 0)
    scored = [(f, totals.get(str(f.get("_id") or f.get("id")), 0)) for f in folders]
    return sorted(scored, key=lambda x: x[1], reverse=True), truncated
