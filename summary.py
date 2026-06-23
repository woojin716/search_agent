"""Paper High-Level Card — 논문을 '이해의 압축'으로 변환하는 요약 agent.

설계 철학: 이 agent 는 "정보 생성기"가 아니라 "이해 압축기"다.
    ❌ 수식 · ablation · baseline detail · experimental setup · critique
    ✅ abstraction · 구조화 · 직관화 · story

파이프라인
    INPUT (title / URL / PDF)
      ↓ (1) Fetch + Parse        — arXiv API / 직접 PDF
      ↓ (2) Section Detection    — abstract / intro / method 만 추출 (나머지 무시)
      ↓ (3) Semantic Compression — LLM 이 motivation·problem·method·contribution 추출
      ↓ (4) Template Fill        — 고정 schema slot filling
    OUTPUT (High-Level Paper Card)
"""

import os
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import arxiv
import openreview
import pypdf
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel

load_dotenv()

MODEL = "gemini-2.5-flash-lite"
MAX_RETRIES = 6   # Gemini 429/503 재시도 횟수

# 본문에서 추출할 섹션만 (나머지는 의도적으로 무시한다)
INTRO_HEADERS = ("introduction", "background", "overview")
METHOD_HEADERS = (
    "method", "methodology", "approach", "model", "framework",
    "architecture", "proposed", "our ", "preliminaries",
)
# 추출을 멈출 섹션 (여기부터는 detail → 무시)
STOP_HEADERS = (
    "experiment", "evaluation", "result", "ablation", "related work",
    "conclusion", "discussion", "reference", "appendix", "acknowledg",
)


# ---------------------------------------------------------------------------
# (1) Paper Loader — title / URL / PDF → {title, abstract, full_text}
# ---------------------------------------------------------------------------
ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


def _arxiv_id_from(text: str) -> str | None:
    """arXiv URL 또는 bare ID 에서 ID 추출."""
    if "arxiv.org" in text:
        m = ARXIV_ID.search(text)
        return m.group(1) if m else None
    # 'agent memory' 같은 제목과 구분: 공백 없는 순수 ID 패턴만 인정
    if re.fullmatch(r"\s*\d{4}\.\d{4,5}(v\d+)?\s*", text):
        return ARXIV_ID.search(text).group(1)
    return None


def _pdf_to_text(path: str) -> str:
    reader = pypdf.PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _download(url: str) -> str:
    fd, tmp = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as r, open(tmp, "wb") as f:
        f.write(r.read())
    return tmp


def _arxiv_result_to_paper(result) -> dict:
    return {
        "title": result.title.strip(),
        "abstract": result.summary.strip(),
        "full_text": _pdf_to_text(_download(result.pdf_url)),
        "authors": [a.name for a in result.authors],
        "year": result.published.year if result.published else None,
        "venue": "arXiv preprint",
        "doi": result.doi,
        "url": result.entry_id,   # arXiv abs 페이지
    }


def _load_arxiv(arxiv_id: str) -> dict:
    client = arxiv.Client()
    result = next(client.results(arxiv.Search(id_list=[arxiv_id])))
    return _arxiv_result_to_paper(result)


def _openreview_val(content: dict, key: str):
    x = content.get(key)
    return x.get("value") if isinstance(x, dict) else x


def _load_openreview(forum_id: str) -> dict:
    client = openreview.api.OpenReviewClient(baseurl="https://api2.openreview.net")
    note = client.get_notes(id=forum_id)[0]
    c = note.content
    venue = _openreview_val(c, "venue") or ""
    year = next((int(y) for y in re.findall(r"\d{4}", venue)), None)
    # 공개 논문 PDF (인증 필요 시 본문 없이 abstract 만으로 진행)
    try:
        full = _pdf_to_text(_download(f"https://openreview.net/pdf?id={forum_id}"))
    except Exception:
        full = ""
    return {
        "title": (_openreview_val(c, "title") or "").strip(),
        "abstract": (_openreview_val(c, "abstract") or "").strip(),
        "full_text": full,
        "authors": _openreview_val(c, "authors") or [],
        "year": year,
        "venue": venue or "OpenReview",
        "doi": None,
        "url": f"https://openreview.net/forum?id={forum_id}",
    }


def _search_arxiv_by_title(title: str) -> dict:
    client = arxiv.Client()
    result = next(client.results(
        arxiv.Search(query=title, max_results=1,
                     sort_by=arxiv.SortCriterion.Relevance)
    ))
    print(f"      매칭: {result.title.strip()}")
    return _arxiv_result_to_paper(result)


