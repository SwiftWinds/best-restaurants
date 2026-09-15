---
name: best-restaurants
description: Pull Yelp's full ranked list (up to 240 places, ads dropped, exact review counts) of restaurants or any other search term near an arbitrary address or city, saved as results.csv plus a ratings-by-price box plot. Use when the user types /best-restaurants <address or city>, asks for the best restaurants near somewhere according to Yelp, or wants the best-sb-restaurants analysis repeated for a new location.
---

# Best restaurants near a location

Runs `scripts/best_restaurants.py` from this repo for the location the user gave and reports the top of the list. The script drives a visible Chrome window through Playwright because Yelp sits behind DataDome, which blocks curl, headless Chrome, and Chrome launched with the usual automation flags. Chrome opens the search page once, and every later page is fetched from inside that page with `fetch()`, spaced one to two seconds apart

## Steps

1. Take the location from the arguments verbatim, an address like `682 MacArthur Dr, Daly City` or a city like `Santa Barbara, CA`. If the user named something other than restaurants (cafes, bars, ramen), pass it with `--what`. If they named a distance, pass it as `--radius <miles>`; the default is 5 miles around the location, and `--radius 0` lets Yelp pick its own smaller area
2. Find the repo root: this skill lives at `.claude/skills/best-restaurants/` inside a checkout of github.com/SwiftWinds/best-sb-restaurants, and when it is symlinked into `~/.claude/skills/`, `readlink -f ~/.claude/skills/best-restaurants` points back into that checkout. Clone the repo if neither exists. From the repo root run `uv sync` once if `.venv` is missing, then `uv run playwright install chrome` only if Google Chrome is not installed (`--channel msedge` and `--channel chromium` also work)
3. Run `uv run scripts/best_restaurants.py "<location>"` and let it finish; a Chrome window opens and closes on its own, so the machine needs a desktop session. It keeps its own Chrome profile under `~/.cache/best-restaurants/` so later runs pass the bot check faster, and if Yelp shows "You have been blocked" it wipes that profile and tries once more. If the window shows a captcha, tell the user to solve it in that window; the script waits up to five minutes
4. Output lands in `results/<location slug>/`: `results.csv` sorted by rating then review count (columns url, name, rating, review_count, review_count_is_exact, price, distance_miles, yelp_rank, categories; distance is only filled in for street addresses searched with `--radius 0`, since Yelp drops it when the search area is a bounding box), `ratings_by_price.png`, and `search_pages.json` with the raw cards. `--out-dir` overrides the folder, `--rebuild` regenerates the CSV and plot from a saved `search_pages.json` without touching Yelp
5. Report the result count and the top few rows. Yelp caps every search at 240 results, so a dense city returns exactly 240 and a small town returns fewer; check the `total` logged on the first page when the count looks low. The 240 are Yelp's most relevant matches inside the radius re-sorted by rating, not the 240 highest rated places, so a well-rated spot can be missing even inside the radius; when the user asks about one, search its cuisine with `--what` (Sofra Grill shows up under Turkish but not under Restaurants near Daly City)

## Gotchas

- Cards abbreviate review counts above 1000 as `1.9k`. The script fetches each such business page and reads the exact count from its `<title>`, which is what `review_count_is_exact` records
- Yelp's `/search/snippet` JSON endpoint no longer carries organic businesses, so the script parses the server-rendered HTML cards (`[data-testid="serp-ia-card"]`); if the card count on the first page is zero with HTTP 200, Yelp changed its markup and the selectors in `PARSE_CARDS_JS` need updating
- A `403` or `429` mid-run makes the script reopen the search page and retry three times before giving up, so a run that stalls with a captcha in the window needs a human once, not a restart
- The radius becomes Yelp's `l=g:west,south,east,north` bounding box, centered on the map center Yelp returns for the location (read from the distance presets on the loaded page); if Yelp stops rendering those presets the script logs it and falls back to Yelp's default area
