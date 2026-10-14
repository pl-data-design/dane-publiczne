"""Zbieranie publicznych danych: legislacja i nabory dotacji.

Uruchamiany przez GitHub Actions. Pobiera:
  1. nowe akty z Dziennika Ustaw i Monitora Polskiego (oficjalne API ELI Sejmu),
  2. nowe druki sejmowe i przebieg związanych z nimi procesów legislacyjnych (API Sejmu),
  3. harmonogramy naborów programów regionalnych (strony urzędów, z poszanowaniem robots.txt).
Wyniki zapisuje w katalogu dane/ jako JSON i tekst.
"""

import csv
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads(Path(os.environ.get("KOLEKTOR_CONFIG", ROOT / "kolektor" / "zrodla.json")).read_text(encoding="utf-8"))
OUT = Path(os.environ.get("KOLEKTOR_OUT", ROOT / "dane"))
SEJM = os.environ.get("SEJM_API", "https://api.sejm.gov.pl")
UA = "Python-urllib/%d.%d" % sys.version_info[:2]  # standardowy identyfikator biblioteki Pythona
TZ_PL = dt.timezone(dt.timedelta(hours=1))  # przybliżenie; dzień liczony z zapasem
MAX_FILE_TEXT = 300_000
MAX_PROCESSES = 400
errors = []


def log(msg):
    print(msg, flush=True)


def http_get(url, accept="application/json", retries=2, timeout=40):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__ + ": " + str(e)[:120]
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url} -> {last}")


def get_json(url):
    body, _ = http_get(url)
    return json.loads(body.decode("utf-8"))


def parse_date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ---------- stan ----------

def load_state():
    p = OUT / "stan_kolektora.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def save_json(name, data):
    p = OUT / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- 1. ELI: Dziennik Ustaw i Monitor Polski ----------

def list_acts(publisher, year):
    base = f"{SEJM}/eli/acts/{publisher}/{year}"
    data = get_json(base)
    items = list(data.get("items", data if isinstance(data, list) else []))
    total = data.get("totalCount", len(items)) if isinstance(data, dict) else len(items)
    guard = 0
    while len(items) < total and guard < 20:
        guard += 1
        more = get_json(f"{base}?offset={len(items)}&limit=500")
        new = more.get("items", []) if isinstance(more, dict) else []
        if not new:
            break
        items.extend(new)
    return items


def collect_acts(since, today):
    out = []
    for pub in ("DU", "MP"):
        for year in sorted({since.year, today.year}):
            try:
                items = list_acts(pub, year)
            except Exception as e:  # noqa: BLE001
                errors.append(f"ELI {pub}/{year}: {e}")
                continue
            for a in items:
                d = parse_date(a.get("announcementDate") or a.get("promulgation") or a.get("changeDate"))
                if not d or d < since:
                    continue
                pos = a.get("pos")
                out.append({
                    "publikator": pub,
                    "rok": a.get("year", year),
                    "poz": pos,
                    "adres": a.get("displayAddress") or a.get("address"),
                    "tytul": a.get("title"),
                    "typ": a.get("type"),
                    "status": a.get("status"),
                    "data_ogloszenia": a.get("announcementDate") or a.get("promulgation"),
                    "link_pdf": f"{SEJM}/eli/acts/{pub}/{a.get('year', year)}/{pos}/text.pdf" if pos else None,
                })
    out.sort(key=lambda x: (x["publikator"], x["data_ogloszenia"] or "", x["poz"] or 0))
    return out


# ---------- 2. Sejm: druki i procesy ----------

def current_term():
    if CONFIG.get("sejm_kadencja"):
        return int(CONFIG["sejm_kadencja"])
    try:
        terms = get_json(f"{SEJM}/sejm/term")
        cur = [t for t in terms if t.get("current")]
        if cur:
            return int(cur[0]["num"])
        return int(max(t["num"] for t in terms))
    except Exception as e:  # noqa: BLE001
        errors.append(f"Kadencja Sejmu: {e} (przyjęto 10)")
        return 10


