"""Paper high-level card: summarization agent that compresses a paper into understanding.

Pipeline: Fetch + Parse -> Section Detection (abstract/intro/method only)
-> Semantic Compression (LLM) -> Template Fill -> High-Level Paper Card.
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
MAX_RETRIES = 6

INTRO_HEADERS = ("introduction", "background", "overview")
METHOD_HEADERS = (
    "method", "methodology", "approach", "model", "framework",
    "architecture", "proposed", "our ", "preliminaries",
)
# Sections where extraction stops (detail beyond this point is ignored).
STOP_HEADERS = (
    "experiment", "evaluation", "result", "ablation", "related work",
    "conclusion", "discussion", "reference", "appendix", "acknowledg",
)


# ---------------------------------------------------------------------------
# (1) Paper loader — title / URL / PDF -> {title, abstract, full_text}
# ---------------------------------------------------------------------------
ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


def _arxiv_id_from(text: str) -> str | None:
    if "arxiv.org" in text:
        m = ARXIV_ID.search(text)
        return m.group(1) if m else None
    # Only accept a bare ID with no spaces, to avoid matching titles.
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
        "url": result.entry_id,
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
    # Public PDF; fall back to abstract-only if access is restricted.
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
    """Detect input form (title / URL / PDF) and load the paper accordingly."""
    text = user_input.strip()

    if text.lower().endswith(".pdf") and Path(text).exists():
        full = _pdf_to_text(text)
        return {"title": Path(text).stem, "abstract": "", "full_text": full,
                "authors": [], "year": None, "venue": None, "doi": None, "url": None}

    if "openreview.net" in text:
        m = re.search(r"id=([\w-]+)", text)
        if m:
            return _load_openreview(m.group(1))

    aid = _arxiv_id_from(text)
    if aid:
        return _load_arxiv(aid)

    if text.lower().startswith("http") and ".pdf" in text.lower():
        full = _pdf_to_text(_download(text))
        return {"title": text.rsplit("/", 1)[-1], "abstract": "", "full_text": full,
                "authors": [], "year": None, "venue": None, "doi": None, "url": text}

    # Otherwise treat the input as a title and search arXiv.
    return _search_arxiv_by_title(text)


# ---------------------------------------------------------------------------
# (2) Lightweight structure extractor — abstract / intro / method only
# ---------------------------------------------------------------------------
def _find_section(text: str, headers: tuple, stop: tuple) -> str:
    """Extract from a header line up to the next stop header."""
    lines = text.split("\n")
    capturing, buf = False, []
    for line in lines:
        low = line.strip().lower()
        # Section titles are usually short — avoid matching long body lines.
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
    """Keep only the core text passed to the LLM; no full parsing."""
    full = paper.get("full_text", "") or ""
    abstract = paper.get("abstract", "")

    # Recover abstract from the body when not provided (non-arXiv sources).
    if not abstract and full:
        m = re.search(r"abstract\s*[\n:]+(.{100,2500}?)(?:\n\s*\n|introduction)",
                      full, re.IGNORECASE | re.DOTALL)
        if m:
            abstract = m.group(1).strip()

    intro = _find_section(full, INTRO_HEADERS, STOP_HEADERS)
    method = _find_section(full, METHOD_HEADERS, STOP_HEADERS)

    # Abstract alone is enough to build a card if body extraction fails.
    return {
        "abstract": abstract[:3000],
        "intro": intro[:6000],
        "method": method[:6000],
    }


# ---------------------------------------------------------------------------
# (3) Semantic compression engine — LLM slot filling
# ---------------------------------------------------------------------------
class PaperCard(BaseModel):
    apa: str                 # APA 7th citation (text)
    motivation: str          # why the paper exists (prior limits)
    problem: str             # one sentence + formal-ish problem definition
    key_idea: str            # core intuition in one line
    method_steps: list[str]  # high-level pipeline, 3-5 steps
    contributions: list[str] # novelty bullets, 2-4
    result: str              # whether performance improved (else "Not Available")
    keywords: list[str]      # 3-5 topical keywords (lowercase English) for tagging


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
- keywords: 이 논문을 대표하는 핵심 키워드 3~5개. **영어 소문자, 1~3단어**의 분야/방법 명사구.
            서로 다른 논문이 같은 주제면 동일한 표기를 쓰도록 일반적인 용어를 사용한다.
            예) ["causal inference", "interpretability", "sparse autoencoders"]
"""


def compress(paper: dict, sections: dict) -> PaperCard:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    meta = (
        f"authors: {', '.join(paper.get('authors') or []) or 'unknown'}\n"
        f"year: {paper.get('year') or 'unknown'}\n"
        f"venue: {paper.get('venue') or 'unknown'}\n"
        f"doi: {paper.get('doi') or 'none'}"
    )
    # Provide the body header too, since local/URL PDFs carry author info there.
    header = (paper.get("full_text") or "")[:1200]
    content = (
        f"# Paper Title\n{paper['title']}\n\n"
        f"# Metadata\n{meta}\n\n"
        f"# Document Header (for author extraction)\n{header}\n\n"
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
    # Gemini 503 (overload) / 429 (quota) are transient — retry, honoring retryDelay.
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
# (4) Template filler — fixed schema -> Markdown card
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


def _slug(text: str, n: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:n]


def _norm_tag(text: str) -> str:
    return re.sub(r"[_\s]+", " ", (text or "").strip().lower())


def _card_tags(card: PaperCard, topic: str | None) -> list[str]:
    """Topic + LLM keywords, normalized and deduped (order preserved)."""
    tags: list[str] = []
    for raw in ([topic] if topic else []) + list(card.keywords or []):
        t = _norm_tag(raw)
        if t and t not in tags:
            tags.append(t)
    return tags[:6]


def _front_matter(tags: list[str]) -> str:
    if not tags:
        return ""
    body = "".join(f"  - {t}\n" for t in tags)
    return f"---\ntags:\n{body}---\n\n"


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

    # A topic becomes cards/<topic>/, which MkDocs renders as a tab.
    folder = Path(out_dir) / _slug(topic) if topic else Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_slug(paper['title']) or 'card'}.md"
    # Front-matter tags drive the Material "tags" plugin (per-keyword listing).
    path.write_text(_front_matter(_card_tags(card, topic)) + rendered, encoding="utf-8")
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
