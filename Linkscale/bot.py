import asyncio
import csv
import io
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord
from discord import app_commands
from dotenv import load_dotenv

import charts
import core
import stats as st
from linkscale_client import LinkScaleClient, LinkScaleError, items
from store import COLOR_NAMES, DEFAULT_COLOR, Store, color_dot, parse_color

load_dotenv()
log = logging.getLogger("linkscale-bot")
logging.basicConfig(level=logging.INFO)

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
LINKSCALE_API_KEY = os.environ["LINKSCALE_API_KEY"]
ENV_DEFAULT_DOMAIN = os.getenv("LINKSCALE_DEFAULT_DOMAIN", "").strip()
ENV_DOMAINS = [d.strip() for d in os.getenv("LINKSCALE_DOMAINS", "").split(",") if d.strip()]
GUILD_ID = os.getenv("GUILD_ID")
ALLOWED_USER_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
MAX_BULK = int(os.getenv("MAX_BULK", "300"))
LOG_MAX_PAGES = int(os.getenv("LOG_MAX_PAGES", "30"))  # 30 pages x 100 = 3000 visites analysées max
LIVE_REFRESH = int(os.getenv("LIVE_REFRESH", "20"))  # secondes
LIVE_MAX_MINUTES = min(int(os.getenv("LIVE_MAX_MINUTES", "14")), 14)  # le jeton d'interaction expire à 15 min


class UserError(Exception):
    """Erreur à afficher telle quelle à l'utilisateur."""


def is_allowed(interaction: discord.Interaction) -> bool:
    if ALLOWED_USER_IDS:
        return interaction.user.id in ALLOWED_USER_IDS
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


class LSTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_allowed(interaction):
            return True
        await interaction.response.send_message("⛔ Tu n'as pas accès à ce bot.", ephemeral=True)
        return False

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        if isinstance(original, UserError):
            text = f"❌ {original}"
        elif isinstance(original, LinkScaleError):
            text = f"❌ Erreur LinkScale ({original.status}) : {original.message}"
        else:
            log.exception("Erreur inattendue", exc_info=error)
            text = "❌ Erreur inattendue, regarde les logs du bot."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


class LinkScaleBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.ls = LinkScaleClient(LINKSCALE_API_KEY)
        self.store = Store()
        self.tree = LSTree(self)
        self.cache = {"folders": [], "folders_ts": 0.0, "links": [], "links_ts": 0.0, "domains": []}

    async def setup_hook(self):
        await self.ls.start()
        self.tree.add_command(ls)
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        asyncio.create_task(self._warmup())

    async def _warmup(self):
        try:
            await get_folders(self)
            await get_links(self)
        except Exception as e:  # le bot reste utilisable sans préchauffage
            log.warning("Préchauffage impossible : %s", e)

    async def close(self):
        await self.ls.close()
        await super().close()


# ---- Caches / résolutions ----------------------------------------------------
def fid(folder: dict) -> str:
    return str(folder.get("_id") or folder.get("id") or "")


async def get_folders(bot, force=False):
    c = bot.cache
    if force or time.monotonic() - c["folders_ts"] > 60:
        c["folders"] = items(await bot.ls.list_folders(), "folders")
        c["folders_ts"] = time.monotonic()
    return c["folders"]


async def get_links(bot, force=False):
    c = bot.cache
    if force or time.monotonic() - c["links_ts"] > 120:
        c["links"] = await bot.ls.all_links()
        c["links_ts"] = time.monotonic()
        hosts = []
        for l in c["links"]:
            host = urlparse(l.get("short_url") or "").netloc
            if host and host not in hosts:
                hosts.append(host)
        c["domains"] = hosts
    return c["links"]


def default_domain(bot) -> str:
    return bot.store.data.get("default_domain") or ENV_DEFAULT_DOMAIN or ""


def known_domains(bot) -> list[str]:
    out = []
    for d in [default_domain(bot), *ENV_DOMAINS, *bot.store.data["domains"], *bot.cache["domains"]]:
        if d and d not in out:
            out.append(d)
    return out


def resolve_domain(bot, domain: str | None) -> str:
    d = (domain or "").strip() or default_domain(bot) or next(iter(known_domains(bot)), "")
    if not d:
        raise UserError("Aucun domaine : précise `domain`, ou définis-en un par défaut avec `/ls domain-default`.")
    return d