def collect_prints(term, since):
    try:
        prints = get_json(f"{SEJM}/sejm/term{term}/prints")
    except Exception as e:  # noqa: BLE001
        errors.append(f"Druki: {e}")
        return []
    out = []
    for p in prints:
        dates = [parse_date(p.get(k)) for k in ("changeDate", "deliveryDate", "documentDate")]
        dates = [d for d in dates if d]
        if not dates or max(dates) < since:
            continue
        num = str(p.get("number"))
        out.append({
            "numer": num,
            "tytul": p.get("title"),
            "data_dokumentu": p.get("documentDate"),
            "data_doreczenia": p.get("deliveryDate"),
            "data_zmiany": p.get("changeDate"),
            "proces": [str(x) for x in (p.get("processPrint") or [])],
            "link": f"https://www.sejm.gov.pl/Sejm{term}.nsf/druk.xsp?nr={urllib.parse.quote(num)}",
        })
    out.sort(key=lambda x: x["numer"])
    return out


def flatten_stages(stages, depth=0):
    rows = []
    for s in stages or []:
        rows.append({
            "etap": s.get("stageName") or s.get("name"),
            "data": s.get("date"),
            "poziom": depth,
            "decyzja": s.get("decision") or s.get("result"),
        })
        rows.extend(flatten_stages(s.get("children"), depth + 1))
    return rows


def collect_processes(term, numbers):
    out = []
    for num in numbers:
        try:
            p = get_json(f"{SEJM}/sejm/term{term}/processes/{urllib.parse.quote(str(num))}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"Proces {num}: {e}")
            continue
        finally:
            time.sleep(0.3)  # łagodne tempo zapytań
        stages = flatten_stages(p.get("stages"))
        out.append({
            "numer": str(num),
            "tytul": p.get("title"),
            "opis": (p.get("description") or "")[:500],
            "przekazano_do_sejmu": p.get("documentDate") or p.get("processStartDate"),
            "uchwalona": p.get("passed"),
            "ostatni_etap": stages[-1] if stages else None,
            "etapy": stages,
            "link": f"https://www.sejm.gov.pl/Sejm{term}.nsf/PrzebiegProc.xsp?nr={urllib.parse.quote(str(num))}",
        })
    return out


# ---------- 3. Harmonogramy regionalne ----------

_robots = {}


def allowed(url):
    parts = urllib.parse.urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            body, _ = http_get(root + "/robots.txt", accept="text/plain", retries=1, timeout=20)
            rp.parse(body.decode("utf-8", "replace").splitlines())
        except Exception:  # noqa: BLE001  brak robots.txt = brak ograniczeń
            rp.parse([])
        _robots[root] = rp
    return _robots[root].can_fetch(UA, url)


def html_to_text(raw):
    t = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6, "lipca": 7,
    "sierpnia": 8, "wrzesnia": 9, "września": 9, "pazdziernika": 10, "października": 10,
    "listopada": 11, "grudnia": 12,
}
DMY = re.compile(r"(?<!\d)(\d{1,2})[._-](\d{1,2})[._-](20\d{2})(?!\d)")
DMY_COMPACT = re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")
D_MONTH_Y = re.compile(r"(?<!\d)(\d{1,2})[\s_-]+(" + "|".join(MONTHS) + r")[\s_-]+(20\d{2})", re.I)
YMD = re.compile(r"(?<![\d.])(20\d{2})-(\d{2})-(\d{2})(?![\d.])")
SKIP_RE = re.compile(r"(opis\w*|karta|wykaz)\W*zmian", re.I)
FILE_EXTS = (".pdf", ".xlsx", ".xlsm", ".xls")
EXT_PRIORITY = {".xlsx": 3, ".xlsm": 3, ".xls": 2, ".pdf": 1}


def dates_in(text):
    """Daty z tekstu: najpierw dd.mm.rrrr i „15 września 2026”, potem rrrr-mm-dd. Odrzuca daty z przyszłości."""
    text = urllib.parse.unquote(text or "")
    found, spans = [], []
    for rx, kind in ((D_MONTH_Y, "month"), (DMY, "dmy"), (DMY_COMPACT, "dmy"), (YMD, "ymd")):
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in spans):
                continue
            g = m.groups()
            try:
                if kind == "month":
                    d = dt.date(int(g[2]), MONTHS[g[1].lower()], int(g[0]))
                elif kind == "dmy":
                    d = dt.date(int(g[2]), int(g[1]), int(g[0]))
                else:
                    d = dt.date(int(g[0]), int(g[1]), int(g[2]))
            except ValueError:
                continue
            spans.append((m.start(), m.end()))
            if d <= dt.date.today() + dt.timedelta(days=60):
                found.append(d)
    return found