def load_paper(user_input: str) -> dict:
    """입력 형태(title / URL / PDF)를 자동 판별해 논문을 로드한다."""
    text = user_input.strip()

    # 1) 로컬 PDF 파일
    if text.lower().endswith(".pdf") and Path(text).exists():
        full = _pdf_to_text(text)
        return {"title": Path(text).stem, "abstract": "", "full_text": full,
                "authors": [], "year": None, "venue": None, "doi": None, "url": None}

    # 2) OpenReview (forum URL)
    if "openreview.net" in text:
        m = re.search(r"id=([\w-]+)", text)
        if m:
            return _load_openreview(m.group(1))

    # 3) arXiv (URL 또는 bare ID)
    aid = _arxiv_id_from(text)
    if aid:
        return _load_arxiv(aid)

    # 4) 직접 PDF URL
    if text.lower().startswith("http") and ".pdf" in text.lower():
        full = _pdf_to_text(_download(text))
        return {"title": text.rsplit("/", 1)[-1], "abstract": "", "full_text": full,
                "authors": [], "year": None, "venue": None, "doi": None, "url": text}

    # 5) 그 외 → 제목으로 간주, arXiv 검색
    return _search_arxiv_by_title(text)


# ---------------------------------------------------------------------------
# (2) Lightweight Structure Extractor — abstract / intro / method 만
# ---------------------------------------------------------------------------
def _find_section(text: str, headers: tuple, stop: tuple) -> str:
    """header 로 시작하는 섹션부터 stop header 직전까지를 추출한다."""
    lines = text.split("\n")
    capturing, buf = False, []
    for line in lines:
        low = line.strip().lower()
        # 섹션 제목 줄은 보통 짧다 → 긴 본문 줄이 header 와 우연히 겹치는 것 방지
        is_header = len(low) < 60 and low
        if is_header and any(low.startswith(h) or h in low[:25] for h in stop):
            if capturing:
                break
        if is_header and any(low.startswith(h) or (h in low[:25] and len(low) < 40)
                             for h in headers):
            capturing = True
        if capturing:
            buf.append(line)
    return "\n".join(buf).strip()


def extract_sections(paper: dict) -> dict:
    """LLM 에 넘길 핵심 텍스트만 추린다. full parsing 은 하지 않는다."""
    full = paper.get("full_text", "") or ""
    abstract = paper.get("abstract", "")

    # 본문에서 abstract 보강 (arXiv 가 아닌 경우)
    if not abstract and full:
        m = re.search(r"abstract\s*[\n:]+(.{100,2500}?)(?:\n\s*\n|introduction)",
                      full, re.IGNORECASE | re.DOTALL)
        if m:
            abstract = m.group(1).strip()

    intro = _find_section(full, INTRO_HEADERS, STOP_HEADERS)
    method = _find_section(full, METHOD_HEADERS, STOP_HEADERS)

    # 본문 추출 실패 시(스캔 PDF 등) abstract 만으로도 카드 생성 가능하게 한다
    return {
        "abstract": abstract[:3000],
        "intro": intro[:6000],
        "method": method[:6000],
    }


# ---------------------------------------------------------------------------
# (3) Semantic Compression Engine — LLM slot filling
# ---------------------------------------------------------------------------
class PaperCard(BaseModel):
    apa: str                 # APA 7th 인용 (text)
    motivation: str          # 왜 이 논문이 존재하는가 (기존 한계 2~3줄)
    problem: str             # 한 문장 + formal-ish 문제 정의
    key_idea: str            # 본질 한 줄 (intuition)
    method_steps: list[str]  # high-level pipeline 3~5 step
    contributions: list[str] # novelty bullet 2~4개
    result: str              # 성능 향상 여부만 (없으면 "Not Available")


SYSTEM_PROMPT = """You are a high-level paper abstraction engine.

Your job is NOT to explain details.

You MUST:
- compress scientific papers into conceptual understanding
- remove all mathematical and experimental detail
- focus on intuition, motivation, and method flow

Output must be:
- short
- structured
- conceptual
- non-technical unless necessary for meaning

Do NOT include:
- equations
- implementation details
- experimental setup
- ablation studies
- dataset descriptions

Write in Korean, but keep core technical terms in English.
Fill each slot of the schema:
- apa: APA 7th 형식의 인용을 text 로. 단, 제목은 이미 카드 헤더에 있으므로 **제목은 제외**한다.
       즉 "저자 (연도). venue.{DOI}" 형태. 주어진 메타데이터(저자/연도/venue/DOI)를 우선 사용하고,
       없으면 논문 본문 첫 부분에서 저자·연도를 추출한다. 추출 불가하면 "Not Available".
       예) Vaswani, A., et al. (2017). arXiv preprint. https://doi.org/...
- motivation: 기존 방법의 한계를 2~3줄로. 왜 이 논문이 필요했는가.
- problem: 논문이 푸는 문제를 한 문장 + 약간의 formal 한 정의로.
- key_idea: 이 논문의 본질 한 줄. intuition 중심.
- method_steps: high-level pipeline 을 3~5 step 으로. 수식 없이.
- contributions: 실제 새로움(novelty)만 2~4 bullet.
- result: 성능 향상 여부만 한 줄 (detail 금지). 정보 없으면 "Not Available".
"""