async def find_folder(bot, ref: str) -> dict | None:
    ref_l = ref.strip().lower()
    folders = await get_folders(bot)
    for f in folders:
        if fid(f) == ref.strip() or str(f.get("name", "")).lower() == ref_l:
            return f
    matches = [f for f in folders if ref_l in str(f.get("name", "")).lower()]
    return matches[0] if len(matches) == 1 else None


async def resolve_folder_id(bot, ref: str | None) -> str | None:
    if not ref:
        return None
    f = await find_folder(bot, ref)
    if not f:
        raise UserError(f"Dossier introuvable : « {ref} » (voir `/ls folders`).")
    return fid(f)


def folder_color(bot, folder_id) -> str:
    return bot.store.folder_color(folder_id) or DEFAULT_COLOR


def short(text, n=1000):
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def hex_int(color: str) -> int:
    return int(color.lstrip("#"), 16)


# ---- Autocomplétions ---------------------------------------------------------
async def ac_domain(interaction: discord.Interaction, current: str):
    doms = known_domains(interaction.client)
    return [app_commands.Choice(name=d[:100], value=d[:100]) for d in doms if current.lower() in d.lower()][:25]


async def ac_folder(interaction: discord.Interaction, current: str):
    try:
        folders = await asyncio.wait_for(get_folders(interaction.client), 2.5)
    except Exception:
        return []
    out = [
        app_commands.Choice(name=str(f.get("name"))[:100], value=fid(f))
        for f in folders
        if current.lower() in str(f.get("name", "")).lower()
    ]
    return out[:25]


async def ac_target(interaction: discord.Interaction, current: str):
    kind = getattr(interaction.namespace, "type", None)
    if kind == "dossier":
        return await ac_folder(interaction, current)
    if kind == "lien":
        try:
            res = await asyncio.wait_for(
                interaction.client.ls.list_links(page=1, limit=25, search=current or None), 2.5
            )
        except Exception:
            return []
        return [
            app_commands.Choice(name=core.link_label(l)[:100], value=core.lid(l)[:100])
            for l in items(res, "links")
            if core.lid(l)
        ][:25]
    return []


async def ac_color(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n) for n in COLOR_NAMES if current.lower() in n][:25]


KIND_CHOICES = [
    app_commands.Choice(name="Un lien", value="lien"),
    app_commands.Choice(name="Un dossier", value="dossier"),
    app_commands.Choice(name="Tout le projet", value="projet"),
]
PERIOD_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in st.PERIOD_LABELS.items()]
RANK_PERIOD_CHOICES = [app_commands.Choice(name="Total (depuis toujours)", value="total"), *PERIOD_CHOICES]
METRIC_CHOICES = [
    app_commands.Choice(name="Visites humaines", value="visites"),
    app_commands.Choice(name="Clics", value="clics"),
]

ls = app_commands.Group(name="ls", description="Pilote ton compte LinkScale")


# ============================================================================
# LIENS
# ============================================================================
@ls.command(name="link-create", description="Crée un lien direct")
@app_commands.describe(
    url="Destination du lien",
    alias="Alias court (aléatoire si vide)",
    domain="Domaine (défaut : celui défini avec /ls domain-default)",
    dossier="Dossier où ranger le lien",
)
async def link_create(
    interaction: discord.Interaction,
    url: str,
    alias: str | None = None,
    domain: str | None = None,
    dossier: str | None = None,
):
    await interaction.response.defer(ephemeral=True)
    bot = interaction.client
    domain = resolve_domain(bot, domain)
    folder_id = await resolve_folder_id(bot, dossier)
    alias = alias or core.random_alias()
    if not core.ALIAS_RE.match(alias):
        raise UserError("Alias invalide (lettres, chiffres, - et _ uniquement).")
    res = await bot.ls.create_direct_link(alias, domain, url, folder_id)
    data = res.get("data", res) if isinstance(res, dict) else {}
    embed = discord.Embed(title="✅ Lien créé", color=discord.Color.green())
    embed.add_field(name="URL courte", value=data.get("short_url") or f"{domain}/{alias}", inline=False)
    embed.add_field(name="Destination", value=short(url, 500), inline=False)
    if data.get("id"):
        embed.add_field(name="ID", value=f"`{data['id']}`")
    await interaction.followup.send(embed=embed)


link_create.autocomplete("domain")(ac_domain)
link_create.autocomplete("dossier")(ac_folder)


