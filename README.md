# search_agent

Paper search and summarization tools.

- `main.py` — search recent papers by keyword, score them, save Top N as CSV
- `summary.py` — summarize a single paper into a High-Level Card in Markdown
- MkDocs site — visualize generated cards as keyword tabs

## Setup

```bash
uv sync
```

Set the Gemini API key in `.env`:

```
GEMINI_API_KEY=your_key_here
```

`uv run` handles the virtual environment automatically, so no manual activation is needed. Run `uv sync` only the first time or when dependencies change.

## summary.py — paper summarization

The input type is detected automatically: title, URL, or PDF.

```bash
uv run python summary.py <arXiv ID, URL, PDF path, or title>
uv run python summary.py            # no args, prompts for input
```

Use `--topic` or `-t` to set a keyword. The card is saved under that folder and becomes a tab on the site. If omitted, you are prompted during the run.

```bash
uv run python summary.py <input> --topic <keyword>
```

Output goes to the console and to a Markdown card under `cards/`. Card sections: Motivation, Problem, Key Idea, Method, Contribution, Result.

## main.py — paper search

```bash
uv run python main.py
```

Enter a query when prompted. Searches recent papers, scores them by relevance, venue, recency, and impact, then saves the Top N as a CSV under `results/` and a table page under `cards/`.

## Site

Cards in `cards/` are published as a searchable site grouped by keyword tabs.

```bash
uv run mkdocs serve        # local preview
uv run mkdocs gh-deploy    # publish or update the site
```

- Top tabs are subfolders under `cards/`
- Sidebar lists the papers within a folder
