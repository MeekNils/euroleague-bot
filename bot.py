#!/usr/bin/env python3
"""EuroLeague-Bot fuer Telegram.

Postet automatisch in einen Telegram-Kanal:
  1. Etwa 60 Minuten vor jedem Spiel beide Kader mit Trikotnummern,
     dazu wer laut offiziellem Injury Report OUT oder fraglich (GTD) ist
  2. Die finalen 12 Spieler vom Spielbogen plus Starting Five,
     sobald die Liga sie veroeffentlicht
  3. Den offiziellen Injury Report, sobald er fuer eine Runde erscheint,
     und danach jede Aenderung sofort
  4. Wichtige News (Verletzungen, Transfers) von Sportando und Eurohoops

Das Skript braucht keine Zusatzpakete. Es merkt sich in der Datei state.json,
was schon gepostet wurde, damit nichts doppelt im Kanal landet.
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Einstellungen (diese Werte darfst du aendern)
# ---------------------------------------------------------------------------
KADER_MINUTEN_VORHER = 65         # Kader-Post so viele Minuten vor Spielbeginn
ZEITZONE = "Europe/Berlin"        # Uhrzeiten in den Posts
NEWS_AN = True                    # False = keine News von Sportando/Eurohoops
NEWS_QUELLEN = [
    # (Name im Post, Adresse des Feeds, Pflicht-Kategorie oder None)
    ("Sportando", "https://sportando.basketball/en/feed/", "euroleague"),
    ("Eurohoops", "https://www.eurohoops.net/en/euroleague/feed/", None),
]
# Nur News, deren Ueberschrift eines dieser Muster enthaelt, werden gepostet
NEWS_MUSTER = (
    r"injur|ruled out|sidelined|surgery|\bto miss\b|\bwill miss\b|\bmiss(es|ed)?\b"
    r"|\bout (for|with|until|indefinitely)\b|doubtful|questionable|game-time"
    r"|\breturns?\b|\bback in action\b|suspend|\bsign(s|ed|ing)?\b|\bjoins?\b"
    r"|parts ways|\bleaves?\b|\bloan|buyout|\bwaive|\breleases?\b|extension"
    r"|\bextends?\b|\bagree|\btransfer|\breplac|fracture|sprain|\btear\b|\btorn\b"
    r"|strain|\bacl\b|achilles|hamstring"
)
# ---------------------------------------------------------------------------

API = "https://api-live.euroleague.net/v2"
LIVE = "https://live.euroleague.net/api"
CMS = "https://article-cms-api.incrowdsports.com/v2"
NEWS_URL = "https://www.euroleaguebasketball.net/en/euroleague/news/{slug}/"
STATE_FILE = "state.json"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 euroleague-telegram-bot"
)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
PENDING_TEXT = "has not yet sent an injury report"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL_RAW = os.environ.get("TELEGRAM_CHANNEL", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
SEND_TEST = os.environ.get("SEND_TEST") == "1"


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def log(*parts):
    print(*parts, flush=True)


def esc(text):
    return html.escape(str(text or ""), quote=False)


def squash(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def strip_accents(text):
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def norm(text):
    """Kleinbuchstaben ohne Akzente und Sonderzeichen, zum Vergleichen."""
    return squash(re.sub(r"[^a-z0-9]+", " ", strip_accents(text).lower()))


def hashtag(name):
    return "#" + re.sub(r"[^A-Za-z0-9]", "", strip_accents(name))


def fingerprint(text):
    return hashlib.sha1(squash(text).encode("utf-8")).hexdigest()[:10]


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_time(moment):
    local = moment.astimezone(ZoneInfo(ZEITZONE))
    return (
        f"{WEEKDAYS[local.weekday()]}, {MONTHS[local.month - 1]} {local.day}"
        f" · {local:%H:%M} {local.tzname()}"
    )


def fetch(url, soft=False):
    """Laedt eine Adresse als Text. soft=True: bei Problemen einfach None."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    last_error = None
    for attempt in range(1 if soft else 3):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code in (400, 404):
                return None
            last_error = error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
        if not soft:
            time.sleep(3 * (attempt + 1))
    if soft:
        return None
    raise RuntimeError(f"Datenquelle nicht erreichbar: {url} ({last_error})")


