# best-sb-restaurants

Best restaurants near any address or city according to Yelp. Started as a one-off notebook for Santa Barbara (`yelp.ipynb`, results in `results/santa-barbara-ca/`), now a script and a Claude Code skill that take an arbitrary location and pull every place Yelp lists inside a radius, past the 240 results Yelp caps a single search at

## Usage

```
uv sync
uv run scripts/best_restaurants.py "682 MacArthur Dr, Daly City"
uv run scripts/best_restaurants.py "Santa Barbara, CA" --what Cafes
uv run scripts/best_restaurants.py "Santa Barbara, CA" --radius 10
```

Output goes to `results/<location slug>/`:

- `results.csv`: organic results with ads dropped, sorted by rating then review count. Columns are url, name, rating, review_count, review_count_is_exact, price, distance_miles, categories. Places with no reviews yet have an empty rating and count and sort to the bottom. Yelp only shows distances when the location is a street address and it picked the search area itself, so `distance_miles` is filled in only with `--radius 0`
- `ratings_by_price.png`: box plot of rating by price level, the same chart the notebook drew
- `search_pages.json`: the raw parsed cards from every search page of every tile plus the exact review counts fetched for abbreviated cards, so `--rebuild` can regenerate the other two files offline

Flags: `--what` changes the search term (default Restaurants), `--radius` sets how many miles around the location to search (default 5, and 0 lets Yelp pick its own smaller area, about 2.7 miles for a Daly City address, with its 240 result cap), `--out-dir` picks another output folder, `--channel` picks chrome, msedge, or chromium, `--max-block-wait` sets how many minutes to keep waiting for Yelp's bot check to clear (default 30), `--fresh` ignores an unfinished run saved in the output folder instead of resuming it, `--rebuild` skips Yelp and rebuilds from a saved `search_pages.json`

## As a Claude Code skill

The repo ships `.claude/skills/best-restaurants/SKILL.md`, so inside this repo `/best-restaurants Santa Barbara, CA` runs the whole thing and summarizes the top of the list. To use it from anywhere, symlink the skill folder into `~/.claude/skills/`

## How it works

Yelp is behind DataDome, which blocks curl, headless Chrome, and Chrome started with Playwright's default automation flags. The script launches a visible Chrome through Playwright without those flags and with its own persistent profile under `~/.cache/best-restaurants/`, loads the search page once so the bot check clears, then fetches the remaining pages from inside that page with `fetch()` one to two seconds apart. A profile Yelp has blocked gets wiped and the run retries once. Results come from the server-rendered HTML cards because the `/search/snippet` JSON the notebook used no longer includes organic businesses. Review counts above 1000 show up abbreviated (`1.9k`) on cards, so those businesses get one extra request each to read the exact count from the business page title

The search area is a bounding box passed through Yelp's `l=g:west,south,east,north` parameter, built from the map center Yelp returns for the location and the `--radius` in miles. Without it Yelp picks a small area of its own, which is why the first Daly City run reached out only 2.7 miles

Yelp caps every search at 240 results, and those 240 are its most relevant matches inside the box re-sorted by rating, not the 240 highest rated places, so a 5 mile box around Daly City used to bottom out at 3.9 stars while leaving out Sofra Grill (4.7 stars, 4.4 miles away). To get past the cap the script checks the total on the first page of the box, and whenever a box reports 240 it splits it into four quarters and searches each one, recursing until every tile is under the cap or roughly a third of a mile across. Tiles do not overlap, and the few places Yelp lists in more than one tile are deduplicated by business URL. The union is then sorted by rating then review count to match the notebook. A denser area means more tiles and a longer run: requests go out four to seven seconds apart with a longer break every forty, since a faster first attempt tripped DataDome's captcha about sixty pages in. Every finished tile and exact count is written to `search_pages.json` as it lands, so a run that hits the captcha waits for it to clear (reloading the search page with growing backoff and clearing cookies every other try, for up to `--max-block-wait` minutes), and a run that gives up or gets killed resumes where it stopped when you run the same command again

## Results

- `results/682-macarthur-dr-daly-city/`: 682 MacArthur Dr, Daly City at the default 5 mile radius, 1854 places from 16 tiles
- `results/santa-barbara-ca/`: the original 2025 Santa Barbara pull lives in `reviews.json` and `results.csv` at the repo root, and this folder holds a rerun with the script from before tiling, so it stops at Yelp's 240 cap; `--rebuild` still reads it
