"""Literature Review Agent — OpenAlex 1차 검색 → 최근 3년 + Venue 필터 → Top 10.

파이프라인
    키워드
      ↓ OpenAlex 검색 (관련성 순 후보 50편)
      ↓ 최근 3년 필터 (Search Scope)
      ↓ Venue / Citation / DOI 파싱  (arXiv 무료 PDF 링크는 보조)
      ↓ 중복 제거 (preprint ↔ 출판본)
      ↓ 점수화 (관련성 · Venue 수준 · 최신성 · 영향력)
      ↓ Top 10 선정
"""

import csv
import json
import math
import re
from pathlib import Path

import openreview
import pyalex
from pyalex import Works

pyalex.config.email = "safeai.snu@gmail.com"  # OpenAlex polite pool

CURRENT_YEAR = 2026
RECENT_YEARS = 3   # 최근 3년 (Search Scope)
CANDIDATES = 100   # OpenAlex 후보 수
TOP_N = 50         # 최종 선정 수

# AGENTS.md 의 Preferred Sources Tier → 점수
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

# OpenAlex 에 venue 데이터가 비어도 DOI 패턴으로 venue 를 추론한다.
DOI_VENUE_PATTERNS = [
    (r"\.acl-",   "ACL"),
    (r"\.emnlp",  "EMNLP"),
    (r"\.naacl",  "NAACL"),
    (r"\.eacl",   "EACL"),
    (r"\.findings-acl",   "ACL Findings"),
    (r"\.findings-emnlp", "EMNLP Findings"),
]

# location source.type 우선순위 (실제 venue > preprint repository)
TYPE_PRIORITY = {"conference": 3, "journal": 2, "book series": 1, "repository": 0}

# 셀프 출판 / preprint 저장소 — 실제 venue 로 인정하지 않는다.
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
# Venue 추출 / 점수
# ---------------------------------------------------------------------------
def extract_venue(work: dict) -> str | None:
    """모든 location 을 훑어 type 우선순위가 가장 높은 source 이름을 고른다.
    source 데이터가 비어 있으면 DOI 패턴으로 추론한다."""
    best, best_p = None, -1
    for loc in work.get("locations") or []:
        src = loc.get("source") or {}
        name = src.get("display_name")
        if not name or is_repository(name):   # repository 는 venue 후보에서 제외
            continue
        p = TYPE_PRIORITY.get(src.get("type"), 0)
        if p > best_p:
            best, best_p = name, p
    if best:
        return best

    # 실제 venue 가 없으면 DOI 패턴으로 추론 (없으면 preprint → None)
    doi = (work.get("doi") or "").lower()
    for pattern, venue in DOI_VENUE_PATTERNS:
        if re.search(pattern, doi):
            return venue
    return None


def venue_score(venue: str | None) -> float:
    if not venue:
        return 0.3  # venue 없음 = preprint
    v = venue.lower()
    for key, score in VENUE_TIERS.items():
        if key in v:
            return score
    return 1.0  # 인정되는 venue 이나 tier 목록 밖 (conference/journal)


def arxiv_url(work: dict) -> str | None:
    for loc in work.get("locations") or []:
        url = loc.get("landing_page_url") or ""
        if "arxiv.org" in url:
            return url
    return None


# ---------------------------------------------------------------------------
# 1. OpenAlex 검색 → 후보
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
            "url": w.get("id"),                 # OpenAlex landing
            "arxiv_url": arxiv_url(w),          # 무료 PDF (보조)
            "grade": None,                      # OpenReview 합격등급 (있으면 병합 시 채움)
            "source": "openalex",
            "relevance": max(0.0, 1.0 - rank / n),   # 검색 관련성 순위 → 0~1
        })
    return papers


# ---------------------------------------------------------------------------
# 2. OpenReview — ICLR / NeurIPS 합격작 (Tier 1 보조 소스)
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
    """venue 의 합격작 전체를 받아 캐시. (제목/저자/초록/등급/forum)"""
    cache = CACHE_DIR / (venue_id.replace("/", "_") + ".json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        notes = client.get_all_notes(content={"venueid": venue_id})
    except Exception:
        return []
    papers = []
    for n in notes:
        title = _val(n.content, "title")
        if not title:
            continue
        papers.append({
            "title": title,
            "authors": _val(n.content, "authors") or [],
            "abstract": _val(n.content, "abstract") or "",
            "venue_str": _val(n.content, "venue") or venue_id,   # "ICLR 2024 oral"
            "forum": n.forum,
        })
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(papers, ensure_ascii=False), encoding="utf-8")
    return papers


def search_openreview(query: str) -> list[dict]:
    try:
        client = openreview.api.OpenReviewClient(baseurl="https://api2.openreview.net")
    except Exception as e:
        print(f"  [warn] OpenReview 연결 실패: {e}")
        return []

    words = [w for w in normalize(query).split() if w]
    results = []
    for vid in _openreview_venue_ids():
        venue_papers = _fetch_venue(client, vid)
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
                "venue": p["venue_str"],                 # "ICLR 2024 oral"
                "venue_score": venue_score(p["venue_str"]),
                "citations": None,                       # OpenAlex 병합 시 채움
                "doi": None,
                "url": f"https://openreview.net/forum?id={p['forum']}",
                "arxiv_url": None,
                "grade": parse_grade(p["venue_str"]),
                "source": "openreview",
                "relevance": 1.0 if in_title else 0.7,   # 제목 매칭이면 관련성↑
            })
        if matched:
            print(f"      {vid}: {matched}편 매칭")
    return results