# ---- Création en masse -------------------------------------------------------
async def run_bulk(interaction: discord.Interaction, text: str, domain: str, folder_id: str | None, copies: int):
    """Suppose que l'interaction a déjà été defer(ephemeral=True)."""
    entries, errors = core.parse_bulk(text, copies, MAX_BULK)
    if not entries:
        detail = "\n".join(f"ligne {n} : {m}" for n, m in errors[:10]) or "aucune ligne exploitable."
        await interaction.edit_original_response(content=f"❌ Rien à créer.\n{detail}")
        return

    eta = int(len(entries) * float(os.getenv("LINKSCALE_MIN_INTERVAL", "0.55")))
    await interaction.edit_original_response(content=f"⏳ Création de **{len(entries)}** liens sur `{domain}`… (~{eta}s)")

    async def progress(done, total):
        try:
            await interaction.edit_original_response(content=f"⏳ {done}/{total} liens traités…")
        except discord.HTTPException:
            pass

    ok, failed, aborted = await core.bulk_create(interaction.client.ls, entries, domain, folder_id, progress)
    interaction.client.cache["links_ts"] = 0  # force le rafraîchissement des classements

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["statut", "alias", "short_url", "destination", "id", "erreur"])
    for r in ok:
        w.writerow(["ok", r["alias"], r["short_url"], r["url"], r["id"], ""])
    for r in failed:
        w.writerow(["erreur", r["alias"], "", r["url"], "", r["error"]])
    for n, m in errors:
        w.writerow(["ignoré", "", "", "", "", f"ligne {n} : {m}"])

    color = discord.Color.green() if not failed else (discord.Color.red() if aborted else discord.Color.orange())
    embed = discord.Embed(title="📦 Création en masse terminée", color=color)
    embed.add_field(name="Créés", value=f"**{len(ok)}**")
    embed.add_field(name="Échecs", value=f"**{len(failed)}**")
    embed.add_field(name="Lignes ignorées", value=f"**{len(errors)}**")
    if ok:
        embed.add_field(name="Aperçu", value=short("\n".join(r["short_url"] for r in ok[:5]), 1000), inline=False)
    if failed:
        embed.add_field(
            name="Premières erreurs",
            value=short("\n".join(f"`{r['alias']}` : {r['error']}" for r in failed[:5]), 1000),
            inline=False,
        )
    file = discord.File(io.BytesIO(buf.getvalue().encode("utf-8")), filename="liens.csv")
    await interaction.edit_original_response(content=None, embed=embed, attachments=[file])


class BulkModal(discord.ui.Modal, title="Création de liens en masse"):
    contenu = discord.ui.TextInput(
        label="Un lien par ligne : url  ou  alias;url",
        style=discord.TextStyle.paragraph,
        placeholder="https://exemple.com/a\npromo;https://exemple.com/b",
        max_length=4000,
    )

    def __init__(self, domain: str, folder_id: str | None, copies: int):
        super().__init__()
        self.domain, self.folder_id, self.copies = domain, folder_id, copies

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await run_bulk(interaction, str(self.contenu), self.domain, self.folder_id, self.copies)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("Erreur bulk", exc_info=error)
        text = f"❌ {error}" if isinstance(error, (UserError, LinkScaleError)) else "❌ Erreur inattendue."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


@ls.command(name="bulk-create", description="Crée plusieurs liens d'un coup (formulaire ou fichier .txt/.csv)")
@app_commands.describe(
    fichier="Fichier texte : un lien par ligne (url ou alias;url). Sans fichier, un formulaire s'ouvre.",
    domain="Domaine (défaut : celui défini avec /ls domain-default)",
    dossier="Dossier où ranger tous les liens",
    copies="Nombre de liens créés par ligne (alias aléatoires)",
)
async def bulk_create(
    interaction: discord.Interaction,
    fichier: discord.Attachment | None = None,
    domain: str | None = None,
    dossier: str | None = None,
    copies: app_commands.Range[int, 1, 100] = 1,
):
    bot = interaction.client
    if fichier is None:
        domain_ = resolve_domain(bot, domain)
        folder_id = await resolve_folder_id(bot, dossier)
        await interaction.response.send_modal(BulkModal(domain_, folder_id, copies))
        return
    await interaction.response.defer(ephemeral=True)
    if fichier.size > 300_000:
        raise UserError("Fichier trop gros (300 Ko max).")
    domain_ = resolve_domain(bot, domain)
    folder_id = await resolve_folder_id(bot, dossier)
    text = (await fichier.read()).decode("utf-8-sig", errors="replace")
    await run_bulk(interaction, text, domain_, folder_id, copies)


bulk_create.autocomplete("domain")(ac_domain)
bulk_create.autocomplete("dossier")(ac_folder)


