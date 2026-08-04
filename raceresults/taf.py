"""Import road-race results manually archived by TAF (Türkiye Atletizm
Federasyonu) at taf.org.tr/tum-sonuclar/, "YOL & ÖZEL YARIŞMALAR" tab.

Unlike every other provider in this package, TAF isn't one piece of
timing software with one data shape - it's a curated list of links to
whatever file each race's organizer uploaded (PDF, XLSX, XLS, ZIP, a
Google Sheet, sometimes just a news article with no result file at all).
So there's no single parser: each file format gets its own row-extractor,
and column roles (rank/bib/name/time/...) are guessed from whatever
header text that file happens to use, the same alias-matching approach
plustiming.py uses for its own per-race column variation.

Scope, deliberately: this only covers the "YOL & ÖZEL YARIŞMALAR" tab
(2018-2022 - TAF hasn't curated a newer edition of that list), and only
PDF/XLSX/XLS get parsed. ZIP archives, Google Sheets (pubhtml renders a JS
shell, not a plain table, without further work), and plain news-article
links are skipped - reported, not silently dropped.
"""
from __future__ import annotations

import io
import re
import subprocess
import tempfile
import zipfile
from typing import Dict, List, Optional, Tuple

import openpyxl
import requests

from .fetch import USER_AGENT
from .models import Course, Race, Runner, Split

BASE_URL = "https://taf.org.tr/tum-sonuclar/"
_ROAD_TAB_START = 'id="e-n-tab-content-2177315082"'
_ROAD_TAB_END = 'id="e-n-tab-content-2177315083"'

_YEAR_HEADER_RE = re.compile(r"<strong>(20\d\d) Yol Yarış[^<]*</strong>")
_STRONG_RE = re.compile(r"(<strong>[^<]+</strong>)")
_STRONG_TEXT_RE = re.compile(r"<strong>([^<]+)</strong>")
_LINK_RE = re.compile(r'<a href="([^"]+)"[^>]*>([^<]*)</a>')

_TIME_RE = re.compile(r"^(\d{1,3}):(\d{2}):(\d{2})(?:[.,](\d+))?$")

_ROLE_ALIASES = {
    "rank": ["sira", "sıra", "derece sirasi", "genel siralama", "pos", "pl", "plac", "place", "rank"],
    "bib": ["bib no", "g.no", "gno", "dosya no", "bib", "no", "race no"],
    "name": ["ad soyad", "adi soyadi", "ad ve soyad", "isim", "ad", "name"],
    "surname": ["soyad", "soyadi"],
    "birth_year": ["d.yili", "dyili", "dogum yili", "dogum tarihi", "yas", "yob", "nat"],
    "category": ["kat", "kategori", "category", "categ", "ag"],
    "cat_rank": [
        "kat. gore", "kat gore", "kategori sirasi", "kategori derecesi",
        "net categ pos", "cat pos", "category pos",
    ],
    "gender": ["cinsiyet", "gender", "gend", "sex"],
    "club": ["il", "kulup", "takim", "club", "affiliation", "team"],
    "time": [
        "zaman", "sure", "derece", "finish", "net time", "gun time", "gunti",
        "guntime", "nettime", "result", "time", "finish time", "chip time",
    ],
}
# Truncated PDF headers (column too narrow, text clipped) sometimes don't
# exactly equal an alias - "GunTi" for "Gun Time". Checked as a fallback,
# with whitespace stripped from both sides, after exact matching fails.



def _fold(s: str) -> str:
    return (
        s.lower()
        .replace("ı", "i")
        .replace("ğ", "g")
        .replace("ş", "s")
        .replace("ç", "c")
        .replace("ö", "o")
        .replace("ü", "u")
        .strip()
    )


def _parse_time(value: str) -> Optional[float]:
    if not value:
        return None
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    h, m, s, frac = match.groups()
    total = int(h) * 3600 + int(m) * 60 + int(s)
    if frac:
        total += int(frac) / (10 ** len(frac))
    return total


