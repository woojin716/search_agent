# search_agent

Paper search and summarization tools.

- `main.py` — search recent papers by keyword, score them, save Top N as CSV
- `summary.py` — summarize a single paper into a High-Level Card in Markdown
- MkDocs site — visualize generated cards as keyword tabs

## Quickstart

Open a terminal and run in order:

```bash
# 1. Go to the project folder
cd /Users/jeong-woojin/workspace/search_agent

# 2. Summarize a paper. Pass a link, title, or PDF path
uv run python summary.py "<link or title or PDF path>" -t "<keyword>"

# 3. Publish the site
uv run mkdocs gh-deploy
```

- No need to set up a virtual environment. `uv run` handles it automatically, so you don't need `source .venv/bin/activate`.
- Run `uv sync` only the first time or when dependencies change.

## Setup

```bash
uv sync
```

Set the Gemini API key in `.env`:

```
GEMINI_API_KEY=your_key_here
```

## summary.py — paper summarization

The input type is detected automatically: title, URL, or PDF.

```bash
uv run python summary.py 1706.03762                          # arXiv ID
uv run python summary.py "https://arxiv.org/abs/2310.06825"  # arXiv URL
uv run python summary.py "https://openreview.net/forum?id=LmLmhb6GEL"  # OpenReview URL
uv run python summary.py "https://example.com/paper.pdf"     # PDF URL
uv run python summary.py paper.pdf                           # local PDF
uv run python summary.py "attention is all you need"         # title search
uv run python summary.py                                     # no args, prompts for input
```

Use `--topic` or `-t` to set a keyword. The card is saved under that folder and becomes a tab on the site. If omitted, you are prompted during the run.

```bash
uv run python summary.py 1706.03762 --topic "transformers"
```

Output goes to the console and to `cards/<keyword>/<title>.md`. Card sections: Motivation, Problem, Key Idea, Method, Contribution, Result.

## main.py — paper search

```bash
uv run python main.py
# enter a query, saved to results/<query>.csv
```

Searches OpenAlex and OpenReview for papers from the last 3 years, scores them by relevance, venue, recency, and impact, then saves the Top N.

## Site

Cards in `cards/` are published as a searchable site grouped by keyword tabs.

Live site: https://woojin716.github.io/search_agent/

```bash
uv run mkdocs serve        # preview at http://127.0.0.1:8000
uv run mkdocs gh-deploy    # publish or update the live site
```

- Top tabs are subfolders under `cards/`
- Sidebar lists the papers within a folder
