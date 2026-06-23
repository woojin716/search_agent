# search_agent

논문 탐색·요약 도구.

- `main.py` — 키워드로 최신 논문을 검색해 점수화하고 Top N을 CSV로 저장
- `summary.py` — 논문 1편을 High-Level Card(Markdown)로 요약
- MkDocs 사이트 — 생성된 카드를 키워드 탭으로 시각화

## 빠른 실행 (컴퓨터 켠 뒤)

터미널을 열고 순서대로:

```bash
# 1. 프로젝트 폴더로 이동
cd /Users/jeong-woojin/workspace/search_agent

# 2. 논문 요약 (링크/제목/PDF 중 아무거나)
uv run python summary.py "<논문 링크 or 제목 or PDF경로>" -t "<키워드>"

# 3. 사이트 띄우기 → 브라우저에서 http://127.0.0.1:8000 열기
uv run mkdocs serve
```

- 가상환경은 따로 만들 필요 없음. `uv run`이 자동으로 처리한다. (`source .venv/bin/activate` 불필요)
- 처음 한 번만, 또는 의존성이 바뀌었을 때만 `uv sync` 실행.
- **사이트는 항상 떠 있지 않다.** `uv run mkdocs serve`를 실행하는 동안에만 보이고, 터미널을 닫거나 `Ctrl+C`를 누르면 내려간다. 본인 컴퓨터(localhost)에서만 접속 가능.

## 설치

```bash
uv sync
```

`.env`에 Gemini API 키 설정:

```
GEMINI_API_KEY=your_key_here
```

## summary.py — 논문 요약

입력(title / URL / PDF)을 자동 판별한다.

```bash
uv run python summary.py 1706.03762                          # arXiv ID
uv run python summary.py "https://arxiv.org/abs/2310.06825"  # arXiv URL
uv run python summary.py "https://openreview.net/forum?id=LmLmhb6GEL"  # OpenReview URL
uv run python summary.py "https://example.com/paper.pdf"     # PDF URL
uv run python summary.py paper.pdf                           # 로컬 PDF
uv run python summary.py "attention is all you need"         # 제목 검색
uv run python summary.py                                     # 인자 없으면 프롬프트
```

`--topic`(또는 `-t`)으로 키워드를 지정하면 해당 폴더에 저장되고, 사이트에서 탭이 된다. 생략하면 실행 중 물어본다.

```bash
uv run python summary.py 1706.03762 --topic "transformers"
```

출력: 화면 + `cards/<키워드>/<제목>.md`. 카드 구성 — Motivation / Problem / Key Idea / Method / Contribution / Result.

## main.py — 논문 검색

```bash
uv run python main.py
# 검색어 입력 → results/<검색어>.csv 저장
```

OpenAlex + OpenReview에서 최근 3년 논문을 검색해 관련성·venue·최신성·영향력으로 점수화한 뒤 Top N을 저장한다.

## 사이트 (논문 카드 시각화)

`cards/`의 카드를 키워드 탭으로 묶어 검색 가능한 사이트로 본다. `cards/`는 레포에 포함하지 않으므로 사이트는 로컬에서 빌드한다.

```bash
uv run mkdocs serve     # 로컬 사이트 → http://127.0.0.1:8000
uv run mkdocs build     # site/ 에 정적 사이트 생성
```

사이트 링크 (로컬): http://127.0.0.1:8000

- 상단 탭 = `cards/` 하위 폴더(키워드)
- 사이드바 = 폴더 안의 논문들