def get_json(url, soft=False):
    """Laedt eine JSON-Adresse. Gibt None zurueck, wenn es sie nicht gibt."""
    text = fetch(url, soft=soft)
    if text is None or not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        if soft:
            return None
        raise RuntimeError(f"Unlesbare Antwort von: {url}")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def channel_id():
    """Macht aus t.me/name, @name oder name immer @name."""
    value = CHANNEL_RAW
    value = re.sub(r"^https?://", "", value, flags=re.I)
    value = re.sub(r"^(www\.)?(t\.me|telegram\.me)/", "", value, flags=re.I)
    value = value.strip().strip("/")
    if re.fullmatch(r"-?\d+", value):
        return value
    return "@" + value.lstrip("@")


def split_message(text, limit=3900):
    """Teilt lange Posts an Leerzeilen, Telegram erlaubt 4096 Zeichen."""
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = ""
        for line in block.split("\n"):
            line = line[:limit]
            candidate = line if not current else current + "\n" + line
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = line
    if current:
        chunks.append(current)
    return chunks


def send(text):
    for chunk in split_message(text):
        if DRY_RUN:
            log("\n===== TELEGRAM-POST =====\n" + chunk + "\n=========================")
            continue
        payload = urllib.parse.urlencode(
            {
                "chat_id": channel_id(),
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        for attempt in range(4):
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, data=payload), timeout=40
                ) as response:
                    json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as error:
                try:
                    body = json.loads(error.read().decode("utf-8"))
                except ValueError:
                    body = {}
                if error.code == 429 and attempt < 3:
                    wait = (body.get("parameters") or {}).get("retry_after", 5)
                    time.sleep(min(int(wait) + 1, 60))
                    continue
                reason = body.get("description") or f"HTTP {error.code}"
                raise SystemExit(
                    "FEHLER von Telegram: " + reason + "\n"
                    "Pruefe: Stimmt der Token? Stimmt der Kanalname? "
                    "Ist der Bot Admin im Kanal mit 'Post Messages'?"
                )
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt == 3:
                    raise RuntimeError(f"Telegram nicht erreichbar ({error})")
                time.sleep(5)
        time.sleep(1.2)


# ---------------------------------------------------------------------------
# Gedaechtnis (state.json)
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            state = json.load(handle)
            return state if isinstance(state, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")


# ---------------------------------------------------------------------------
# Spielplan und Kader
# ---------------------------------------------------------------------------
_round_cache = {}
_squad_cache = {}


def game_time(game):
    return parse_time(game.get("utcDate"))


def game_id(game, season):
    return str(game.get("identifier") or f"{season}_{game.get('gameCode')}")


def club_of(game, side):
    return (game.get(side) or {}).get("club") or {}


def round_games(season, round_number):
    key = (season, round_number)
    if key not in _round_cache:
        data = get_json(
            f"{API}/competitions/E/seasons/{season}/games?roundNumber={round_number}"
        )
        _round_cache[key] = (data or {}).get("data") or []
    return _round_cache[key]


def active_rounds(state, season, now):
    """Runden mit Spielen in den naechsten Tagen. Wird alle 6 Stunden erneuert."""
    info = state.get("schedule") or {}
    checked = parse_time(info.get("checked"))
    if (
        info.get("season") == season
        and checked
        and now - checked < timedelta(hours=6)
    ):
        return info.get("rounds") or []
    data = get_json(f"{API}/competitions/E/seasons/{season}/games")
    games = (data or {}).get("data") or []
    if not games:
        return info.get("rounds") or []
    rounds = set()
    for game in games:
        tip = game_time(game)
        if tip and now - timedelta(hours=6) < tip < now + timedelta(days=4):
            if isinstance(game.get("round"), int):
                rounds.add(game["round"])
    state["schedule"] = {
        "season": season,
        "checked": now.isoformat(timespec="seconds"),
        "rounds": sorted(rounds),
    }
    return sorted(rounds)


def nice_word(word, keep_short=False):
    if not word.isupper():
        return word
    if keep_short and len(word) <= 2:
        return word
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), word)