# ---- Consultation / édition --------------------------------------------------
@ls.command(name="links", description="Liste tes liens")
@app_commands.describe(search="Filtre texte", page="Page (10 liens par page)", dossier="Filtrer par dossier")
async def links_cmd(
    interaction: discord.Interaction,
    search: str | None = None,
    page: app_commands.Range[int, 1, 1000] = 1,
    dossier: str | None = None,
):
    await interaction.response.defer(ephemeral=True)
    folder_id = await resolve_folder_id(interaction.client, dossier)
    res = await interaction.client.ls.list_links(page=page, limit=10, search=search, folder_id=folder_id)
    rows = items(res, "links")
    if not rows:
        await interaction.followup.send("Aucun lien trouvé.")
        return
    lines = [f"• **{core.link_label(l)}** — {fmt(l.get('clicks') or 0)} clics — `{core.lid(l)}`" for l in rows]
    pagination = res.get("pagination", {}) if isinstance(res, dict) else {}
    embed = discord.Embed(title="🔗 Tes liens", description=short("\n".join(lines), 4000))
    embed.set_footer(text=f"Page {pagination.get('page', page)}/{pagination.get('pages', '?')}")
    await interaction.followup.send(embed=embed)


links_cmd.autocomplete("dossier")(ac_folder)


@ls.command(name="link-info", description="Détails d'un lien")
@app_commands.describe(lien="Lien (tape pour chercher)")
async def link_info(interaction: discord.Interaction, lien: str):
    await interaction.response.defer(ephemeral=True)
    res = await interaction.client.ls.get_link(lien)
    link = res.get("link", res.get("data", res)) if isinstance(res, dict) else {}
    embed = discord.Embed(title=short(link.get("title") or core.link_label(link) or lien, 200))
    for name, key in [
        ("URL courte", "short_url"),
        ("Destination", "original_url"),
        ("Clics", "clicks"),
        ("Actif", "is_active"),
        ("Shield", "shield"),
        ("Créé le", "created_at"),
    ]:
        if link.get(key) is not None:
            embed.add_field(name=name, value=short(link[key], 500), inline=False)
    names = [f.get("name") for f in res.get("folders", []) if isinstance(f, dict)] if isinstance(res, dict) else []
    if names:
        embed.add_field(name="Dossiers", value=", ".join(map(str, names)), inline=False)
    await interaction.followup.send(embed=embed)


async def ac_link(interaction: discord.Interaction, current: str):
    try:
        res = await asyncio.wait_for(interaction.client.ls.list_links(page=1, limit=25, search=current or None), 2.5)
    except Exception:
        return []
    return [
        app_commands.Choice(name=core.link_label(l)[:100], value=core.lid(l)[:100])
        for l in items(res, "links")
        if core.lid(l)
    ][:25]


link_info.autocomplete("lien")(ac_link)


@ls.command(name="link-update", description="Change la destination d'un lien")
async def link_update(interaction: discord.Interaction, lien: str, url: str):
    await interaction.response.defer(ephemeral=True)
    await interaction.client.ls.update_link(lien, url=url)
    await interaction.followup.send(f"✅ Lien `{lien}` mis à jour → {url}")


link_update.autocomplete("lien")(ac_link)


