"""Literature review agent: OpenAlex search -> recent-years + venue filter -> Top N."""

from __future__ import annotations  # allow `str | None` syntax on Python 3.9

import csv
import json
import math
import os
import re
import sys
from pathlib import Path

import openreview
import pyalex
from pyalex import Works

pyalex.config.email = "safeai.snu@gmail.com"  # OpenAlex polite pool


def _load_env(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a local .env file into os.environ (no dependency).

    Used for OpenReview credentials so they live in a gitignored file, not the shell.
    """
    p = Path(__file__).parent / path
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


CURRENT_YEAR = 2026
RECENT_YEARS = 3
CANDIDATES = 100
TOP_N = 50

VENUE_TIERS = {
    # Tier 1
    "neurips": 3, "neural information processing systems": 3,
    "icml": 3, "international conference on machine learning": 3,
    "iclr": 3, "international conference on learning representations": 3,
    # Tier 2
    "aaai": 2, "ijcai": 2, "acl": 2, "emnlp": 2, "naacl": 2, "eacl": 2,
    "cvpr": 2, "iccv": 2, "eccv": 2,
    # Tier 3
    "tmlr": 1, "jmlr": 1, "nature machine intelligence": 1,
}

# Infer venue from DOI pattern when OpenAlex venue data is missing.
DOI_VENUE_PATTERNS = [
    (r"\.acl-",   "ACL"),
    (r"\.emnlp",  "EMNLP"),
    (r"\.naacl",  "NAACL"),
    (r"\.eacl",   "EACL"),
    (r"\.findings-acl",   "ACL Findings"),
    (r"\.findings-emnlp", "EMNLP Findings"),
]

# location source.type priority (real venue > preprint repository)
TYPE_PRIORITY = {"conference": 3, "journal": 2, "book series": 1, "repository": 0}

# Self-publishing / preprint repositories — not counted as real venues.
REPOSITORY_HINTS = (
    "arxiv", "zenodo", "ssrn", "researchgate", "biorxiv", "medrxiv",
    "preprints.org", "hal", "osf", "techrxiv", "authorea", "research square",
)


def is_repository(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in REPOSITORY_HINTS)


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


# ---------------------------------------------------------------------------
# Venue extraction / scoring
# ---------------------------------------------------------------------------
def extract_venue(work: dict) -> str | None:
    """Pick the source with the highest type priority; fall back to DOI pattern."""
    best, best_p = None, -1
    for loc in work.get("locations") or []:
        src = loc.get("source") or {}
        name = src.get("display_name")
        if not name or is_repository(name):
            continue
        p = TYPE_PRIORITY.get(src.get("type"), 0)
        if p > best_p:
            best, best_p = name, p
    if best:
        return best

    doi = (work.get("doi") or "").lower()
    for pattern, venue in DOI_VENUE_PATTERNS:
        if re.search(pattern, doi):
            return venue
    return None


def venue_score(venue: str | None) -> float:
    if not venue:
        return 0.3  # no venue = preprint
    v = venue.lower()
    for key, score in VENUE_TIERS.items():
        if key in v:
            return score
    return 1.0  # recognized venue outside the tier list


def arxiv_url(work: dict) -> str | None:
    for loc in work.get("locations") or []:
        url = loc.get("landing_page_url") or ""
        if "arxiv.org" in url:
            return url
    return None


# ---------------------------------------------------------------------------
# 1. OpenAlex search -> candidates
# ---------------------------------------------------------------------------
def search_openalex(query: str, n: int = CANDIDATES) -> list[dict]:
    start = f"{CURRENT_YEAR - RECENT_YEARS + 1}-01-01"
    works = (
        Works()
        .search(query)
        .filter(from_publication_date=start)
        .sort(relevance_score="desc")
        .get(per_page=min(n, 200))
    )
    papers = []
    for rank, w in enumerate(works):
        venue = extract_venue(w)
        papers.append({
            "title": (w.get("title") or "").strip(),
            "authors": [
                a["author"]["display_name"] for a in w.get("authorships", [])
            ],
            "year": w.get("publication_year") or CURRENT_YEAR,
            "venue": venue,
            "venue_score": venue_score(venue),
            "citations": w.get("cited_by_count", 0),
            "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
            "url": w.get("id"),
            "arxiv_url": arxiv_url(w),
            "grade": None,
            "source": "openalex",
            "relevance": max(0.0, 1.0 - rank / n),
        })
    return papers


# ---------------------------------------------------------------------------
# 2. OpenReview — accepted ICLR / NeurIPS / ICML papers (Tier 1 supplement)
# ---------------------------------------------------------------------------
OPENREVIEW_FAMILIES = ["ICLR.cc", "NeurIPS.cc", "ICML.cc"]
CACHE_DIR = Path(".cache/openreview")
GRADE_KEYWORDS = [("oral", "oral"), ("spotlight", "spotlight"),
                  ("notable", "spotlight"), ("poster", "poster")]


def parse_grade(venue_str: str) -> str | None:
    s = (venue_str or "").lower()
    for kw, grade in GRADE_KEYWORDS:
        if kw in s:
            return grade
    return None


def _openreview_venue_ids() -> list[str]:
    years = range(CURRENT_YEAR - RECENT_YEARS + 1, CURRENT_YEAR + 1)
    return [f"{fam}/{y}/Conference" for y in years for fam in OPENREVIEW_FAMILIES]


def _val(content: dict, key: str):
    x = content.get(key)
    return x.get("value") if isinstance(x, dict) else x


def _fetch_venue(client, venue_id: str) -> list[dict]:
    """Fetch and cache all accepted papers of a venue.

    Raises on API errors (e.g. the anonymous-access challenge) so the caller can
    report them instead of silently returning an empty result.
    """
    cache = CACHE_DIR / (venue_id.replace("/", "_") + ".json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    notes = client.get_all_notes(content={"venueid": venue_id})
    papers = []
    for n in notes:
        title = _val(n.content, "title")
        if not title:
            continue
        papers.append({
            "title": title,
            "authors": _val(n.content, "authors") or [],
            "abstract": _val(n.content, "abstract") or "",
            "venue_str": _val(n.content, "venue") or venue_id,
            "forum": n.forum,
        })
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(papers, ensure_ascii=False), encoding="utf-8")
    return papers


def search_openreview(query: str) -> list[dict]:
    # OpenReview now gates anonymous bulk access behind an anti-bot challenge.
    # Logging in with an account bypasses it; set these to enable the supplement.
    user = os.environ.get("OPENREVIEW_USERNAME")
    pw = os.environ.get("OPENREVIEW_PASSWORD")
    try:
        client = openreview.api.OpenReviewClient(
            baseurl="https://api2.openreview.net", username=user, password=pw,
        )
    except Exception as e:
        print(f"  [warn] OpenReview connection failed: {e}")
        return []

    words = [w for w in normalize(query).split() if w]
    results = []
    for vid in _openreview_venue_ids():
        try:
            venue_papers = _fetch_venue(client, vid)
        except Exception as e:
            # A challenge/403 will hit every venue the same way — stop and explain.
            if "Challenge" in str(e) and not user:
                print("  [warn] OpenReview anonymous access is blocked (anti-bot challenge).")
                print("         Set OPENREVIEW_USERNAME / OPENREVIEW_PASSWORD to enable the")
                print("         ICLR/NeurIPS/ICML supplement. Using OpenAlex results only.")
            else:
                print(f"  [warn] OpenReview fetch failed ({vid}): {str(e)[:100]}")
            return results
        if not venue_papers:
            continue
        year = int(re.search(r"\d{4}", vid).group())
        matched = 0
        for p in venue_papers:
            text = normalize(p["title"] + " " + p["abstract"])
            if words and not all(w in text for w in words):
                continue
            matched += 1
            in_title = all(w in normalize(p["title"]) for w in words)
            results.append({
                "title": p["title"].strip(),
                "authors": p["authors"],
                "year": year,
                "venue": p["venue_str"],
                "venue_score": venue_score(p["venue_str"]),
                "citations": None,
                "doi": None,
                "url": f"https://openreview.net/forum?id={p['forum']}",
                "arxiv_url": None,
                "grade": parse_grade(p["venue_str"]),
                "relevance": 1.0 if in_title else 0.7,  # title match ranks higher
                "source": "openreview",
            })
        if matched:
            print(f"      {vid}: {matched} matched")
    return results


# ---------------------------------------------------------------------------
# 3. Merge sources + dedupe by title (venue/grade: best-of; citations/DOI: OpenAlex)
# ---------------------------------------------------------------------------
def merge_sources(*lists: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for papers in lists:
        for p in papers:
            key = normalize(p["title"])
            if not key:
                continue
            m = merged.get(key)
            if m is None:
                merged[key] = dict(p)
                continue
            # Fill empty fields from the other source.
            if m["citations"] is None and p["citations"] is not None:
                m["citations"] = p["citations"]
            for f in ("doi", "arxiv_url", "grade"):
                if not m.get(f) and p.get(f):
                    m[f] = p[f]
            # Keep the higher-tier venue (usually OpenReview).
            if p["venue_score"] > m["venue_score"]:
                m["venue"], m["venue_score"] = p["venue"], p["venue_score"]
            m["relevance"] = max(m["relevance"], p["relevance"])
            if len(p["authors"]) > len(m["authors"]):
                m["authors"] = p["authors"]
    return list(merged.values())


# ---------------------------------------------------------------------------
# 4. Scoring -> Top N
# ---------------------------------------------------------------------------
GRADE_BONUS = {"oral": 1.0, "spotlight": 0.6, "poster": 0.3}


def selection_score(p: dict) -> float:
    relevance = p["relevance"]
    venue = p["venue_score"] / 3.0
    recency = max(0.0, 1.0 - (CURRENT_YEAR - p["year"]) / RECENT_YEARS)
    # Log-scaled citations to dampen outliers / accumulation bias (~1000 = full).
    impact = min(1.0, math.log10((p["citations"] or 0) + 1) / 3.0)
    grade = GRADE_BONUS.get(p["grade"], 0.0)
    return (0.22 * relevance + 0.35 * venue + 0.18 * recency
            + 0.13 * impact + 0.12 * grade)


def apa_citation(p: dict) -> str:
    authors = p["authors"]
    if not authors:
        author_str = "Unknown"
    elif len(authors) > 3:
        author_str = f"{authors[0]}, et al."
    else:
        author_str = ", ".join(authors)
    venue = p["venue"] or "arXiv preprint"
    doi = f" https://doi.org/{p['doi']}" if p["doi"] else ""
    return f"{author_str} ({p['year']}). {p['title']}. {venue}.{doi}"


# ---------------------------------------------------------------------------
# Output — CSV (filename slugged from the query)
# ---------------------------------------------------------------------------
CSV_COLUMNS = [
    "select", "no", "year", "venue", "title", "authors",
    "citations", "doi", "url", "arxiv_url", "score", "apa",
]


def save_results(query: str, papers: list[dict], out_dir: str = "results") -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_") or "query"
    path = Path(out_dir) / f"{slug}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig keeps Unicode readable in Excel.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for i, p in enumerate(papers, 1):
            writer.writerow({
                "select": "",
                "no": i,
                "title": p["title"],
                "authors": "; ".join(p["authors"]),
                "year": p["year"],
                "venue": p["venue"] or "Not Available",
                "citations": "" if p["citations"] is None else p["citations"],
                "doi": p["doi"] or "",
                "url": p["url"] or "",
                "arxiv_url": p["arxiv_url"] or "",
                "score": round(p["score"], 3),
                "apa": apa_citation(p),
            })
    return path


# ---------------------------------------------------------------------------
# Output — Markdown table page (cards/searches/ -> "Searches" tab)
# ---------------------------------------------------------------------------
def save_search_page(query: str, papers: list[dict],
                     out_dir: str = "cards/searches") -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_") or "query"
    path = Path(out_dir) / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    def cell(text: str) -> str:  # escape pipes inside table cells
        return str(text).replace("|", "\\|")

    rows = [
        f"# {query}",
        "",
        f"**{len(papers)} papers** · last {RECENT_YEARS} years · ranked by relevance · venue · recency · impact",
        "",
        "| # | Year | Venue | Title |",
        "|:-:|:----:|-------|-------|",
    ]
    for i, p in enumerate(papers, 1):
        link = p["url"] or p["arxiv_url"]
        title = f"[{cell(p['title'])}]({link})" if link else cell(p["title"])
        venue = cell(p["venue"] or "N/A")
        rows.append(f"| {i} | {p['year']} | {venue} | {title} |")

    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def main():
    _load_env()  # pick up OpenReview credentials from a local .env file, if present
    # Query can come from the command line (scriptable) or an interactive prompt.
    query = " ".join(sys.argv[1:]).strip() or input("Query: ").strip()
    if not query:
        print("A query is required.")
        return

    print(f"\n[1/4] OpenAlex candidates ({CANDIDATES}, last {RECENT_YEARS} years)...")
    oa = search_openalex(query)
    print(f"      {len(oa)} collected")

    print("[2/4] OpenReview (accepted ICLR / NeurIPS / ICML)...")
    orv = search_openreview(query)
    print(f"      {len(orv)} matched")

    print("[3/4] Merge sources + dedupe...")
    papers = merge_sources(oa, orv)
    print(f"      {len(papers)} after merge")

    print(f"[4/4] Scoring -> Top {TOP_N}...\n")
    for p in papers:
        p["score"] = selection_score(p)
    # Select Top N by score, then display ordered by year / venue tier / score.
    top = sorted(papers, key=lambda p: p["score"], reverse=True)[:TOP_N]
    top.sort(key=lambda p: (p["year"], p["venue_score"], p["score"]), reverse=True)

    for i, p in enumerate(top, 1):
        cites = "N/A" if p["citations"] is None else p["citations"]
        print("=" * 60)
        print(f"[{i}] {p['year']} | {p['venue'] or 'Not Available'}")
        print(f"    Title    : {p['title']}")
        print(f"    Citations: {cites}")
        print(f"    DOI      : {p['doi'] or 'Not Available'}")
        if p["arxiv_url"]:
            print(f"    arXiv    : {p['arxiv_url']}")
        print(f"    Score    : {p['score']:.3f}")
        print(f"    APA      : {apa_citation(p)}")

    path = save_results(query, top)
    page = save_search_page(query, top)
    print("\n" + "=" * 60)
    print(f"Saved: {path}  ({len(top)} papers)")
    print(f"Site page: {page}  (mkdocs 'Searches' tab)")


if __name__ == "__main__":
    main()