# ---------------------------------------------------------------------------
# 3. 소스 병합 + 중복 제거 (제목 기준) — venue/등급은 OR, 인용/DOI는 OpenAlex
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
            # 인용수 / DOI / arXiv 링크 / 등급 — 비어있는 쪽을 채운다.
            if m["citations"] is None and p["citations"] is not None:
                m["citations"] = p["citations"]
            for f in ("doi", "arxiv_url", "grade"):
                if not m.get(f) and p.get(f):
                    m[f] = p[f]
            # venue — tier 높은 쪽 우선 (보통 OpenReview)
            if p["venue_score"] > m["venue_score"]:
                m["venue"], m["venue_score"] = p["venue"], p["venue_score"]
            m["relevance"] = max(m["relevance"], p["relevance"])
            if len(p["authors"]) > len(m["authors"]):
                m["authors"] = p["authors"]
    return list(merged.values())


# ---------------------------------------------------------------------------
# 4. 점수화 → Top N
# ---------------------------------------------------------------------------
GRADE_BONUS = {"oral": 1.0, "spotlight": 0.6, "poster": 0.3}


def selection_score(p: dict) -> float:
    relevance = p["relevance"]
    venue = p["venue_score"] / 3.0                              # 0 ~ 1
    recency = max(0.0, 1.0 - (CURRENT_YEAR - p["year"]) / RECENT_YEARS)
    # 인용수는 log 스케일 — 데이터 이상치 / 누적 편향 완화 (1000회 ≈ 만점)
    impact = min(1.0, math.log10((p["citations"] or 0) + 1) / 3.0)
    grade = GRADE_BONUS.get(p["grade"], 0.0)                    # 합격등급 보너스
    # 2026(최신) 우선 → recency 비중 유지, 합격등급 신호 추가
    return (0.22 * relevance + 0.35 * venue + 0.18 * recency
            + 0.13 * impact + 0.12 * grade)


# ---------------------------------------------------------------------------
# 4. APA 7th
# ---------------------------------------------------------------------------
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
# 결과 저장 — 파일명에 검색 키워드 포함
# ---------------------------------------------------------------------------
CSV_COLUMNS = [
    "select", "no", "year", "venue", "grade", "title", "authors",
    "citations", "doi", "url", "arxiv_url", "score", "apa",
]


def save_results(query: str, papers: list[dict], out_dir: str = "results") -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_") or "query"
    path = Path(out_dir) / f"{slug}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig → Excel 에서 한글/유니코드 깨짐 방지
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for i, p in enumerate(papers, 1):
            writer.writerow({
                "select": "",                              # 선별용 빈 칸 (✓ 표시)
                "no": i,
                "title": p["title"],
                "authors": "; ".join(p["authors"]),
                "year": p["year"],
                "venue": p["venue"] or "Not Available",
                "grade": p["grade"] or "",
                "citations": "" if p["citations"] is None else p["citations"],
                "doi": p["doi"] or "",
                "url": p["url"] or "",
                "arxiv_url": p["arxiv_url"] or "",
                "score": round(p["score"], 3),
                "apa": apa_citation(p),
            })
    return path


# ---------------------------------------------------------------------------
# 사이트용 — 검색 결과를 Markdown 표 페이지로 (cards/searches/ → "Searches" 탭)
# ---------------------------------------------------------------------------
def save_search_page(query: str, papers: list[dict],
                     out_dir: str = "cards/searches") -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_") or "query"
    path = Path(out_dir) / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    def cell(text: str) -> str:                       # 표 안 파이프 깨짐 방지
        return str(text).replace("|", "\\|")

    rows = [f"# {query}", "",
            "| # | Year | Venue | Title |",
            "|---|------|-------|-------|"]
    for i, p in enumerate(papers, 1):
        link = p["url"] or p["arxiv_url"]
        title = f"[{cell(p['title'])}]({link})" if link else cell(p["title"])
        grade = f" ({p['grade']})" if p["grade"] else ""
        venue = cell((p["venue"] or "N/A") + grade)
        rows.append(f"| {i} | {p['year']} | {venue} | {title} |")

    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def main():
    query = input("검색어: ").strip()

    print(f"\n[1/4] OpenAlex 후보 {CANDIDATES}편 검색 (최근 {RECENT_YEARS}년)...")
    oa = search_openalex(query)
    print(f"      {len(oa)}편 수집")

    print("[2/4] OpenReview 검색 (ICLR / NeurIPS 합격작)...")
    orv = search_openreview(query)
    print(f"      {len(orv)}편 매칭")

    print("[3/4] 소스 병합 + 중복 제거...")
    papers = merge_sources(oa, orv)
    print(f"      {len(papers)}편 (병합 후)")

    print(f"[4/4] 점수화 후 Top {TOP_N} 선정...\n")
    for p in papers:
        p["score"] = selection_score(p)
    # 1) 점수로 Top N 선별 → 2) 표시는 연도(2026 우선)·venue tier·점수 순으로 정렬
    top = sorted(papers, key=lambda p: p["score"], reverse=True)[:TOP_N]
    top.sort(key=lambda p: (p["year"], p["venue_score"], p["score"]), reverse=True)

    for i, p in enumerate(top, 1):
        grade = f" ({p['grade']})" if p["grade"] else ""
        cites = "N/A" if p["citations"] is None else p["citations"]
        print("=" * 60)
        print(f"[{i}] {p['year']} | {p['venue'] or 'Not Available'}{grade}")
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
    print(f"저장 완료: {path}  ({len(top)}편)")
    print(f"사이트 페이지: {page}  (mkdocs 'Searches' 탭)")


if __name__ == "__main__":
    main()