def guess_ext(href, label):
    ext = Path(urllib.parse.urlsplit(href).path).suffix.lower()
    if ext in FILE_EXTS:
        return ext
    if ext in (".docx", ".doc", ".zip", ".odt"):
        return None
    words = re.findall(r"[a-z]+", label.lower())
    for e in (".xlsm", ".xlsx", ".pdf", ".xls"):
        if e[1:] in words:
            return e
    return None


def rank_key(c):
    return (c["data"] or dt.date(1900, 1, 1), EXT_PRIORITY.get(c["ext"], 0), -c["kolejnosc"])


def find_schedule_files(raw, base):
    """Zwraca kandydatów na plik harmonogramu, od najnowszego (wg dat w nazwie, opisie i kontekście)."""
    page_is_schedule = "harmonogram" in (base + " " + raw[:5000]).lower()
    cands = []
    for idx, m in enumerate(re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', raw)):
        href, label = html.unescape(m.group(1)), html_to_text(m.group(2))
        full = urllib.parse.urljoin(base, href)
        after = html_to_text(raw[m.end():m.end() + 160])
        ext = guess_ext(full, label) or (guess_ext(full, after) if "download" in href.lower() else None)
        if not ext:
            continue
        hay = (urllib.parse.unquote(full) + " " + label).lower()
        if SKIP_RE.search(urllib.parse.unquote(hay)):
            continue
        if not (page_is_schedule or "harmonogram" in hay):
            continue
        context = html_to_text(raw[max(0, m.start() - 1500):m.start()])
        ctx_dates = dates_in(context)
        own_dates = dates_in(urllib.parse.unquote(full) + " " + label)
        best = max(own_dates) if own_dates else (ctx_dates[-1] if ctx_dates else None)
        cands.append({"url": full, "opis": (label or after)[:200], "ext": ext, "data": best, "kolejnosc": idx})
    uniq = {}
    for c in cands:
        if c["url"] not in uniq:
            uniq[c["url"]] = c
    ranked = sorted(uniq.values(), key=rank_key, reverse=True)
    return ranked


def file_to_text(data, ext):
    if ext in (".xlsx", ".xlsm"):
        import openpyxl  # noqa: PLC0415
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        buf = io.StringIO()
        w = csv.writer(buf)
        for ws in wb.worksheets:
            w.writerow([f"# arkusz: {ws.title}"])
            for row in ws.iter_rows(values_only=True):
                if any(c not in (None, "") for c in row):
                    w.writerow(["" if c is None else str(c).strip() for c in row])
        return buf.getvalue(), ".csv"
    if ext == ".pdf":
        from pdfminer.high_level import extract_text  # noqa: PLC0415
        return extract_text(io.BytesIO(data)), ".txt"
    return None, None


def collect_regions(prev):
    result = {}
    for region, cfg_urls in CONFIG["regiony"].items():
        urls = cfg_urls if isinstance(cfg_urls, list) else ([cfg_urls] if cfg_urls else [])
        slug = re.sub(r"[^a-z0-9]+", "-", region.lower().translate(str.maketrans("ąćęłńóśźż", "acelnoszz"))).strip("-")
        entry = {"url": urls, "status": None, "zmiana": None, "strony": [], "pliki": []}
        result[region] = entry
        if not urls:
            entry["status"] = "brak adresu"
            continue
        d = OUT / "regiony" / slug
        texts, statuses, file_links = [], [], []
        for n, url in enumerate(urls, 1):
            if not allowed(url):
                statuses.append(f"{url}: zablokowane przez robots.txt")
                continue
            try:
                raw_b, _ = http_get(url, accept="text/html", timeout=75)
            except Exception as e:  # noqa: BLE001
                statuses.append(f"{url}: błąd pobierania: {e}")
                continue
            raw = raw_b.decode("utf-8", "replace")
            text = html_to_text(raw)
            texts.append(text)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"strona{n}.txt").write_text(text[:40_000], encoding="utf-8")
            entry["strony"].append({"url": url, "tekst": f"regiony/{slug}/strona{n}.txt"})
            file_links.extend(find_schedule_files(raw, url))
            time.sleep(1)
        if not texts:
            entry["status"] = "; ".join(statuses) or "brak danych"
            continue
        entry["status"] = "ok" if not statuses else "częściowo: " + "; ".join(statuses)
        digest = hashlib.sha256("\n".join(texts).encode()).hexdigest()
        entry["zmiana"] = prev.get(region, {}).get("sha") != digest
        entry["sha"] = digest
        old = {f.get("url"): f.get("sha") for f in prev.get(region, {}).get("pliki", [])}
        file_links.sort(key=rank_key, reverse=True)
        entry["pliki_kandydaci"] = [
            {"url": c["url"], "opis": c["opis"], "data": c["data"].isoformat() if c["data"] else None}
            for c in file_links[:6]
        ]
        for i, c in enumerate(file_links[:2], 1):
            furl, label, ext = c["url"], c["opis"], c["ext"]
            item = {"url": furl, "opis": label, "data_wg_strony": c["data"].isoformat() if c["data"] else None}
            entry["pliki"].append(item)
            if not allowed(furl):
                item["status"] = "zablokowane przez robots.txt"
                continue
            try:
                data, _ = http_get(furl, accept="*/*", timeout=90)
                txt, out_ext = file_to_text(data, ext)
                if txt is None:
                    item["status"] = f"format {ext} bez konwersji"
                    continue
                name = f"plik{i}{out_ext}"
                (d / name).write_text(txt[:MAX_FILE_TEXT], encoding="utf-8")
                item.update(status="ok", tekst=f"regiony/{slug}/{name}", sha=hashlib.sha256(data).hexdigest())
                item["zmiana"] = old.get(furl) != item["sha"]
            except Exception as e:  # noqa: BLE001
                item["status"] = f"błąd: {str(e)[:150]}"
    return result


