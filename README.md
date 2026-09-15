# best-sb-restaurants

Best restaurants near any address or city according to Yelp. Started as a one-off notebook for Santa Barbara (`yelp.ipynb`, results in `results/santa-barbara-ca/`), now a script and a Claude Code skill that take an arbitrary location and pull Yelp's full ranked list, up to the 240 results Yelp caps every search at

## Usage

```
uv sync
uv run scripts/best_restaurants.py "682 MacArthur Dr, Daly City"
uv run scripts/best_restaurants.py "Santa Barbara, CA" --what Cafes
```

Output goes to `results/<location slug>/`:

- `results.csv`: organic results with ads dropped, sorted by rating then review count. Columns are url, name, rating, review_count, review_count_is_exact, price, distance_miles, yelp_rank, categories. Yelp only shows distances when the location is a street address, so `distance_miles` is empty for city searches
- `ratings_by_price.png`: box plot of rating by price level, the same chart the notebook drew
- `search_pages.json`: the raw parsed cards from every search page plus the exact review counts fetched for abbreviated cards, so `--rebuild` can regenerate the other two files offline

Flags: `--what` changes the search term (default Restaurants), `--out-dir` picks another output folder, `--channel` picks chrome, msedge, or chromium, `--rebuild` skips Yelp and rebuilds from a saved `search_pages.json`

## As a Claude Code skill

The repo ships `.claude/skills/best-restaurants/SKILL.md`, so inside this repo `/best-restaurants Santa Barbara, CA` runs the whole thing and summarizes the top of the list. To use it from anywhere, symlink the skill folder into `~/.claude/skills/`

## How it works

Yelp is behind DataDome, which blocks curl, headless Chrome, and Chrome started with Playwright's default automation flags. The script launches a visible Chrome through Playwright without those flags and with its own persistent profile under `~/.cache/best-restaurants/`, loads the search page once so the bot check clears, then fetches the remaining pages from inside that page with `fetch()` one to two seconds apart. A profile Yelp has blocked gets wiped and the run retries once. Results come from the server-rendered HTML cards because the `/search/snippet` JSON the notebook used no longer includes organic businesses. Review counts above 1000 show up abbreviated (`1.9k`) on cards, so those businesses get one extra request each to read the exact count from the business page title

Yelp decides the search radius from the location, so a place a few miles away can be missing even when a name search finds it. Sort order on Yelp's side is rating, and the script re-sorts by rating then review count to match the notebook

## Results

- `results/santa-barbara-ca/`: the original 2025 Santa Barbara pull lives in `reviews.json` and `results.csv` at the repo root, and this folder holds the rerun with the script
- `results/682-macarthur-dr-daly-city/`: 682 MacArthur Dr, Daly City