class ConfirmDelete(discord.ui.View):
    def __init__(self, link_id: str, owner_id: int):
        super().__init__(timeout=30)
        self.link_id, self.owner_id = link_id, owner_id

    @discord.ui.button(label="Supprimer", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Ce bouton n'est pas pour toi.", ephemeral=True)
            return
        try:
            await interaction.client.ls.delete_link(self.link_id)
            interaction.client.cache["links_ts"] = 0
            await interaction.response.edit_message(content=f"🗑️ Lien `{self.link_id}` supprimé.", view=None)
        except LinkScaleError as e:
            await interaction.response.edit_message(content=f"❌ {e.message}", view=None)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Annulé.", view=None)


@ls.command(name="link-delete", description="Supprime définitivement un lien")
async def link_delete(interaction: discord.Interaction, lien: str):
    await interaction.response.send_message(
        f"⚠️ Supprimer définitivement `{lien}` ? Action irréversible.",
        view=ConfirmDelete(lien, interaction.user.id),
        ephemeral=True,
    )


link_delete.autocomplete("lien")(ac_link)


# ============================================================================
# DOSSIERS (couleurs)
# ============================================================================
def parse_color_or_fail(value: str | None) -> str | None:
    if value is None:
        return None
    c = parse_color(value)
    if not c:
        raise UserError(f"Couleur invalide « {value} » : un nom ({', '.join(COLOR_NAMES)}) ou un hexadécimal (#ff8800).")
    return c


@ls.command(name="folders", description="Liste les dossiers (avec leur couleur)")
async def folders_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    bot = interaction.client
    rows = await get_folders(bot, force=True)
    if not rows:
        await interaction.followup.send("Aucun dossier.")
        return
    rows = sorted(rows, key=lambda f: int(f.get("links_count") or 0), reverse=True)
    lines = [
        f"{color_dot(bot.store.folder_color(fid(f)))} **{f.get('name')}** — {fmt(f.get('links_count') or 0)} liens — `{fid(f)}`"
        for f in rows
    ]
    await interaction.followup.send(embed=discord.Embed(title="📁 Dossiers", description=short("\n".join(lines), 4000)))


@ls.command(name="folder-create", description="Crée un dossier (avec une couleur)")
@app_commands.describe(name="Nom du dossier", couleur="Nom de couleur ou hexadécimal (#ff8800)")
async def folder_create(interaction: discord.Interaction, name: str, couleur: str | None = None):
    await interaction.response.defer(ephemeral=True)
    bot = interaction.client
    color = parse_color_or_fail(couleur)
    res = await bot.ls.create_folder(name)
    folder = res.get("folder", res.get("data", {})) if isinstance(res, dict) else {}
    new_id = fid(folder)
    if color and new_id:
        bot.store.set_folder_color(new_id, color)
    bot.cache["folders_ts"] = 0
    await interaction.followup.send(f"✅ Dossier {color_dot(color)} **{name}** créé — `{new_id or '?'}`")


folder_create.autocomplete("couleur")(ac_color)


@ls.command(name="folder-color", description="Change la couleur d'un dossier")
@app_commands.describe(dossier="Dossier", couleur="Nom de couleur ou hexadécimal (#ff8800)")
async def folder_color_cmd(interaction: discord.Interaction, dossier: str, couleur: str):
    await interaction.response.defer(ephemeral=True)
    bot = interaction.client
    color = parse_color_or_fail(couleur)
    f = await find_folder(bot, dossier)
    if not f:
        raise UserError(f"Dossier introuvable : « {dossier} ».")
    bot.store.set_folder_color(fid(f), color)
    await interaction.followup.send(f"{color_dot(color)} Couleur de **{f.get('name')}** → `{color}`")


folder_color_cmd.autocomplete("dossier")(ac_folder)
folder_color_cmd.autocomplete("couleur")(ac_color)


# ============================================================================
# DOMAINES
# ============================================================================
@ls.command(name="domains", description="Domaines connus (le défaut est marqué ⭐)")
async def domains_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    bot = interaction.client
    await get_links(bot, force=True)  # découvre les domaines déjà utilisés par tes liens
    default = default_domain(bot)
    doms = known_domains(bot)
    if not doms:
        await interaction.followup.send("Aucun domaine connu. Ajoute-en un avec `/ls domain-add`.")
        return
    lines = [f"{'⭐' if d == default else '•'} `{d}`" for d in doms]
    await interaction.followup.send(embed=discord.Embed(title="🌐 Domaines", description="\n".join(lines)))


@ls.command(name="domain-add", description="Ajoute un domaine à la liste sélectionnable")
async def domain_add(interaction: discord.Interaction, domain: str):
    bot = interaction.client
    domain = domain.strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
    if "." not in domain:
        raise UserError("Domaine invalide.")
    bot.store.add_domain(domain)
    await interaction.response.send_message(f"✅ `{domain}` ajouté.", ephemeral=True)


@ls.command(name="domain-default", description="Définit le domaine par défaut pour les nouveaux liens")
async def domain_default(interaction: discord.Interaction, domain: str):
    bot = interaction.client
    domain = domain.strip().lower()
    bot.store.set_default_domain(domain)
    bot.store.add_domain(domain)
    await interaction.response.send_message(f"⭐ Domaine par défaut : `{domain}`", ephemeral=True)


domain_default.autocomplete("domain")(ac_domain)


# ============================================================================
# STATS (chiffres + graphiques)
# ============================================================================
async def resolve_target(bot, kind: str, ref: str | None):
    """-> (scope, scope_id, titre, couleur hex)."""
    if kind == "projet":
        return "project", None, "Projet", DEFAULT_COLOR
    if not ref:
        raise UserError("Précise la cible (`cible`) pour un lien ou un dossier.")
    if kind == "dossier":
        f = await find_folder(bot, ref)
        if not f:
            raise UserError(f"Dossier introuvable : « {ref} ».")
        return "folder", fid(f), f"Dossier {f.get('name')}", folder_color(bot, fid(f))
    try:
        res = await bot.ls.get_link(ref)
        link = res.get("link", res.get("data", res)) if isinstance(res, dict) else {}
        link_id = core.lid(link) or ref
    except LinkScaleError as e:
        if e.status != 404:
            raise
        found = items(await bot.ls.list_links(page=1, limit=5, search=ref), "links")
        if not found:
            raise UserError(f"Lien introuvable : « {ref} ».")
        link = found[0]
        link_id = core.lid(link)
    colors = [bot.store.folder_color(f) for f in core.link_folder_ids(link) if bot.store.folder_color(f)]
    return "link", link_id, core.link_label(link) or ref, (colors[0] if colors else DEFAULT_COLOR)


def stats_embed(title, color, s, period, truncated, link_labels):
    e = discord.Embed(title=f"📊 {title}", color=hex_int(color), timestamp=datetime.now(timezone.utc))
    e.add_field(name="Visites humaines", value=f"**{fmt(s.humans)}**")
    e.add_field(name="Clics", value=f"**{fmt(s.clicks)}**")
    e.add_field(name="CTR", value=f"**{s.ctr * 100:.1f} %**")
    e.add_field(name="Bots détectés", value=f"{fmt(s.bots)} ({s.bot_rate * 100:.0f} %)")
    e.add_field(name="Total visites", value=fmt(s.visits))
    if s.last_visit:
        e.add_field(name="Dernière visite", value=f"<t:{int(s.last_visit.timestamp())}:R>")
    if s.link_humans and link_labels is not None:
        top = s.link_humans.most_common(5)
        lines = [
            f"{i}. {link_labels.get(k, k)} — {fmt(v)} visites · {fmt(s.link_clicks[k])} clics"
            for i, (k, v) in enumerate(top, 1)
        ]
        e.add_field(name="Top liens", value=short("\n".join(lines), 1000), inline=False)
    note = f"Période : {st.PERIOD_LABELS[period]}"
    if truncated:
        note += f" · ⚠️ limité aux {LOG_MAX_PAGES * 100} visites les plus récentes"
    e.set_footer(text=note)
    e.set_image(url="attachment://stats.png")
    return e


async def render_stats(bot, scope, title, color, s, period, truncated):
    labels = None
    if scope != "link":
        try:
            labels = {core.lid(l): core.link_label(l) for l in await get_links(bot)}
        except LinkScaleError:
            labels = {}
    _, _, bucket_label = st.PERIODS[period]
    png = await asyncio.to_thread(charts.dashboard_png, s, title, color, bucket_label, st.PERIOD_LABELS[period])
    return stats_embed(title, color, s, period, truncated, labels), discord.File(io.BytesIO(png), "stats.png")


@ls.command(name="stats", description="Statistiques chiffrées + graphiques d'un lien, d'un dossier ou du projet")
@app_commands.describe(type="Ce que tu veux analyser", cible="Le lien ou le dossier", periode="Période (défaut : 24 h)")
@app_commands.choices(type=KIND_CHOICES, periode=PERIOD_CHOICES)
async def stats_cmd(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    cible: str | None = None,
    periode: app_commands.Choice[str] | None = None,
):
    await interaction.response.defer(ephemeral=True)
    bot = interaction.client
    period = periode.value if periode else "24h"
    scope, sid, title, color = await resolve_target(bot, type.value, cible)
    s, truncated = await core.load_window(bot.ls, scope, sid, period, LOG_MAX_PAGES)
    embed, file = await render_stats(bot, scope, title, color, s, period, truncated)
    await interaction.followup.send(embed=embed, file=file)


stats_cmd.autocomplete("cible")(ac_target)


# ---- Temps réel --------------------------------------------------------------
class LiveView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.stop_event = asyncio.Event()

    @discord.ui.button(label="Arrêter", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Seul celui qui a lancé le live peut l'arrêter.", ephemeral=True)
            return
        self.stop_event.set()
        await interaction.response.defer()


class LiveSession:
    """Garde les visites en mémoire et ne redemande que les nouvelles à chaque tick."""

    def __init__(self, client, scope, sid, period):
        self.client, self.scope, self.sid, self.period = client, scope, sid, period
        self.rows: dict = {}
        self.last_ts = None

    async def refresh(self):
        delta, bucket, _ = st.PERIODS[self.period]
        now = st.utcnow()
        since = now - delta
        fetch_since = since if self.last_ts is None else max(since, self.last_ts - st.timedelta(seconds=60))
        rows, truncated = await self.client.logs(self.scope, self.sid, fetch_since, now, LOG_MAX_PAGES)
        for r in rows:
            key = r.get("_id") or (r.get("timestamp"), r.get("ip"), r.get("userAgent"))
            self.rows[key] = r
        self.rows = {
            k: r for k, r in self.rows.items() if (st.parse_ts(r.get("timestamp")) or since) >= since
        }
        stamps = [t for t in (st.parse_ts(r.get("timestamp")) for r in self.rows.values()) if t]
        self.last_ts = max(stamps) if stamps else None
        return st.compute(list(self.rows.values()), since, now, bucket), truncated


@ls.command(name="live", description="Stats + graphique rafraîchis automatiquement (temps réel)")
@app_commands.describe(type="Ce que tu veux suivre", cible="Le lien ou le dossier", periode="Fenêtre glissante (défaut : 1 h)")
@app_commands.choices(type=KIND_CHOICES, periode=PERIOD_CHOICES)
async def live_cmd(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    cible: str | None = None,
    periode: app_commands.Choice[str] | None = None,
):
    await interaction.response.defer()
    bot = interaction.client
    period = periode.value if periode else "1h"
    scope, sid, title, color = await resolve_target(bot, type.value, cible)
    session = LiveSession(bot.ls, scope, sid, period)
    view = LiveView(interaction.user.id)
    deadline = time.monotonic() + LIVE_MAX_MINUTES * 60
    errors = 0
    while True:
        try:
            s, truncated = await session.refresh()
            embed, file = await render_stats(bot, scope, f"🔴 LIVE · {title}", color, s, period, truncated)
            embed.set_footer(text=f"{embed.footer.text} · MAJ toutes les {LIVE_REFRESH}s · fin auto dans {LIVE_MAX_MINUTES} min max")
            await interaction.edit_original_response(embed=embed, attachments=[file], view=view)
            errors = 0
        except (LinkScaleError, discord.HTTPException) as e:
            errors += 1
            log.warning("Live : erreur %s", e)
            if errors >= 3:
                await interaction.edit_original_response(content=f"❌ Live interrompu : {e}", view=None)
                return
        if time.monotonic() >= deadline:
            break
        try:
            await asyncio.wait_for(view.stop_event.wait(), timeout=LIVE_REFRESH)
            break
        except asyncio.TimeoutError:
            pass
    await interaction.edit_original_response(content="⏹️ Live terminé (relance `/ls live` pour continuer).", view=None)


live_cmd.autocomplete("cible")(ac_target)


# ============================================================================
# CLASSEMENTS
# ============================================================================
def medal(i: int) -> str:
    return ["🥇", "🥈", "🥉"][i] if i < 3 else f"`{i + 1}.`"


@ls.command(name="top-links", description="Classement des liens")
@app_commands.describe(periode="Période (Total = clics cumulés)", metrique="Critère (ignoré pour Total)", dossier="Limiter à un dossier", limite="Nombre de lignes")
@app_commands.choices(periode=RANK_PERIOD_CHOICES, metrique=METRIC_CHOICES)
async def top_links(
    interaction: discord.Interaction,
    periode: app_commands.Choice[str] | None = None,
    metrique: app_commands.Choice[str] | None = None,
    dossier: str | None = None,
    limite: app_commands.Range[int, 3, 20] = 10,
):
    await interaction.response.defer(ephemeral=True)
    bot = interaction.client
    period = periode.value if periode else "total"
    metric = metrique.value if metrique else "visites"
    folder_id = await resolve_folder_id(bot, dossier)
    links = await get_links(bot, force=True)
    scored, truncated = await core.link_scores(bot.ls, links, period, metric, folder_id, LOG_MAX_PAGES)
    scored = scored[:limite]
    if not scored:
        await interaction.followup.send("Aucune donnée pour ce classement.")
        return
    unit_name = "clics" if period == "total" or metric == "clics" else "visites"
    entries, lines = [], []
    for i, (l, v) in enumerate(scored):
        first = next((bot.store.folder_color(f) for f in core.link_folder_ids(l) if bot.store.folder_color(f)), DEFAULT_COLOR)
        entries.append((core.link_label(l), v, first))
        lines.append(f"{medal(i)} **{core.link_label(l)}** — {fmt(v)} {unit_name}")
    label = "total" if period == "total" else st.PERIOD_LABELS[period]
    title = f"Top liens — {label}" + (f" · {unit_name}" if period != "total" else " · clics")
    png = await asyncio.to_thread(charts.ranking_png, title, entries)
    embed = discord.Embed(title=f"🏆 {title}", description="\n".join(lines), color=hex_int(DEFAULT_COLOR))
    embed.set_image(url="attachment://top.png")
    if truncated:
        embed.set_footer(text=f"⚠️ limité aux {LOG_MAX_PAGES * 100} visites les plus récentes")
    await interaction.followup.send(embed=embed, file=discord.File(io.BytesIO(png), "top.png"))


top_links.autocomplete("dossier")(ac_folder)


@ls.command(name="top-folders", description="Classement des dossiers")
@app_commands.describe(periode="Période (Total = clics cumulés)", metrique="Critère (ignoré pour Total)", limite="Nombre de lignes")
@app_commands.choices(periode=RANK_PERIOD_CHOICES, metrique=METRIC_CHOICES)
async def top_folders(
    interaction: discord.Interaction,
    periode: app_commands.Choice[str] | None = None,
    metrique: app_commands.Choice[str] | None = None,
    limite: app_commands.Range[int, 3, 20] = 10,
):
    await interaction.response.defer(ephemeral=True)
    bot = interaction.client
    period = periode.value if periode else "total"
    metric = metrique.value if metrique else "visites"
    folders = await get_folders(bot, force=True)
    links = await get_links(bot, force=True)
    scored, truncated = await core.folder_scores(bot.ls, links, folders, period, metric, LOG_MAX_PAGES)
    scored = scored[:limite]
    if not scored:
        await interaction.followup.send("Aucun dossier.")
        return
    unit_name = "clics" if period == "total" or metric == "clics" else "visites"
    entries, lines = [], []
    for i, (f, v) in enumerate(scored):
        c = folder_color(bot, fid(f))
        entries.append((str(f.get("name")), v, c))
        lines.append(f"{medal(i)} {color_dot(bot.store.folder_color(fid(f)))} **{f.get('name')}** — {fmt(v)} {unit_name}")
    label = "total" if period == "total" else st.PERIOD_LABELS[period]
    title = f"Top dossiers — {label} · {unit_name}"
    png = await asyncio.to_thread(charts.ranking_png, title, entries)
    embed = discord.Embed(title=f"🏆 {title}", description="\n".join(lines), color=hex_int(DEFAULT_COLOR))
    embed.set_image(url="attachment://top.png")
    if truncated:
        embed.set_footer(text=f"⚠️ limité aux {LOG_MAX_PAGES * 100} visites les plus récentes")
    await interaction.followup.send(embed=embed, file=discord.File(io.BytesIO(png), "top.png"))


# ============================================================================
# DIVERS
# ============================================================================
@ls.command(name="logs", description="Dernières visites d'un lien (IP masquées par l'API)")
async def logs_cmd(interaction: discord.Interaction, lien: str, limit: app_commands.Range[int, 1, 20] = 10):
    await interaction.response.defer(ephemeral=True)
    rows = items(await interaction.client.ls.link_logs(lien, limit))
    if not rows:
        await interaction.followup.send("Aucune visite sur les 30 derniers jours.")
        return
    lines = []
    for v in rows:
        ts = str(v.get("timestamp", ""))[:16].replace("T", " ")
        flag = "🤖" if v.get("bot") else "👤"
        lines.append(f"{flag} `{ts}` {v.get('country', '??')} · {v.get('device_type', '?')} · {len(v.get('clicks', []))} clic(s)")
    await interaction.followup.send(embed=discord.Embed(title="🧾 Dernières visites", description=short("\n".join(lines), 4000)))


logs_cmd.autocomplete("lien")(ac_link)


@ls.command(name="trending", description="Liens en tendance (selon LinkScale)")
async def trending(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    res = await interaction.client.ls.trending_links()
    rows = items(res, "links", "trending_links")
    if not rows:
        await interaction.followup.send(f"```json\n{short(res, 1800)}\n```")
        return
    lines = [f"{i}. **{core.link_label(r)}** — {fmt(r.get('clicks') or 0)} clics" for i, r in enumerate(rows[:10], 1)]
    await interaction.followup.send(embed=discord.Embed(title="🔥 Tendances", description="\n".join(lines)))


if __name__ == "__main__":
    LinkScaleBot().run(DISCORD_TOKEN)
