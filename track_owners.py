#!/usr/bin/env python3
"""
OssDsign-ägartracker
=====================
Hämtar aktuellt antal ägare hos Avanza och Nordnet för OssDsign (OSSD),
sparar en historikpost i data/history.json och postar en uppdatering
till Discord via en webhook.

Körs normalt en gång per dag via GitHub Actions, men går fint att
köra manuellt lokalt också:

    pip install -r requirements.txt
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    python track_owners.py

Felsökning av Nordnet-delen (om siten ändrat struktur):

    python track_owners.py --debug-nordnet
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import requests

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

# OssDsign på Avanza: avanza.se/aktier/om-aktien.html/962596/ossdsign
AVANZA_ORDERBOOK_ID = "962596"

# borsbolag.se visar en KOMBINERAD siffra (Avanza + Nordnet tillsammans) i
# vanlig, server-renderad HTML. Nordnets egen sida är en JS-app som inte
# visar ägarantal utan inloggning, så vi räknar istället ut Nordnet-delen
# genom: kombinerad_siffra - avanza_siffra.
BORSBOLAG_URL = "https://borsbolag.se/ossdsign/"

DATA_FILE = Path(__file__).parent / "data" / "history.json"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
# Valfritt: länk till din GitHub Pages-graf, t.ex.
# https://dittanvandarnamn.github.io/ossdsign-tracker/
CHART_URL = os.environ.get("CHART_URL", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Möjliga JSON-nycklar Nordnet kan tänkas använda för antal ägare.
# Skriptet letar brett för att vara motståndskraftigt mot mindre ändringar.
NORDNET_KEY_CANDIDATES = [
    "numberOfOwners",
    "numberOfShareholders",
    "ownerCount",
    "shareholderCount",
    "antalAgare",
    "antalÄgare",
]


# ---------------------------------------------------------------------------
# Avanza
# ---------------------------------------------------------------------------

def _find_key_recursive(obj, target_key: str):
    """Letar rekursivt efter en nyckel var som helst i en nästlad dict/list."""
    if isinstance(obj, dict):
        if target_key in obj:
            return obj[target_key]
        for value in obj.values():
            found = _find_key_recursive(value, target_key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key_recursive(item, target_key)
            if found is not None:
                return found
    return None


def get_avanza_owners(orderbook_id: str) -> int:
    """Hämtar antal ägare hos Avanza via pyavanza (obemannat, publikt API)."""
    import pyavanza

    async def _fetch():
        async with aiohttp.ClientSession() as session:
            return await pyavanza.get_stock_async(session, orderbook_id)

    data = asyncio.run(_fetch())

    # Avanzas API har bytt struktur över tid, så vi letar brett efter fältet
    # istället för att anta en exakt path.
    for candidate_key in ("numberOfOwners", "numberOfShareholders", "ownerCount"):
        value = _find_key_recursive(data, candidate_key)
        if value is not None:
            return int(value)

    raise RuntimeError(
        "Hittade inget ägarantal i Avanza-svaret, oavsett nästling. "
        f"Tillgängliga toppnivåfält: {list(data.keys())}. "
        "Kör 'python track_owners.py --debug-avanza' för att se hela strukturen."
    )


# ---------------------------------------------------------------------------
# Nordnet
# ---------------------------------------------------------------------------

def get_combined_owners_borsbolag(url: str, debug: bool = False) -> int:
    """Hämtar den KOMBINERADE ägarsiffran (Avanza + Nordnet) från borsbolag.se.

    Sidan är en vanlig, server-renderad WordPress-sida med texten:
    'Antal ägare av <bolag> på Avanza och Nordnet: <N> st'
    """
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html = resp.text

    if debug:
        idx = html.find("Avanza och Nordnet")
        print("---- DEBUG: kontext runt 'Avanza och Nordnet' ----")
        print(html[max(0, idx - 100):idx + 300] if idx != -1 else "Hittade inte texten alls.")
        print("---- slut på debug-utskrift ----")

    idx = html.find("Avanza och Nordnet")
    if idx == -1:
        raise RuntimeError(
            "Kunde inte hitta text om 'Avanza och Nordnet' på borsbolag.se. "
            "Sidan kan ha ändrat struktur. Kör med --debug-nordnet för mer info."
        )

    # Leta efter första talet följt av "st" inom en rimlig radie efter texten
    window = html[idx:idx + 300]
    m = re.search(r"([\d\s\u00A0]{2,10})\s*st\b", window)
    if not m:
        raise RuntimeError(
            "Hittade texten 'Avanza och Nordnet' men inget tal med 'st' efter. "
            "Kör med --debug-nordnet för att se den råa HTML-kontexten."
        )
    number = re.sub(r"[\s\u00A0]", "", m.group(1))
    if not number.isdigit():
        raise RuntimeError(f"Kunde inte tolka talet '{m.group(1)}' som ett heltal.")
    return int(number)


def get_nordnet_owners_derived(avanza_owners: int, debug: bool = False) -> int:
    """Räknar ut Nordnet-antalet: kombinerad siffra (borsbolag.se) - Avanza."""
    combined = get_combined_owners_borsbolag(BORSBOLAG_URL, debug=debug)
    nordnet = combined - avanza_owners
    if nordnet < 0:
        raise RuntimeError(
            f"Uträknat Nordnet-antal blev negativt ({nordnet}). "
            f"Kombinerad siffra: {combined}, Avanza: {avanza_owners}. "
            "Källorna kan vara ur synk (olika uppdateringstider) - kör om senare."
        )
    return nordnet


# ---------------------------------------------------------------------------
# Historik
# ---------------------------------------------------------------------------

def load_history() -> list:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return []


def save_history(history: list) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def post_to_discord(avanza: int, nordnet: int, avanza_diff: int, nordnet_diff: int):
    if not DISCORD_WEBHOOK_URL:
        print("Ingen DISCORD_WEBHOOK_URL satt – hoppar över Discord-postning.")
        return

    def fmt_diff(diff: int) -> str:
        if diff > 0:
            return f"(+{diff})"
        if diff < 0:
            return f"({diff})"
        return "(±0)"

    total = avanza + nordnet
    total_diff = avanza_diff + nordnet_diff

    embed = {
        "title": "📊 OssDsign – ägaruppdatering",
        "color": 0x2ECC71 if total_diff >= 0 else 0xE74C3C,
        "fields": [
            {
                "name": "Avanza",
                "value": f"**{avanza}** {fmt_diff(avanza_diff)}",
                "inline": True,
            },
            {
                "name": "Nordnet",
                "value": f"**{nordnet}** {fmt_diff(nordnet_diff)}",
                "inline": True,
            },
            {
                "name": "Totalt",
                "value": f"**{total}** {fmt_diff(total_diff)}",
                "inline": True,
            },
        ],
        "footer": {
            "text": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        },
    }

    if CHART_URL:
        embed["url"] = CHART_URL
        embed["description"] = f"[Se historisk graf]({CHART_URL})"

    payload = {"embeds": [embed]}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    r.raise_for_status()
    print("Postat till Discord.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    debug_nordnet = "--debug-nordnet" in sys.argv
    debug_avanza = "--debug-avanza" in sys.argv

    if debug_nordnet:
        get_combined_owners_borsbolag(BORSBOLAG_URL, debug=True)
        return

    if debug_avanza:
        import pyavanza

        async def _fetch():
            async with aiohttp.ClientSession() as session:
                return await pyavanza.get_stock_async(session, AVANZA_ORDERBOOK_ID)

        data = asyncio.run(_fetch())
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    print("Hämtar antal ägare hos Avanza...")
    avanza = get_avanza_owners(AVANZA_ORDERBOOK_ID)
    print(f"  Avanza: {avanza}")

    print("Räknar ut antal ägare hos Nordnet (via borsbolag.se kombinerad siffra)...")
    nordnet = get_nordnet_owners_derived(avanza)
    print(f"  Nordnet: {nordnet}")

    history = load_history()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prev = history[-1] if history else None
    avanza_diff = avanza - prev["avanza"] if prev else 0
    nordnet_diff = nordnet - prev["nordnet"] if prev else 0

    entry = {"date": today, "avanza": avanza, "nordnet": nordnet}

    if prev and prev["date"] == today:
        # Redan en post för idag (t.ex. manuell omkörning) – uppdatera den.
        history[-1] = entry
    else:
        history.append(entry)

    save_history(history)
    print(f"Historik sparad ({len(history)} poster totalt) i {DATA_FILE}")

    try:
        import build_excel
        build_excel.main()
    except Exception as e:  # Excel-filen ska aldrig stoppa Discord-postningen
        print(f"Varning: kunde inte uppdatera Excel-filen: {e}")

    post_to_discord(avanza, nordnet, avanza_diff, nordnet_diff)


if __name__ == "__main__":
    main()