def fetch_road_race_entries(timeout: int = 30) -> List[Dict]:
    """Scrape the "YOL & ÖZEL YARIŞMALAR" tab into
    [{"year": "2022", "name": "...", "files": [{"label":..., "url":...}]}, ...],
    deduped ((year, name)) - the site's own 2018 tab is a verbatim repeat of 2019.
    """
    resp = requests.get(BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    html = resp.text
    i = html.find(_ROAD_TAB_START)
    j = html.find(_ROAD_TAB_END)
    section = html[i:j] if i != -1 and j != -1 else ""

    year_blocks = _YEAR_HEADER_RE.split(section)
    races: List[Dict] = []
    for k in range(1, len(year_blocks), 2):
        year = year_blocks[k]
        block = year_blocks[k + 1]
        current = None
        for part in _STRONG_RE.split(block):
            match = _STRONG_TEXT_RE.match(part)
            if match:
                name = re.sub(r"&#8211;|&#8217;", "-", match.group(1)).strip()
                if not name or name == "SONUÇLAR":
                    current = None
                    continue
                current = {"year": year, "name": name, "files": []}
                races.append(current)
            elif current is not None:
                for href, text in _LINK_RE.findall(part):
                    url = href.replace("www.taf.org.tr", "taf.org.tr").replace("&amp;", "&")
                    current["files"].append({"label": text.strip(), "url": url})

    seen = set()
    unique = []
    for r in races:
        key = (r["year"], r["name"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


def classify_file(url: str) -> str:
    lower = url.lower()
    if "taf.org.tr/" in lower and "/wp-content/uploads/" not in lower and not re.search(r"\.\w{2,4}$", lower):
        return "article"  # a news post, not a result file
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith(".xlsx"):
        return "xlsx"
    if lower.endswith(".xls"):
        return "xls"
    if lower.endswith(".zip"):
        return "zip"
    if "docs.google.com/spreadsheets" in lower:
        return "gsheet"
    if lower.endswith(".docx"):
        return "docx"
    return "unknown"


def _download(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _rows_from_xlsx(data: bytes) -> List[List[str]]:
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.worksheets[0]
    return [["" if c is None else str(c) for c in row] for row in ws.iter_rows(values_only=True)]


def _rows_from_xls(data: bytes) -> List[List[str]]:
    import pandas as pd

    df = pd.read_excel(io.BytesIO(data), engine="xlrd", header=None)
    return df.fillna("").astype(str).values.tolist()


_TOKEN_RE = re.compile(r"\S(?:(?!\s{2,}).)*")


def _rows_from_pdf(data: bytes) -> List[List[str]]:
    """pdftotext -layout keeps character alignment across lines, so once we
    know which character offset each header word starts at, every data row
    (including ones with a blank cell) can be sliced at those same fixed
    offsets - unlike splitting each row on whitespace runs independently,
    which silently shifts every later column when an earlier cell is empty.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        result = subprocess.run(
            ["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True, timeout=60
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]

    col_starts: Optional[List[int]] = None
    rows: List[List[str]] = []
    for line in lines:
        tokens = list(_TOKEN_RE.finditer(line))
        if col_starts is None:
            folded = [_fold(t.group()) for t in tokens]
            if any(cell in aliases for cell in folded for aliases in _ROLE_ALIASES.values()):
                col_starts = [t.start() for t in tokens]
                rows.append([t.group().strip() for t in tokens])
            continue
        bounds = col_starts + [len(line) + 1]
        rows.append([line[bounds[i] : bounds[i + 1]].strip() for i in range(len(col_starts))])
    return rows


def _rows_from_zip(data: bytes) -> List[List[str]]:
    """Extract the first parseable (xlsx/xls/pdf) member and parse that."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            fmt = classify_file(name)
            if fmt in ("xlsx", "xls", "pdf"):
                member_data = zf.read(name)
                return _ROW_EXTRACTORS[fmt](member_data)
    raise ValueError("No parseable file found inside zip")


_ROW_EXTRACTORS = {"xlsx": _rows_from_xlsx, "xls": _rows_from_xls, "pdf": _rows_from_pdf, "zip": _rows_from_zip}


def _find_header(rows: List[List[str]]) -> Tuple[int, Dict[str, int]]:
    """Find the row that looks like a header and map role -> column index."""
    for row_idx, row in enumerate(rows[:15]):
        folded = [_fold(str(c)) for c in row]
        compact = [c.replace(" ", "").replace(".", "") for c in folded]
        role_idx: Dict[str, int] = {}
        for role, aliases in _ROLE_ALIASES.items():
            compact_aliases = [a.replace(" ", "").replace(".", "") for a in aliases]
            for col_idx, cell in enumerate(folded):
                if cell in aliases:
                    role_idx[role] = col_idx
                    break
            else:
                # Fallback for truncated PDF headers ("GunTi" for "Gun
                # Time"): compare with spaces/periods stripped, either
                # direction being a prefix of the other.
                for col_idx, cell in enumerate(compact):
                    if cell and any(cell.startswith(a) or a.startswith(cell) for a in compact_aliases if a):
                        role_idx[role] = col_idx
                        break
        if "name" in role_idx or ("surname" in role_idx and "bib" in role_idx):
            return row_idx, role_idx
    raise ValueError("No header row recognized")


def rows_to_runners(rows: List[List[str]], course_code: str) -> List[Runner]:
    header_idx, role_idx = _find_header(rows)

    def cell(row: List[str], role: str) -> str:
        idx = role_idx.get(role)
        if idx is None or idx >= len(row):
            return ""
        return str(row[idx]).strip()

    runners = []
    for row in rows[header_idx + 1 :]:
        if not row or all(not str(c).strip() for c in row):
            continue
        name = cell(row, "name")
        surname = cell(row, "surname")
        full_name = f"{name} {surname}".strip() if surname else name
        full_name = " ".join(full_name.replace("\xa0", " ").split())
        if not full_name:
            continue

        bib = cell(row, "bib")
        if bib and not bib.isdigit():
            # PDF page headers/footers ("services by DeparTiming, www...")
            # repeat on every page and sometimes land on a column boundary
            # that makes them look like a data row. A real result always
            # has a numeric bib (or none at all), so a non-numeric one here
            # means this is page furniture, not a runner.
            continue

        time_text = cell(row, "time")
        finish_seconds = _parse_time(time_text)
        status = "finished" if finish_seconds is not None else "no_result"

        gender_raw = _fold(cell(row, "gender"))
        sex = {"erkek": "M", "kadin": "F", "e": "M", "k": "F"}.get(gender_raw)

        birth_year_text = cell(row, "birth_year")
        birth_year = None
        digits = re.search(r"(\d{4})", birth_year_text)
        if digits:
            birth_year = int(digits.group(1))

        rank_text = cell(row, "rank").rstrip(".")
        cat_rank_text = cell(row, "cat_rank").rstrip(".")

        runners.append(
            Runner(
                bib=cell(row, "bib"),
                name=full_name,
                club=cell(row, "club") or None,
                birth_year=birth_year,
                sex=sex,
                category=cell(row, "category") or None,
                course_code=course_code,
                nationality=None,
                start_time_text=None,
                status=status,
                finish_time_text=time_text or None,
                finish_seconds=finish_seconds,
                pace=None,
                finish_clock_text=None,
                gap_text=None,
                gap_seconds=None,
                splits=[],
                rank_course=int(rank_text) if rank_text.isdigit() else None,
                rank_category=int(cat_rank_text) if cat_rank_text.isdigit() else None,
                rank_gender=None,
            )
        )
    return runners


_MONTHS = {
    "ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "mayis": 5,
    "haziran": 6, "temmuz": 7, "ağustos": 8, "agustos": 8, "eylül": 9, "eylul": 9,
    "ekim": 10, "kasım": 11, "kasim": 11, "aralık": 12, "aralik": 12,
}
_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)\s+(20\d\d)")


def _parse_turkish_date(text: str) -> Optional[str]:
    match = _DATE_RE.search(text)
    if not match:
        return None
    day, month_name, year = match.groups()
    month = _MONTHS.get(month_name.lower())
    if not month:
        return None
    return f"{year}-{month:02d}-{int(day):02d}"


def scrape_taf_race(entry: Dict, slug: str, timeout: int = 30) -> Tuple[Optional[Race], List[str]]:
    """Build a Race from a fetch_road_race_entries() entry.

    Returns (race_or_None, notes) - notes lists any files that were skipped
    or failed, so callers can report partial success instead of an opaque
    all-or-nothing failure.
    """
    notes: List[str] = []
    courses: List[Course] = []
    all_runners: List[Runner] = []
    seen_course_codes = set()

    for file in entry["files"]:
        fmt = classify_file(file["url"])
        if fmt in ("article", "gsheet", "docx", "unknown"):
            notes.append(f"skipped ({fmt}): {file['label']} {file['url']}")
            continue
        try:
            data = _download(file["url"], timeout=timeout)
            rows = _ROW_EXTRACTORS[fmt](data)
            course_code = file["label"] or fmt.upper()
            runners = rows_to_runners(rows, course_code)
            if not runners:
                notes.append(f"empty after parsing: {file['label']} {file['url']}")
                continue
            if course_code not in seen_course_codes:
                seen_course_codes.add(course_code)
                courses.append(Course(code=course_code, distance_m=0))
            all_runners.extend(runners)
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't sink the whole race
            notes.append(f"failed ({fmt}): {file['label']} {file['url']} - {exc}")

    if not all_runners:
        return None, notes

    race = Race(
        slug=slug,
        name=re.sub(r",?\s*\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü]+\s+20\d\d.*$", "", entry["name"]).strip(", -"),
        organizer="TAF",
        date=_parse_turkish_date(entry["name"]) or f"{entry['year']}-01-01",
        source_url=BASE_URL,
        courses=courses,
        checkpoints=[],
        categories=[],
        runners=all_runners,
    )
    return race, notes
