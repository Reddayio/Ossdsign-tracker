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

# OssDsign på Nordnet
NORDNET_URL = "https://www.nordnet.se/marknaden/aktiekurser/17041659-oss-dsign"

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

def get_avanza_owners(orderbook_id: str) -> int:
    """Hämtar antal ägare hos Avanza via pyavanza (obemannat, publikt API)."""
    import pyavanza

    async def _fetch():
        async with aiohttp.ClientSession() as session:
            return await pyavanza.get_stock_async(session, orderbook_id)

    data = asyncio.run(_fetch())
    if "numberOfOwners" not in data:
        raise RuntimeError(
            "Fältet 'numberOfOwners' saknas i Avanza-svaret. "
            f"Tillgängliga fält: {list(data.keys())}"
        )
    return int(data["numberOfOwners"])


# ---------------------------------------------------------------------------
# Nordnet
# ---------------------------------------------------------------------------

def _find_owner_count_in_text(html: str):
    """Leta efter kända JSON-nycklar för antal ägare i sidans råtext."""
    for key in NORDNET_KEY_CANDIDATES:
        m = re.search(rf'"{re.escape(key)}"\s*:\s*(\d+)', html, re.IGNORECASE)
        if m:
            return int(m.group(1)), key

    # Fallback: leta efter synlig text i stil med "Antal ägare ... 1 234"
    m = re.search(r"[Aa]ntal\s*ägare[^0-9]{0,30}([\d\s\u00A0]{2,10})", html)
    if m:
        number = re.sub(r"[\s\u00A0]", "", m.group(1))
        if number.isdigit():
            return int(number), "text-fallback (Antal ägare)"

    return None, None


def get_nordnet_owners(url: str, debug: bool = False) -> int:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html = resp.text

    if debug:
        print("---- DEBUG: söker efter ägar-relaterade nycklar i Nordnet-sidan ----")
        for m in re.finditer(r'"([A-Za-z]{3,40}[Oo]wner[A-Za-z]*)"\s*:\s*(\d+)', html):
            print(f"  Kandidat: {m.group(1)} = {m.group(2)}")
        for m in re.finditer(r"[Aa]ntal\s*ägare", html):
            start = max(0, m.start() - 20)
            print(f"  Text runt 'ägare': ...{html[start:m.start()+80]}...")
        print("---- slut på debug-utskrift ----")

    count, source = _find_owner_count_in_text(html)
    if count is None:
        raise RuntimeError(
            "Kunde inte hitta antal ägare på Nordnets sida. Sidan kan ha ändrat "
            "struktur. Kör 'python track_owners.py --debug-nordnet' och skicka "
            "utskriften för felsökning."
        )
    return count


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

    if debug_nordnet:
        get_nordnet_owners(NORDNET_URL, debug=True)
        return

    print("Hämtar antal ägare hos Avanza...")
    avanza = get_avanza_owners(AVANZA_ORDERBOOK_ID)
    print(f"  Avanza: {avanza}")

    print("Hämtar antal ägare hos Nordnet...")
    nordnet = get_nordnet_owners(NORDNET_URL)
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
