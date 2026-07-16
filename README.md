# search_agent

Keyword search and ranking for recent AI conference papers.

## Install

```bash
pip install -r requirements.txt
```

## Search

```bash
python main.py "your keywords"
```

Queries OpenAlex + OpenReview (ICLR/NeurIPS/ICML), ranks by relevance, venue, recency, and
impact, and saves the Top N to `results/<slug>.csv` and `cards/searches/<slug>.md`.

OpenReview blocks anonymous bulk access, so that source is off unless you log in. Copy
`.env.example` to `.env` and fill in your OpenReview credentials to enable it.

## Site

```bash
mkdocs serve        # local preview
mkdocs gh-deploy    # publish to GitHub Pages
```
