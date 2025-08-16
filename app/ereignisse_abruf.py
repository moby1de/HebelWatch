# -*- coding: utf-8 -*-
"""
ereignisse_abruf.py
Kalender für HebelWatch (Ampel 3) mit:
- Jahresbasierte fixe Termine (Hexensabbat, DAX-Heuristik)
- Offizielle Fetcher (best effort) für DAX/ES50/SPDJI + Zinsentscheide
- Yahoo-Earnings (heute & morgen)
- Tages-Cache, Debug-Logging
"""

from __future__ import annotations
from bs4 import BeautifulSoup
import os
import json
import re
import requests
from datetime import datetime, timedelta, date
from typing import List, Dict, Any, Optional
#from HebelWatchv30 import is_market_open  # Hier den richtigen Import-Pfad verwenden


# --- Helpers (oben in der Datei) -----------------------------------
import requests

FRED_API_KEY = "ec6efd7df5925ad3852f5bf4f2c8ac2e"   # dein echter Key
TE_API_KEY   = "123456"                               # echter TE-Key (Token oder "user:pass")

def _fred_get(path, params):
    key = FRED_API_KEY
    if not key or requests is None:
        return None
    base = "https://api.stlouisfed.org/fred"
    params = {**params, "api_key": key, "file_type": "json"}
    try:
        r = requests.get(f"{base}/{path}", params=params, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def _te_get(url):
    key = TE_API_KEY
    if not key or requests is None:
        return None
    try:
        if ":" in key:
            auth = tuple(key.split(":", 1))
            r = requests.get(url, auth=auth, timeout=12)
        else:
            r = requests.get(f"{url}&c={key}", timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

# --- US: FRED -------------------------------------------------------
def _fetch_us_cpi_dates_via_fred():
    rel = _fred_get("releases", {"search_text": "Consumer Price Index"})
    if not rel or "releases" not in rel or not rel["releases"]:
        return []
    release_id = sorted(rel["releases"], key=lambda x: x.get("id", 0))[-1]["id"]

    today = date.today()
    end   = date(today.year + 2, today.month, today.day)
    dates = _fred_get("release/dates", {
        "release_id": release_id,
        "include_release_dates_with_no_data": "true",
        "realtime_start": today.strftime("%Y-%m-%d"),
        "realtime_end":   end.strftime("%Y-%m-%d"),
    })
    out = []
    if dates and "release_dates" in dates:
        for d in dates["release_dates"]:
            ds = d.get("date")
            if not ds:
                continue
            out.append({"datum": ds, "typ": "CPI",
                        "text": "US Verbraucherpreise (CPI)",
                        "index": "SP500"})  # ggf. "S&P 500"
    return out

# --- DE: TradingEconomics ------------------------------------------
def _fetch_de_cpi_dates_via_tradingeconomics():
    js = _te_get("https://api.tradingeconomics.com/calendar/country/germany?format=json")
    out = []
    if not js:
        return out
    for it in js:
        name = (it.get("Event") or it.get("Category") or "").lower()
        if any(k in name for k in ("cpi", "inflation")):
            ds = (it.get("DateUtc") or it.get("Date") or "")[:10]
            if not ds:
                continue
            # robustes Parsing
            d = None
            for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
                try:
                    d = datetime.strptime(ds, fmt).date()
                    break
                except ValueError:
                    continue
            if d:
                out.append({"datum": d.strftime("%Y-%m-%d"), "typ": "CPI",
                            "text": "DE Verbraucherpreise (VPI)",
                            "index": "DAX"})
    # Dedupe
    seen, res = set(), []
    for e in out:
        k = (e["datum"], e["typ"], e["index"])
        if k not in seen:
            seen.add(k); res.append(e)
    return res

# --- Gesamte Schnittstelle (wird in lade_oder_erstelle_ereignisse() benutzt)
def fetch_cpi_events(debug: bool=False) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        us = _fetch_us_cpi_dates_via_fred()
        if debug: print(f"[Fetcher] US CPI erkannt: {[e['datum'] for e in us]}")
        rows += us
    except Exception:
        if debug: print("[Fetcher] US CPI Fehler")

    try:
        de = _fetch_de_cpi_dates_via_tradingeconomics()
        if debug: print(f"[Fetcher] DE CPI erkannt: {[e['datum'] for e in de]}")
        rows += de
    except Exception:
        if debug: print("[Fetcher] DE CPI Fehler")

    if debug: print(f"[Fetcher] CPI US/DE gesamt: {len(rows)} Einträge.")
    return rows


# ------------------------------------------------------------
# Index normalisieren
# ------------------------------------------------------------
def _normalize_index(name: str) -> str:
    if not name:
        return ""
    n = name.strip().upper()
    mapping = {
        "DAX": "DAX",
        "EURO STOXX 50": "EURO STOXX 50",
        "EUROSTOXX50": "EURO STOXX 50",
        "ESTOXX50": "EURO STOXX 50",
        "SX5E": "EURO STOXX 50",
        "S&P 500": "S&P 500",
        "SP500": "S&P 500",
        "S&P500": "S&P 500",
        "SPX": "S&P 500",
        "DOW JONES": "DOW JONES",
        "DJIA": "DOW JONES",
        "DJI": "DOW JONES",
        "ALL": "ALL",
    }
    return mapping.get(n, n)

# ------------------------------------------------------------
# Firmenliste für Earnings
# ------------------------------------------------------------
TECH_FIRMEN: Dict[str, str] = {
    "AAPL": "Apple", "MSFT": "Microsoft", "AMZN": "Amazon", "NVDA": "Nvidia",
    "GOOGL": "Alphabet", "META": "Meta Platforms", "TSLA": "Tesla", "ADBE": "Adobe",
    "INTC": "Intel", "AMD": "AMD", "CRM": "Salesforce", "IBM": "IBM",
    "SAP.DE": "SAP", "IFX.DE": "Infineon", "SIE.DE": "Siemens", "DTE.DE": "Deutsche Telekom",
    "ADS.DE": "Adidas", "ASML.AS": "ASML", "OR.PA": "L'Oréal", "MC.PA": "LVMH", "AIR.PA": "Airbus"
}

# ------------------------------------------------------------
# Regeln
# ------------------------------------------------------------
def _third_friday(y: int, m: int) -> date:
    d = date(y, m, 1)
    offset = (4 - d.weekday()) % 7  # Friday=4
    first_friday = d + timedelta(days=offset)
    return first_friday + timedelta(weeks=2)

def rule_hexensabbat(year: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for m in (3, 6, 9, 12):
        dte = _third_friday(year, m)
        rows.append({"datum": dte.strftime("%Y-%m-%d"), "typ": "Hexensabbat",
                     "text": "Großer Verfallstag (Hexensabbat)", "index": "ALL"})
    return rows

def rule_dax_rebalancing_after_hexensabbat(year: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for m in (3, 6, 9, 12):
        fri = _third_friday(year, m)
        mon = fri + timedelta(days=3)
        rows.append({"datum": mon.strftime("%Y-%m-%d"), "typ": "Rebalancing",
                     "text": "DAX/MDAX-Rebalancing (heuristisch)", "index": "DAX"})
    return rows

# ------------------------------------------------------------
# Fetcher: Zinsentscheide (mit Fallback LISTEN wie in deiner alten Datei)
# ------------------------------------------------------------
ECB_FALLBACK_2025 = ["2025-01-23","2025-03-13","2025-04-10","2025-06-12",
                     "2025-07-17","2025-09-11","2025-10-30","2025-12-11"]

FOMC_FALLBACK_2025 = ["2025-01-29","2025-03-19","2025-05-07","2025-06-18",
                      "2025-07-30","2025-09-17","2025-11-05","2025-12-17"]

def fetch_fomc(year: int, debug: bool=False) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    html = _fetch("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
    if html and BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(text=re.compile(rf"\b{year}\b")):
            m = re.search(r"([A-Za-z]+)\s+(\d{1,2})(?:–|-)?(\d{0,2}),\s*%d" % year, tag)
            if m:
                mm, dd = m.group(1), m.group(2)
                try:
                    d = datetime.strptime(f"{mm} {dd} {year}", "%B %d %Y").date()
                    rows.append({"datum": d.strftime("%Y-%m-%d"), "typ": "FED", "text": "FED-Zinsentscheid", "index": "ALL"})
                except Exception:
                    pass
    if not rows and year == 2025:
        rows = [{"datum": d, "typ": "FED", "text": "FED-Zinsentscheid", "index": "ALL"} for d in FOMC_FALLBACK_2025]
        if debug: print("[Fallback] FOMC 2025 Termine verwendet:", FOMC_FALLBACK_2025)
    if debug: print(f"[Fetcher] FOMC {year}: {len(rows)} Termine.")
    return rows

def fetch_ecb(year: int, debug: bool=False) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    html = _fetch("https://www.ecb.europa.eu/press/calendars/mgc/html/index.en.html")
    if html and BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(text=re.compile(rf"\b{year}\b")):
            m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+%d" % year, tag)
            if m:
                dd, mon = m.group(1), m.group(2)
                try:
                    d = datetime.strptime(f"{dd} {mon} {year}", "%d %B %Y").date()
                    rows.append({"datum": d.strftime("%Y-%m-%d"), "typ": "EZB", "text": "EZB-Zinsentscheid", "index": "ALL"})
                except Exception:
                    pass
    if not rows and year == 2025:
        rows = [{"datum": d, "typ": "EZB", "text": "EZB-Zinsentscheid", "index": "ALL"} for d in ECB_FALLBACK_2025]
        if debug: print("[Fallback] EZB 2025 Termine verwendet:", ECB_FALLBACK_2025)
    if debug: print(f"[Fetcher] EZB {year}: {len(rows)} Termine.")
    return rows

# ------------------------------------------------------------
# Fetcher: Indizes (best effort)
# ------------------------------------------------------------
def fetch_deutsche_boerse_dax_reviews(year: int, debug: bool=False) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    html = _fetch("https://www.deutsche-boerse.com/dbg-de/ueber-uns/presse/pressemitteilungen")
    if html and BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            txt = (link.get_text() or "").strip()
            if str(year) in txt and re.search(r"(DAX|MDAX|TecDAX).*(Review|Überprüfung|Indexanpassung)", txt, re.IGNORECASE):
                sub = _fetch(link["href"]) if link["href"].startswith("http") else None
                if sub:
                    for m in re.finditer(r"(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})", sub):
                        ds = m.group(1)
                        try:
                            dt = datetime.strptime(ds, "%d.%m.%Y").date() if "." in ds else datetime.strptime(ds, "%Y-%m-%d").date()
                            rows.append({"datum": dt.strftime("%Y-%m-%d"), "typ": "Rebalancing",
                                         "text": "DAX/MDAX/TecDAX Review (offiziell)", "index": "DAX"})
                        except Exception:
                            pass
    if debug: print(f"[Fetcher] Deutsche Börse DAX {year}: {len(rows)} Termine.")
    return rows

def fetch_stoxx_blue_chip_reviews(year: int, debug: bool=False) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    html = _fetch("https://www.stoxx.com/index-reviews")
    if html and BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(text=re.compile(rf"\b{year}\b")):
            if re.search(r"(EURO\s*STOXX\s*50|SX5E)", tag, re.IGNORECASE):
                ctx = tag.parent.get_text(" ", strip=True) if tag and tag.parent else str(tag)
                for m in re.finditer(r"(\d{1,2}\s+[A-Za-z]+\s+%d|\d{4}-\d{2}-\d{2})" % year, ctx):
                    ds = m.group(1)
                    try:
                        dt = datetime.strptime(ds, "%Y-%m-%d").date() if "-" in ds else datetime.strptime(ds, "%d %B %Y").date()
                        rows.append({"datum": dt.strftime("%Y-%m-%d"), "typ": "Rebalancing",
                                     "text": "EURO STOXX 50 Review (offiziell)", "index": "EURO STOXX 50"})
                    except Exception:
                        pass
    if debug: print(f"[Fetcher] STOXX ES50 {year}: {len(rows)} Termine.")
    return rows

def fetch_spdji_changes(year: int, include_minor: bool=True, debug: bool=False) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    html = _fetch("https://www.spglobal.com/spdji/en/index-notices/")
    if html and BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            txt = (link.get_text() or "").strip()
            if not txt:
                continue
            if re.search(rf"\b{year}\b", txt) and (("S&P 500" in txt) or ("Dow Jones" in txt) or ("DJIA" in txt)):
                if not include_minor and not re.search(r"(reconstitution|rebalance|index changes?)", txt, re.IGNORECASE):
                    continue
                sub = _fetch(link["href"]) if link["href"].startswith("http") else None
                if not sub:
                    continue
                for m in re.finditer(r"(\d{4}-\d{2}-\d{2}|\b[A-Za-z]+\s+\d{1,2},\s+\d{4}\b)", sub):
                    ds = m.group(1)
                    try:
                        dt = datetime.strptime(ds, "%Y-%m-%d").date() if "-" in ds else datetime.strptime(ds, "%B %d, %Y").date()
                        idx = "S&P 500" if re.search(r"S&P\s*500", sub, re.IGNORECASE) else ("DOW JONES" if re.search(r"(Dow Jones|DJIA)", sub, re.IGNORECASE) else "ALL")
                        rows.append({"datum": dt.strftime("%Y-%m-%d"), "typ": "Index Notice",
                                     "text": "S&P DJI: Indexänderung/Notice", "index": idx})
                    except Exception:
                        pass
    if debug: print(f"[Fetcher] S&P DJI {year}: {len(rows)} Notices.")
    return rows

# ------------------------------------------------------------
# Yahoo Earnings (heute & morgen)
# ------------------------------------------------------------
def get_tech_earnings_events(debug: bool=False) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if requests is None or BeautifulSoup is None:
        if debug: print("[Ereignisse] requests/BeautifulSoup nicht installiert – Earnings werden übersprungen.")
        return results

    base_url = "https://finance.yahoo.com/calendar/earnings?day={date}"
    headers = {"User-Agent": "Mozilla/5.0"}

    for offset in range(2):
        date_obj = datetime.today().date() + timedelta(days=offset)
        iso_date = date_obj.strftime("%Y-%m-%d")
        url = base_url.format(date=iso_date)
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            symbols = set(re.findall(r'"symbol":"([A-Z.\-]+)"', soup.text))
            for symbol in symbols:
                if symbol in TECH_FIRMEN:
                    results.append({
                        "datum": iso_date,
                        "typ": "Earnings",
                        "text": f"{TECH_FIRMEN[symbol]} Quartalsbericht",
                        "index": "ALL"
                    })
        except Exception as e:
            results.append({
                "datum": iso_date,
                "typ": "Earnings",
                "text": f"⚠ Fehler beim Abruf Yahoo Earnings: {str(e)}",
                "index": "ALL"
            })
    if debug: print(f"[Fetcher] Yahoo Earnings heute+morgen: {len(results)} Einträge.")
    return results

# ------------------------------------------------------------
# Jahresereignisse zusammenstellen
# ------------------------------------------------------------
def fetch_fixed_events(year: int, include_minor_us_changes: bool=True, debug: bool=False) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    # Zinsentscheide (mit Fallbacks)
    try: events += fetch_fomc(year, debug=debug)
    except Exception: pass
    try: events += fetch_ecb(year, debug=debug)
    except Exception: pass
    # Regeln
    events += rule_hexensabbat(year)
    events += rule_dax_rebalancing_after_hexensabbat(year)
    # Offizielle Index-Termine
    try: events += fetch_deutsche_boerse_dax_reviews(year, debug=debug)
    except Exception: pass
    try: events += fetch_stoxx_blue_chip_reviews(year, debug=debug)
    except Exception: pass
    try: events += fetch_spdji_changes(year, include_minor=include_minor_us_changes, debug=debug)
    except Exception: pass
    return events

# ------------------------------------------------------------
# CPI (US + DE) – robustes Scraping (best effort)
# ------------------------------------------------------------
# ------------------------------------------------------------
# CPI (US + DE) – robuste Version mit Fallbacks und Debug
# ------------------------------------------------------------
from typing import List, Dict, Any

def fetch_cpi_events(debug: bool=False) -> List[Dict[str, Any]]:
    """
    Liefert CPI-Termine (US via FRED, DE via TradingEconomics) im Format:
    {"datum": "YYYY-MM-DD", "typ": "CPI", "text": "...", "index": "SP500|DAX"}
    Benötigt Env-Vars:
      - FRED_API_KEY
      - TE_API_KEY  (Token ODER "user:pass")
    """
    import os, re
    from datetime import date, datetime, timedelta
    try:
        import requests  # wird im Projekt ohnehin genutzt
    except Exception:
        if debug: print("[Fetcher] CPI: requests fehlt – übersprungen.")
        return []

    def log(msg): 
        if debug: print(msg)

    headers = globals().get("UA", None)  # falls in deiner Datei definiert
    req = lambda url, **kw: requests.get(url, headers=headers, timeout=12, **kw)

    rows: List[Dict[str, Any]] = []

    # -------- US CPI via FRED --------
    us_dates = set()
    fred_key = os.getenv("FRED_API_KEY")
    if fred_key:
        base = "https://api.stlouisfed.org/fred"
        try:
            # 1) Release-ID für "Consumer Price Index" suchen
            r = req(f"{base}/releases", params={"search_text":"Consumer Price Index",
                                                "api_key": fred_key, "file_type":"json"})
            rid = None
            if r.ok:
                js = r.json().get("releases", [])
                if js:
                    # Bevorzuge exakten Namen, sonst größte ID
                    js.sort(key=lambda x: (x.get("name","").lower() != "consumer price index", x.get("id",0)))
                    rid = js[0].get("id")

            # 2) Release-Daten abrufen
            if rid:
                r2 = req(f"{base}/release/dates", params={
                    "release_id": rid,
                    "include_release_dates_with_no_data": "true",
                    "api_key": fred_key,
                    "file_type": "json",
                })
                if r2.ok:
                    for it in r2.json().get("release_dates", []):
                        ds = it.get("date")
                        if not ds:
                            continue
                        try:
                            d = datetime.strptime(ds, "%Y-%m-%d").date()
                            if d >= date.today() - timedelta(days=7):  # nur nahe Vergangenheit + Zukunft
                                us_dates.add(d)
                        except ValueError:
                            pass
            else:
                log("[Fetcher] FRED: keine passende CPI-Release-ID gefunden.")
        except Exception:
            log("[Fetcher] FRED: Fehler beim Abruf.")
    else:
        log("[Fetcher] FRED_API_KEY fehlt – US CPI übersprungen.")

    for d in sorted(us_dates):
        rows.append({"datum": d.strftime("%Y-%m-%d"),
                     "typ": "CPI",
                     "text": "US Verbraucherpreise (CPI)",
                     "index": "SP500"})

    log(f"[Fetcher] US CPI erkannt: {[d.strftime('%Y-%m-%d') for d in sorted(us_dates)]}")

    # -------- DE CPI via TradingEconomics --------
    de_dates = set()
    te_key = os.getenv("TE_API_KEY")
    if te_key:
        try:
            url = "https://api.tradingeconomics.com/calendar/country/germany?format=json"
            if ":" in te_key:
                auth = tuple(te_key.split(":", 1))
                r = req(url, auth=auth)
            else:
                r = req(f"{url}&c={te_key}")
            if r.ok:
                js = r.json()
                for it in js:
                    name = (it.get("Event") or it.get("Category") or "").lower()
                    if any(k in name for k in ("cpi", "inflation")):
                        ds = (it.get("DateUtc") or it.get("Date") or "")[:10]
                        if not ds:
                            continue
                        # Date-Parsing robust
                        d = None
                        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
                            try:
                                d = datetime.strptime(ds, fmt).date()
                                break
                            except ValueError:
                                continue
                        if d and d >= date.today() - timedelta(days=7):
                            de_dates.add(d)
            else:
                log(f"[Fetcher] TE: HTTP {r.status_code}")
        except Exception:
            log("[Fetcher] TE: Fehler beim Abruf.")
    else:
        log("[Fetcher] TE_API_KEY fehlt – DE CPI übersprungen.")

    for d in sorted(de_dates):
        rows.append({"datum": d.strftime("%Y-%m-%d"),
                     "typ": "CPI",
                     "text": "DE Verbraucherpreise (VPI)",
                     "index": "DAX"})

    log(f"[Fetcher] DE CPI erkannt: {[d.strftime('%Y-%m-%d') for d in sorted(de_dates)]}")

    # -------- Dedupe & Ergebnis --------
    seen, out = set(), []
    for e in rows:
        key = (e["datum"], e["typ"], e["index"])
        if key not in seen:
            seen.add(key); out.append(e)

    log(f"[Fetcher] CPI US/DE gesamt: {len(out)} Einträge.")
    return out



# ------------------------------------------------------------
# Tages-Cache + Aggregation
# ------------------------------------------------------------
def lade_oder_erstelle_ereignisse(debug: bool=False) -> List[Dict[str, Any]]:
    heute = datetime.today().date()
    pfad = os.path.join("ereignisse", f"events_{heute.isoformat()}.json")

    if os.path.exists(pfad):
        with open(pfad, "r", encoding="utf-8") as f:
            events = json.load(f)
        if debug: print(f"[Ereignisse] {len(events)} Events aus Cache geladen ({pfad})")
        return events

    os.makedirs("ereignisse", exist_ok=True)

    year = heute.year
    fixed = fetch_fixed_events(year, include_minor_us_changes=True, debug=debug)
    if debug: print(f"[Ereignisse] Feste Termine {year}: {len(fixed)} Einträge.")

    earnings_events = get_tech_earnings_events(debug=debug)

    if debug: print(f"[Ereignisse] Earnings-Events: {len(earnings_events)} Einträge.")
    cpi_events = fetch_cpi_events(debug=debug) 
# NEU: CPI einsammeln
    cpi_events = fetch_cpi_events(debug=debug)  # <— NEU
    events: List[Dict[str, Any]] = fixed + earnings_events + cpi_events

    with open(pfad, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, ensure_ascii=False)

    if debug: print(f"[Ereignisse] Gesamt: {len(events)} Events gespeichert ({pfad})")
    return events

# ------------------------------------------------------------
# Bewertung: heute / morgen -> Ampel 3
# ------------------------------------------------------------
def bewerte_ampel_3(ereignisse: List[Dict[str, Any]], indexname: str):
    heute = datetime.today().date()
    morgen = heute + timedelta(days=1)
    rot = []
    gelb = []

    canon = _normalize_index(indexname)
    from HebelWatch import is_market_open 

    for ev in ereignisse:
        try:
            ev_datum = datetime.strptime(ev["datum"], "%Y-%m-%d").date()
        except Exception:
            continue

        ev_idx = _normalize_index(ev.get("index", "ALL"))

        relevant = (ev_idx == "ALL") or (ev_idx == canon) or (ev["typ"] == "CPI")
        if not relevant:
            continue

        if ev_datum == heute:
            rot.append(f"🔴 {ev.get('text','(ohne Text)')} ({ev_datum})")
        elif ev_datum == morgen:
            gelb.append(f"🟡 {ev.get('text','(ohne Text)')} ({ev_datum})")

    if rot:
        return "red", "\n".join(rot)
    elif gelb:
        return "yellow", "\n".join(gelb)
    else:
        market_status = is_market_open(indexname)  # Marktstatus abfragen
        return "#90EE90", f"Kommentar: Keine marktrelevanten Ereignisse. "
# {market_status}

__all__ = ['lade_oder_erstelle_ereignisse', 'bewerte_ampel_3']