def compress(paper: dict, sections: dict) -> PaperCard:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    meta = (
        f"authors: {', '.join(paper.get('authors') or []) or 'unknown'}\n"
        f"year: {paper.get('year') or 'unknown'}\n"
        f"venue: {paper.get('venue') or 'unknown'}\n"
        f"doi: {paper.get('doi') or 'none'}"
    )
    # 메타데이터가 없으면(로컬/URL PDF) 본문 앞부분에 저자 정보가 있으므로 함께 제공
    header = (paper.get("full_text") or "")[:1200]
    content = (
        f"# Paper Title\n{paper['title']}\n\n"
        f"# Metadata\n{meta}\n\n"
        f"# Document Header (저자 추출용)\n{header}\n\n"
        f"# Abstract\n{sections['abstract'] or '(none)'}\n\n"
        f"# Introduction\n{sections['intro'] or '(none)'}\n\n"
        f"# Method\n{sections['method'] or '(none)'}"
    )
    config = {
        "system_instruction": SYSTEM_PROMPT,
        "response_mime_type": "application/json",
        "response_schema": PaperCard,
        "temperature": 0.3,
    }
    # Gemini 503(과부하) / 429(quota) 는 일시적 → 재시도.
    # 429 응답에 retryDelay 가 있으면 그 값을 존중한다.
    for attempt in range(4):
        try:
            return client.models.generate_content(
                model=MODEL, contents=content, config=config
            ).parsed
        except (genai.errors.ServerError, genai.errors.ClientError) as e:
            code = getattr(e, "code", None)
            if code not in (429, 503) or attempt == 3:
                raise
            m = re.search(r"retryDelay['\":\s]+(\d+)", str(e))
            wait = int(m.group(1)) + 1 if m else 5 * (attempt + 1)
            reason = "quota 초과" if code == 429 else "과부하"
            print(f"      [warn] Gemini {reason} — {wait}s 후 재시도...")
            time.sleep(wait)


# ---------------------------------------------------------------------------
# (4) Template Filler — 고정 schema → Markdown card
# ---------------------------------------------------------------------------
def render_card(title: str, card: PaperCard, url: str | None = None) -> str:
    steps = "\n".join(f"   {i}. {s}" for i, s in enumerate(card.method_steps, 1))
    contribs = "\n".join(f"   - {c}" for c in card.contributions)
    link = f"\n🔗 [{url}]({url})\n" if url else ""
    return f"""# {title}

{card.apa}
{link}
---

## Motivation
{card.motivation}

## Problem / Task Definition
{card.problem}

## Key Idea
> {card.key_idea}

## Method
{steps}

## Contribution
{contribs}

## Result
{card.result}
"""


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def _slug(text: str, n: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:n]


def summarize(user_input: str, topic: str | None = None, out_dir: str = "cards") -> str:
    print("[1/4] Fetch + Parse...")
    paper = load_paper(user_input)

    print("[2/4] Section Detection (abstract / intro / method)...")
    sections = extract_sections(paper)

    print("[3/4] Semantic Compression (LLM)...")
    card = compress(paper, sections)

    print("[4/4] Template Fill...\n")
    rendered = render_card(paper["title"], card, paper.get("url"))
    print(rendered)

    # topic(키워드)이 있으면 cards/<topic>/ 하위에 저장 → MkDocs 에서 탭이 됨
    folder = Path(out_dir) / _slug(topic) if topic else Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_slug(paper['title']) or 'card'}.md"
    path.write_text(rendered, encoding="utf-8")
    print(f"\n저장 완료: {path}")
    return rendered


def main():
    args = sys.argv[1:]
    topic = None
    for flag in ("--topic", "-t"):
        if flag in args:
            i = args.index(flag)
            topic = args[i + 1] if i + 1 < len(args) else None
            del args[i:i + 2]
            break

    user_input = " ".join(args).strip() or input("논문 (title / URL / PDF): ").strip()
    if topic is None:
        topic = input("주제/키워드 (탭 이름, 비우면 분류 안 함): ").strip() or None
    summarize(user_input, topic)


if __name__ == "__main__":
    main()