def split_name(raw):
    """Macht aus 'WILLIAMS-GOSS, NIGEL' das Paar ('Nigel', 'Williams-Goss')."""
    raw = squash(raw)
    if "," in raw:
        last, first = [part.strip() for part in raw.split(",", 1)]
    else:
        parts = raw.split()
        first, last = " ".join(parts[:-1]), (parts[-1] if parts else "")
    first = " ".join(nice_word(word, keep_short=True) for word in first.split())
    last = " ".join(nice_word(word) for word in last.split())
    return first, last


def short_name(first, last):
    if not first:
        return last
    if len(first) <= 2 and first.isupper():
        return f"{first} {last}"
    return f"{first[0]}. {last}"


def number_key(number):
    return int(number) if str(number).isdigit() else 999


def squad(season, club_code):
    """Alle aktiven Spieler eines Klubs als Liste von Woerterbuechern."""
    key = (season, club_code)
    if key in _squad_cache:
        return _squad_cache[key]
    data = get_json(f"{API}/competitions/E/seasons/{season}/clubs/{club_code}/people")
    people = data if isinstance(data, list) else (data or {}).get("data") or []
    now = datetime.now(timezone.utc)
    players = []
    for entry in people:
        if entry.get("type") != "J" or entry.get("active") is False:
            continue
        end = parse_time(entry.get("endDate"))
        if end and end < now:
            continue
        first, last = split_name((entry.get("person") or {}).get("name"))
        if not last:
            continue
        players.append(
            {
                "first": first,
                "last": last,
                "number": squash(entry.get("dorsal")),
                "position": squash(entry.get("positionName")) or "Other",
            }
        )
    _squad_cache[key] = players
    return players


def squad_lines(players):
    order = ["Guard", "Forward", "Center"]
    groups = {}
    for player in players:
        groups.setdefault(player["position"], []).append(player)
    lines = []
    for position in order + sorted(p for p in groups if p not in order):
        members = groups.get(position)
        if not members:
            continue
        members.sort(key=lambda p: (number_key(p["number"]), p["last"]))
        names = ", ".join(
            (f"#{esc(p['number'])} " if p["number"] else "") + esc(p["last"])
            for p in members
        )
        label = position[0] if position in order else position
        lines.append(f"<b>{esc(label)}:</b> {names}")
    return lines


# ---------------------------------------------------------------------------
# Injury Report
# ---------------------------------------------------------------------------
def is_injury_article(article):
    slug = str(article.get("slug") or "").lower()
    title = str((article.get("heroMedia") or {}).get("title") or "").lower()
    if "eurocup" in slug or "eurocup" in title:
        return False
    return "injury-report" in slug or "injury report" in title


def article_round(article):
    candidates = [
        (article.get("heroMedia") or {}).get("title"),
        (article.get("articleMetadata") or {}).get("title"),
    ]
    for block in article.get("content") or []:
        candidates.append((block.get("customContent") or {}).get("title"))
    for text in candidates:
        match = re.search(r"round\s+(\d+)", str(text or ""), flags=re.I)
        if match:
            return int(match.group(1))
    match = re.search(r"round-(\d+)", str(article.get("slug") or ""))
    return int(match.group(1)) if match else None


def scan_articles(pages):
    """Sucht in den neuesten News der Liga nach Injury Reports."""
    found = {}
    for page in range(pages):
        data = get_json(f"{CMS}/articles?clientId=EUROLEAGUE&page={page}&size=30")
        articles = ((data or {}).get("data") or {}).get("articles") or []
        if not articles:
            break
        for article in articles:
            if article.get("slug") and is_injury_article(article):
                found.setdefault(article["slug"], article)
    return found


def fetch_article(slug):
    data = get_json(
        f"{CMS}/articles/slug/{urllib.parse.quote(slug)}?clientId=EUROLEAGUE"
    )
    return ((data or {}).get("data") or {}).get("article")


