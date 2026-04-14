from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo
from datetime import datetime


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
SEEN_PATH = DATA_DIR / "seen_jobs.json"
MD_OUTPUT_PATH = OUTPUT_DIR / "latest_matches.md"
JSON_OUTPUT_PATH = OUTPUT_DIR / "latest_matches.json"

BASE_URL = "https://www.finn.no"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    )
}


@dataclass
class MatchResult:
    job_id: str
    title: str
    url: str
    score: int
    tier: str
    matched_include_all: list[str]
    matched_include_any: list[str]
    matched_education_any: list[str]
    excluded_hits: list[str]

def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}


def save_seen(path: Path, seen: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(dict(sorted(seen.items())), f, ensure_ascii=False, indent=2)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def find_hits(text: str, terms: list[str]) -> list[str]:
    normalized_text = normalize(text)
    hits: list[str] = []
    for term in terms:
        if normalize(term) in normalized_text:
            hits.append(term)
    return hits


def fetch(session: requests.Session, url: str) -> str:
    response = session.get(url, headers=HEADERS, timeout=30)

    if response.status_code == 403:
        raise RuntimeError(
            f"403 Forbidden fra nettstedet for URL: {url}. "
            "Dette tyder på at kilden blokkerer runneren eller den automatiserte trafikken."
        )

    response.raise_for_status()
    return response.text


def extract_search_results(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    results: dict[str, dict[str, str]] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = re.search(r"/job/ad/(\d+)", href)
        if not match:
            continue

        job_id = match.group(1)
        url = urljoin(BASE_URL, href)
        link_text = clean_text(a.get_text(" ", strip=True))

        if job_id not in results:
            results[job_id] = {
                "job_id": job_id,
                "url": url,
                "title_hint": link_text,
            }
        else:
            if len(link_text) > len(results[job_id]["title_hint"]):
                results[job_id]["title_hint"] = link_text

    return list(results.values())


def extract_ad_page_details(html: str, fallback_title: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")

    title = fallback_title
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = clean_text(og_title["content"])

    if not title:
        title_tag = soup.find("title")
        if title_tag:
            title = clean_text(title_tag.get_text(" ", strip=True))

    text = soup.get_text(" ", strip=True)
    return title, text


def evaluate_job(
    title: str,
    full_text: str,
    include_all: list[str],
    include_any: list[str],
    education_any: list[str],
    exclude_any: list[str],
) -> tuple[bool, MatchResult]:
    combined_text = f"{title}\n{full_text}"
    normalized_title = normalize(title)
    normalized_text = normalize(combined_text)

    matched_include_all = find_hits(combined_text, include_all)
    matched_include_any = find_hits(combined_text, include_any)
    matched_education_any = find_hits(combined_text, education_any)
    excluded_hits = find_hits(combined_text, exclude_any)

    title_hits = [term for term in include_any if normalize(term) in normalized_title]

    score = 0

    score += len(matched_include_all) * 3
    score += len(matched_include_any) * 2
    score += len(matched_education_any) * 3
    score += len(title_hits) * 3

    junior_terms = [
        "junior",
        "nyutdannet",
        "graduate",
        "trainee",
        "førstekonsulent",
        "konsulent",
        "koordinator",
    ]
    if any(term in normalized_text for term in junior_terms):
        score += 3

    senior_title_terms = [
        "senior",
        "head of",
        "director",
        "chief",
        "leder for",
        "seniorrådgiver",
    ]
    if any(term in normalized_title for term in senior_title_terms):
        score -= 6

    if excluded_hits:
        score -= len(excluded_hits) * 8

    if include_all and len(matched_include_all) != len(include_all):
        score -= 4

    role_hit_count = len(matched_include_any)
    education_hit_count = len(matched_education_any)

    # Må ha noe faktisk relevans
    passes = role_hit_count > 0

    # Avvis tydelig irrelevante eller for senior annonser
    if excluded_hits:
        passes = False

    if any(term in normalized_title for term in senior_title_terms):
        passes = False

    # Ranger i nivåer
    if score >= 12 and role_hit_count >= 2:
        tier = "Sterk match"
    elif score >= 6 and role_hit_count >= 1:
        tier = "Mulig match"
    else:
        tier = "Svak match"

    # Behold sterke og mulige matcher
    if tier == "Svak match":
        passes = False

    return passes, MatchResult(
        job_id="",
        title=title,
        url="",
        score=score,
        tier=tier,
        matched_include_all=matched_include_all,
        matched_include_any=matched_include_any,
        matched_education_any=matched_education_any,
        excluded_hits=excluded_hits,
    )


def write_outputs(
    matches: list[MatchResult],
    search_url: str,
    scanned_count: int,
    today: str,
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        f"# FINN-treff for {today}",
        "",
        f"Søk: {search_url}",
        "",
        f"Antall annonser skannet: {scanned_count}",
        f"Nye treff: {len(matches)}",
        "",
    ]

    if not matches:
        lines.append("Ingen nye annonser matchet kriteriene i dag.")
    else:
        for idx, match in enumerate(matches, start=1):
            lines.extend(
                [
                    f"## {idx}. [{match.title}]({match.url})",
                    "",
                    f"- FINN-ID: `{match.job_id}`",
                    f"- Rangering: `{match.tier}`",
                    f"- Score: `{match.score}`",
                    f"- Rolleord funnet: {', '.join(match.matched_include_any) if match.matched_include_any else 'Ingen'}",
                    f"- Utdanningsord funnet: {', '.join(match.matched_education_any) if match.matched_education_any else 'Ingen'}",
                    f"- Ekskluderingsord: {', '.join(match.excluded_hits) if match.excluded_hits else 'Ingen'}",
                    "",
                ]
            )

    MD_OUTPUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    JSON_OUTPUT_PATH.write_text(
        json.dumps([asdict(m) for m in matches], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    config = load_yaml(CONFIG_PATH)

    search_url = config["search_url"]
    include_all = config.get("include_all", []) or []
    include_any = config.get("include_any", []) or []
    education_any = config.get("education_any", []) or []
    exclude_any = config.get("exclude_any", []) or []
    max_results = int(config.get("max_results", 50))
    request_delay = float(config.get("request_delay_seconds", 1.0))

    today = datetime.now(ZoneInfo("Europe/Oslo")).date().isoformat()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    seen = load_seen(SEEN_PATH)
    session = requests.Session()

    try:
        search_html = fetch(session, search_url)
    except Exception as exc:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(ZoneInfo("Europe/Oslo")).date().isoformat()
        MD_OUTPUT_PATH.write_text(
            f"# FINN-treff for {today}\n\n"
            f"Kunne ikke hente søkeresultater.\n\n"
            f"Feil: {exc}\n",
            encoding="utf-8",
        )
        print(str(exc))
        raise

    candidates = extract_search_results(search_html)

    matches: list[MatchResult] = []
    scanned_count = 0

    for candidate in candidates[:max_results]:
        job_id = candidate["job_id"]

        if job_id in seen:
            continue

        scanned_count += 1

        try:
            ad_html = fetch(session, candidate["url"])
            title, full_text = extract_ad_page_details(
                ad_html,
                fallback_title=candidate.get("title_hint", ""),
            )
            passed, match = evaluate_job(
                title=title,
                full_text=full_text,
                include_all=include_all,
                include_any=include_any,
                education_any=education_any,
                exclude_any=exclude_any,
            )

            seen[job_id] = today

            if passed:
                match.job_id = job_id
                match.url = candidate["url"]
                matches.append(match)

        except Exception as exc:
            print(f"Kunne ikke lese annonse {candidate['url']}: {exc}")

        time.sleep(request_delay)

    matches.sort(key=lambda m: (-m.score, m.title.lower()))
    write_outputs(matches, search_url, scanned_count, today)
    save_seen(SEEN_PATH, seen)

    print(f"Ferdig. Skannet {scanned_count} nye annonser, fant {len(matches)} treff.")


if __name__ == "__main__":
    main()