# ---------- main ----------

def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    event = os.environ.get("GITHUB_EVENT_NAME", "manual")
    tomorrow = (now + dt.timedelta(hours=1) + dt.timedelta(days=1)).date()  # czas PL ~ UTC+1/2
    if event == "schedule" and tomorrow.day not in CONFIG["dni_zbierania"]:
        log(f"Pomijam: {tomorrow} nie jest dniem zbierania.")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("pominieto=true\n")
        return 0

    OUT.mkdir(exist_ok=True)
    state = load_state()
    # Stałe okno (domyślnie 35 dni) – ręczne uruchomienia między terminami niczego nie „zjadają”.
    since = today - dt.timedelta(days=CONFIG.get("dni_wstecz", 35))
    log(f"Okno: {since} – {today}")

    acts = collect_acts(since, today)
    log(f"Akty: {len(acts)}")
    term = current_term()
    prints = collect_prints(term, since)
    log(f"Druki: {len(prints)}")
    proc_nums = []
    for p in prints:
        proc_nums.extend(p.get("proces") or [re.sub(r"-[A-Z]+$", "", p["numer"])])
    seen = state.get("procesy_widziane", {})
    for n in proc_nums:
        seen.setdefault(n, today.isoformat())
    keep_from = today - dt.timedelta(days=CONFIG.get("procesy_dni", 120))
    seen = {n: d for n, d in seen.items() if parse_date(d) and parse_date(d) >= keep_from}
    order = sorted(seen, key=lambda n: seen[n], reverse=True)
    processes = collect_processes(term, order[:MAX_PROCESSES])
    log(f"Procesy: {len(processes)}")
    regions = collect_regions(state.get("regiony", {}))
    log("Regiony: " + ", ".join(f"{k}={v['status']}" for k, v in regions.items()))

    save_json("akty.json", acts)
    save_json("druki.json", prints)
    save_json("procesy.json", processes)
    index = {
        "wygenerowano": now.isoformat(timespec="seconds"),
        "okno_od": since.isoformat(),
        "okno_do": today.isoformat(),
        "kadencja_sejmu": term,
        "pliki": {"akty": "akty.json", "druki": "druki.json", "procesy": "procesy.json"},
        "liczby": {"akty": len(acts), "druki": len(prints), "procesy": len(processes)},
        "regiony": {k: {kk: vv for kk, vv in v.items() if kk != "sha"} for k, v in regions.items()},
        "bledy": errors,
        "uwaga": "Dane z oficjalnego API Sejmu (ELI) i stron programów regionalnych. Strony blokujące roboty (robots.txt) są pomijane.",
    }
    save_json("index.json", index)
    state = {
        "ostatnie_uruchomienie": today.isoformat(),
        "procesy_widziane": seen,
        "regiony": {k: {"sha": v.get("sha"), "pliki": [{"url": f["url"], "sha": f.get("sha")} for f in v["pliki"]]}
                    for k, v in regions.items()},
    }
    save_json("stan_kolektora.json", state)
    if errors:
        log("Błędy:\n- " + "\n- ".join(errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