def html_to_markdown(text):
    text = re.sub(
        r"<h([1-6])[^>]*>(.*?)</h\1>",
        lambda m: "\n" + "#" * int(m.group(1)) + " " + m.group(2) + "\n",
        text,
        flags=re.I | re.S,
    )
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"<(br|/p|/li|/ul|/ol|/div)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def clean_markdown(text):
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(\*\*|__|`)", "", text)
    text = re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", text)
    text = text.replace("\\", "")
    return squash(html.unescape(text))


def parse_report(article):
    """Zerlegt den Report in Teams.

    Ergebnis: {Teamname: {"game": Ueberschrift, "statement": Text, "notes": [...]}}
    """
    parts = []
    for block in article.get("content") or []:
        if block.get("contentType") != "TEXT":
            continue
        content = str(block.get("content") or "")
        parts.append(html_to_markdown(content) if block.get("isHtml") else content)
    teams = {}
    game = None
    team = None
    for raw in "\n".join(parts).splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = re.match(r"^#{1,6}\s*(.+?)\s*#*$", line)
        bold = re.match(r"^(\*\*|__)(.{2,80}?)\1:?$", line)
        if heading or bold:
            title = clean_markdown(heading.group(1) if heading else bold.group(2))
            if not title:
                continue
            if re.search(r"\bvs\b\.?", title, flags=re.I):
                game, team = title, None
            elif re.match(
                r"^(mon|tues|wednes|thurs|fri|satur|sun)day\b", title, flags=re.I
            ):
                game, team = None, None
            elif game:
                team = title
                teams.setdefault(team, {"game": game, "statement": [], "notes": []})
            continue
        if team is None:
            continue
        bullet = re.match(r"^[-*•]\s+(.*)$", line)
        if bullet:
            note = clean_markdown(bullet.group(1))
            if note:
                teams[team]["notes"].append(note)
        else:
            statement = clean_markdown(line)
            if statement:
                teams[team]["statement"].append(statement)
    for entry in teams.values():
        entry["statement"] = " ".join(entry["statement"])
    return teams


def is_pending(entry):
    return not entry["statement"] or PENDING_TEXT in entry["statement"].lower()


def match_team(team_name, games):
    """Findet zu einem Teamnamen aus dem Report das Spiel aus dem Spielplan."""
    wanted = norm(team_name)
    if len(wanted) < 3:
        return None, None
    hits = []
    for game in games:
        for side in ("local", "road"):
            club = club_of(game, side)
            keys = [
                norm(club.get(field))
                for field in ("name", "abbreviatedName", "editorialName", "tvCode", "code")
                if club.get(field)
            ]
            for key in keys:
                if wanted == key or f" {wanted} " in f" {key} " or (
                    len(key) >= 5 and f" {key} " in f" {wanted} "
                ):
                    hits.append((game, side))
                    break
    return hits[0] if len(hits) == 1 else (None, None)


# --- Aus dem offiziellen Satz Namen und Status lesen ------------------------
NONE_RE = re.compile(
    r"no injur|no absentees|no absences|full roster|all (of its )?players (are )?available",
    re.I,
)
OUT_RE = re.compile(
    r"ruled out|absentee|\b(is|are|remains?|still) out\b|\bout for\b|will miss"
    r"|not travel|sidelined|unavailable|not play|won't play|will not be available",
    re.I,
)
GTD_RE = re.compile(r"game[- ]time decision|questionable|doubtful", re.I)
OK_RE = re.compile(
    r"probable|(?<!not be )\bavailable\b|expected to play|cleared to play"
    r"|will return|returns? to action|\bis back\b|\bare back\b",
    re.I,
)
VERB_RE = re.compile(
    r"\s(?:is|are|was|were|will|did|does|do|remains?|has|have|won't|cannot|can't)\s"
)
NAME_RE = re.compile(
    r"^[A-ZÀ-Þ][\w'’.\-]*"
    r"(?:\s+(?:[A-ZÀ-Þ][\w'’.\-]*|de|da|del|della|van|von|der|la|le|di|dos|el|al|bin))*$"
)


def split_names(text):
    text = re.sub(r"^(both|guards?|forwards?|centers?)\s+", "", text.strip(), flags=re.I)
    names = [
        squash(part)
        for part in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+|\s*&\s*", text)
        if squash(part)
    ]
    for name in names:
        if len(name.split()) > 5 or not NAME_RE.match(name):
            return None
    return names or None


def parse_statement(statement):
    """Liest Namen und Status aus dem Satz des Klubs.

    Ergebnis: {"OUT": [...], "GTD": [...], "OK": [...]} oder {"none": True}.
    Gibt None zurueck, wenn der Satz nicht sicher zu verstehen ist. Dann
    postet der Bot lieber den Originalsatz als etwas Falsches.
    """
    text = re.sub(
        r",?\s*the club (has )?(confirmed|announced|said)\.?\s*$", "", statement.strip(),
        flags=re.I,
    )
    if not text:
        return None
    if NONE_RE.search(text):
        return {"none": True}
    result = {}
    for clause in re.split(r"\s+while\s+|;\s+|,\s+but\s+|\.\s+(?=[A-Z])", text):
        clause = clause.strip(" .")
        if not clause:
            continue
        kinds = [
            kind
            for kind, pattern in (("OUT", OUT_RE), ("GTD", GTD_RE), ("OK", OK_RE))
            if pattern.search(clause)
        ]
        verb = VERB_RE.search(clause)
        if len(kinds) != 1 or not verb:
            return None
        names = split_names(clause[: verb.start()])
        if not names:
            return None
        bucket = result.setdefault(kinds[0], [])
        bucket += [name for name in names if name not in bucket]
    return result or None


def status_lines(tag, entry, notes=None):
    """Kurzform fuer ein Team, z. B. '🚫 #Barcelona: Minaya OUT'."""
    if is_pending(entry):
        lines = [f"⏳ {tag}: no report yet"]
    else:
        parsed = parse_statement(entry["statement"])
        if parsed is None:
            lines = [f"ℹ️ {tag}: {esc(entry['statement'])}"]
        elif parsed.get("none"):
            lines = [f"✅ {tag}: no injuries reported"]
        else:
            lines = []
            if parsed.get("OUT"):
                lines.append(f"\U0001f6ab {tag}: {esc(', '.join(parsed['OUT']))} <b>OUT</b>")
            if parsed.get("GTD"):
                lines.append(f"❓ {tag}: {esc(', '.join(parsed['GTD']))} <b>GTD</b>")
            if parsed.get("OK"):
                lines.append(f"✅ {tag}: {esc(', '.join(parsed['OK']))} available")
    for note in notes or []:
        lines.append(f"• {esc(note)}")
    return lines


def source_line(slug):
    link = NEWS_URL.format(slug=urllib.parse.quote(slug))
    return f'Source: <a href="{link}">EuroLeague Injury Report</a>'


def club_tag(game, side, report_teams=None):
    """Hashtag fuer einen Klub, moeglichst so wie die Liga ihn nennt."""
    for name, entry in (report_teams or {}).items():
        if entry.get("match") is game and entry.get("side") == side:
            return hashtag(name)
    club = club_of(game, side)
    return hashtag(club.get("editorialName") or club.get("abbreviatedName") or club.get("name") or "Team")


def game_tags(game, report_teams=None):
    return f"{club_tag(game, 'local', report_teams)} vs {club_tag(game, 'road', report_teams)}"


def build_overview(round_number, slug, teams, names):
    label = f" · Round {round_number}" if round_number else ""
    blocks = [f"\U0001f4cb <b>Injury Report{label}</b>"]
    headings = []
    for name in names:
        if teams[name]["game"] not in headings:
            headings.append(teams[name]["game"])
    for heading in headings:
        members = [n for n in names if teams[n]["game"] == heading]
        game = teams[members[0]].get("match")
        if game and game_time(game):
            lines = [f"<b>{game_tags(game, teams)}</b> · {format_time(game_time(game))}"]
        else:
            lines = [f"<b>{esc(heading)}</b>"]
        for name in members:
            lines += status_lines(hashtag(name), teams[name])
        blocks.append("\n".join(lines))
    blocks.append(source_line(slug))
    return "\n\n".join(blocks)


def build_update(slug, teams, changed, new_notes):
    blocks = []
    for name in changed:
        entry = teams[name]
        lines = status_lines(hashtag(name), entry, notes=new_notes.get(name))
        game, side = entry.get("match"), entry.get("side")
        if game and game_time(game):
            other = club_tag(game, "road" if side == "local" else "local", teams)
            lines.append(f"<i>vs {other} · {format_time(game_time(game))}</i>")
        blocks.append("\n".join(lines))
    blocks.append(source_line(slug))
    return "\n\n".join(blocks)


def is_changed(old, new, pending):
    if old is None:
        return not pending
    if new["s"] != old.get("s"):
        return not pending
    return any(note not in (old.get("n") or []) for note in new["n"])


def run_injuries(state, season, now):
    """Postet neue Reports und Aenderungen. Gibt {Runde: Teams} zurueck."""
    reports = state.setdefault("reports", {})
    first_run = not reports
    articles = scan_articles(6 if first_run else 1)
    for slug, info in list(reports.items()):
        if not info.get("done") and slug not in articles:
            article = fetch_article(slug)
            if article:
                articles[slug] = article

    by_round = {}
    ordered = sorted(articles.items(), key=lambda item: str(item[1].get("publishDate")))
    for slug, article in ordered:
        info = reports.get(slug)
        if info and info.get("done"):
            continue
        teams = parse_report(article)
        if not teams:
            continue
        round_number = article_round(article)
        published = parse_time(article.get("publishDate"))
        games = round_games(season, round_number) if round_number else []
        open_names = []
        for name, entry in teams.items():
            entry["match"], entry["side"] = match_team(name, games)
            if entry["match"] and game_time(entry["match"]):
                still_open = game_time(entry["match"]) > now
            else:
                still_open = bool(published) and now - published < timedelta(days=3)
            if still_open:
                open_names.append(name)
        prints = {
            name: {
                "s": fingerprint(entry["statement"]),
                "n": [fingerprint(note) for note in entry["notes"]],
            }
            for name, entry in teams.items()
        }
        record = {
            "round": round_number,
            "published": article.get("publishDate"),
            "teams": prints,
            "done": not open_names,
        }
        if info is None:
            # Teams ohne eingereichten Report kommen spaeter als Update
            ready = [name for name in open_names if not is_pending(teams[name])]
            if ready:
                send(build_overview(round_number, slug, teams, ready))
                log(f"Injury Report gepostet: {slug} ({len(ready)} Teams)")
        else:
            old = info.get("teams") or {}
            changed = [
                name
                for name in open_names
                if is_changed(old.get(name), prints[name], is_pending(teams[name]))
            ]
            if changed:
                new_notes = {}
                for name in changed:
                    known = (old.get(name) or {}).get("n") or []
                    fresh = [
                        note
                        for note in teams[name]["notes"]
                        if fingerprint(note) not in known
                    ]
                    new_notes[name] = fresh[:3]
                send(build_update(slug, teams, changed, new_notes))
                log(f"Injury-Update gepostet: {slug} ({', '.join(changed)})")
        reports[slug] = record
        save_state(state)
        if round_number and open_names:
            by_round[round_number] = teams

    # Nur die neuesten Reports im Gedaechtnis behalten
    if len(reports) > 12:
        keep = sorted(reports, key=lambda s: str(reports[s].get("published")))[-12:]
        state["reports"] = {slug: reports[slug] for slug in keep}
    return by_round


# ---------------------------------------------------------------------------
# Posts rund ums Spiel: Kader, Final 12, Starting Five
# ---------------------------------------------------------------------------
def build_squad_post(game, season, report_teams):
    head = [f"\U0001f3c0 <b>{game_tags(game, report_teams)}</b>"]
    meta = format_time(game_time(game))
    if game.get("round"):
        meta += f" · Round {game['round']}"
    head.append(meta)
    blocks = ["\n".join(head)]
    complete = True
    for side in ("local", "road"):
        club = club_of(game, side)
        players = squad(season, club.get("code")) if club.get("code") else []
        lines = [f"<b>{esc(club.get('name') or '?')}</b>"]
        if players:
            lines += squad_lines(players)
        else:
            complete = False
            lines.append("Roster currently unavailable")
        for name, entry in (report_teams or {}).items():
            if entry.get("match") is game and entry.get("side") == side:
                lines += status_lines(hashtag(name), entry)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), complete


def read_boxscore(game, season):
    """Die Spieler vom Spielbogen. None, solange die Liga noch nichts zeigt."""
    code = game.get("gameCode")
    if code is None:
        return None
    data = get_json(f"{LIVE}/Boxscore?gamecode={code}&seasoncode={season}", soft=True)
    stats = (data or {}).get("Stats") if isinstance(data, dict) else None
    if not isinstance(stats, list) or len(stats) != 2:
        return None
    codes = {side: club_of(game, side).get("code") for side in ("local", "road")}
    result = {}
    for index, team in enumerate(stats):
        entries = (team or {}).get("PlayersStats") or []
        team_codes = {squash(entry.get("Team")) for entry in entries}
        side = next(
            (s for s in ("local", "road") if codes[s] and codes[s] in team_codes),
            ("local", "road")[index],
        )
        positions = {}
        if codes.get(side):
            try:
                positions = {p["number"]: p["position"] for p in squad(season, codes[side])}
            except RuntimeError:
                positions = {}
        players = []
        for entry in entries:
            first, last = split_name(entry.get("Player"))
            if not last:
                continue
            number = squash(entry.get("Dorsal"))
            try:
                starter = int(float(entry.get("IsStarter") or 0)) == 1
            except (TypeError, ValueError):
                starter = False
            position = positions.get(number, "")
            players.append(
                {
                    "first": first,
                    "last": last,
                    "number": number,
                    "starter": starter,
                    "pos": position[0] if position in ("Guard", "Forward", "Center") else "",
                }
            )
        players.sort(key=lambda p: (number_key(p["number"]), p["last"]))
        result[side] = players
    if len(result) != 2 or any(len(players) < 8 for players in result.values()):
        return None
    return result


def has_starters(box):
    return all(sum(p["starter"] for p in players) == 5 for players in box.values())


def player_line(player):
    number = f"#{esc(player['number'])} " if player["number"] else ""
    position = f" {player['pos']}" if player["pos"] else ""
    return f"{number}{esc(short_name(player['first'], player['last']))}{position}"


def bench_text(players):
    return ", ".join(
        (f"#{esc(p['number'])} " if p["number"] else "") + esc(p["last"]) for p in players
    )


def build_final_post(game, box, report_teams, starters_only=False):
    starters_known = has_starters(box)
    title = "Starting 5" if starters_only else f"Final {max(len(p) for p in box.values())}"
    blocks = [f"\U0001f4cb <b>{title} · {game_tags(game, report_teams)}</b>"]
    for side in ("local", "road"):
        players = box[side]
        lines = [f"<b>{esc(club_of(game, side).get('name') or '?')}</b>"]
        if starters_known:
            lines += [player_line(p) for p in players if p["starter"]]
            if not starters_only:
                lines.append("Bench: " + bench_text([p for p in players if not p["starter"]]))
        else:
            lines += [player_line(p) for p in players]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def run_games(state, season, now, rounds, by_round):
    memory = state.setdefault("games", {})
    for round_number in rounds:
        games = [g for g in round_games(season, round_number) if game_time(g)]
        report_teams = by_round.get(round_number)
        for game in sorted(games, key=game_time):
            identifier = game_id(game, season)
            flags = memory.get(identifier) or {}
            flags["t"] = game_time(game).isoformat(timespec="minutes")
            minutes = (game_time(game) - now).total_seconds() / 60
            if not -30 <= minutes <= KADER_MINUTEN_VORHER:
                continue
            status = str(game.get("gameStatus") or "").lower()
            if any(word in status for word in ("postpon", "cancel", "suspend")):
                continue

            if not flags.get("squad") and minutes > 0:
                text, complete = build_squad_post(game, season, report_teams)
                if complete or minutes <= 30:
                    send(text)
                    log(f"Kader gepostet: {identifier}")
                    flags["squad"] = 1
                    memory[identifier] = flags
                    save_state(state)

            if not (flags.get("final") and flags.get("start")):
                box = read_boxscore(game, season)
                if box:
                    starters = has_starters(box)
                    if not flags.get("final"):
                        send(build_final_post(game, box, report_teams))
                        log(f"Final 12 gepostet: {identifier}")
                        flags["final"] = 1
                        if starters:
                            flags["start"] = 1
                    elif starters:
                        send(build_final_post(game, box, report_teams, starters_only=True))
                        log(f"Starting 5 gepostet: {identifier}")
                        flags["start"] = 1
                    memory[identifier] = flags
                    save_state(state)
    if len(memory) > 80:
        oldest = sorted(memory, key=lambda k: str(memory[k].get("t")))
        for key in oldest[: len(memory) - 80]:
            del memory[key]


# ---------------------------------------------------------------------------
# News von Sportando und Eurohoops
# ---------------------------------------------------------------------------
def feed_items(text):
    items = []
    for block in re.findall(r"<item\b.*?</item>", text or "", flags=re.S | re.I):

        def tag(name, source=block):
            found = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", source, flags=re.S | re.I)
            if not found:
                return ""
            value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", found.group(1), flags=re.S)
            return squash(html.unescape(re.sub(r"<[^>]+>", "", value)))

        categories = [
            squash(html.unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", c, flags=re.S)))
            for c in re.findall(r"<category\b[^>]*>(.*?)</category>", block, flags=re.S | re.I)
        ]
        try:
            published = parsedate_to_datetime(tag("pubDate"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            published = None
        if tag("title") and tag("link").startswith("http"):
            items.append(
                {
                    "title": tag("title"),
                    "link": tag("link"),
                    "published": published,
                    "categories": [c.lower() for c in categories],
                }
            )
    return items


def title_words(title):
    return {word for word in norm(title).split() if len(word) > 3}


def is_duplicate(title, recent):
    words = title_words(title)
    if not words:
        return False
    for other in recent:
        other_words = title_words(other)
        if other_words and len(words & other_words) / len(words | other_words) >= 0.6:
            return True
    return False


def run_news(state, now):
    memory = state.setdefault("news", {})
    started = memory.setdefault("started", [])
    seen = memory.setdefault("seen", [])
    titles = memory.setdefault("titles", [])
    pattern = re.compile(NEWS_MUSTER, flags=re.I)
    for source, url, category in NEWS_QUELLEN:
        items = feed_items(fetch(url, soft=True))
        if not items:
            log(f"News: {source} liefert gerade nichts.")
            continue
        fresh = source not in started
        for item in sorted(items, key=lambda i: i["published"] or now):
            key = fingerprint(item["link"])
            if key in seen:
                continue
            seen.append(key)
            if fresh:
                continue  # beim ersten Mal nur merken, nichts Altes posten
            if category and not any(category in c for c in item["categories"]):
                continue
            if item["published"] and now - item["published"] > timedelta(hours=6):
                continue
            if "injury report" in item["title"].lower():
                continue  # den Report posten wir schon selbst
            if not pattern.search(item["title"]):
                continue
            if is_duplicate(item["title"], titles):
                continue
            send(
                f"\U0001f6a8 <b>{esc(source)}:</b> {esc(item['title'])}\n"
                f'<a href="{html.escape(item["link"], quote=True)}">Read more</a>'
            )
            log(f"News gepostet: {source}: {item['title']}")
            titles.append(item["title"])
            save_state(state)
        if fresh:
            started.append(source)
    memory["seen"] = seen[-400:]
    memory["titles"] = titles[-40:]


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
def main():
    if not DRY_RUN and (not TOKEN or not CHANNEL_RAW):
        raise SystemExit(
            "FEHLER: TELEGRAM_BOT_TOKEN oder TELEGRAM_CHANNEL fehlt.\n"
            "Lege beide unter Settings > Secrets and variables > Actions an."
        )
    now = datetime.now(timezone.utc)
    season = f"E{now.year if now.month >= 8 else now.year - 1}"
    state = load_state()

    if SEND_TEST:
        send("✅ Test: the bot is connected to this channel.")
        log("Testnachricht gesendet.")

    problems = []
    by_round = {}
    try:
        by_round = run_injuries(state, season, now)
    except RuntimeError as error:
        problems.append(f"Injury Report: {error}")
    try:
        rounds = active_rounds(state, season, now)
        run_games(state, season, now, rounds, by_round)
    except RuntimeError as error:
        problems.append(f"Spiele: {error}")
    if NEWS_AN:
        try:
            run_news(state, now)
        except RuntimeError as error:
            problems.append(f"News: {error}")

    if problems:
        state["fails"] = int(state.get("fails") or 0) + 1
        for problem in problems:
            log("WARNUNG:", problem)
    else:
        state["fails"] = 0
    save_state(state)
    log("Fertig.")
    # Erst nach einer Stunde Dauerfehler rot melden, damit du nicht
    # bei jedem kurzen Aussetzer eine E-Mail von GitHub bekommst.
    if state["fails"] == 6:
        sys.exit(1)


if __name__ == "__main__":
    main()
