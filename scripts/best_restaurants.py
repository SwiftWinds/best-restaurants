import argparse
import json
import math
import random
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from playwright.sync_api import sync_playwright

YELP = "https://www.yelp.com"
PAGE_SIZE = 10
YELP_RESULT_CAP = 240
DEFAULT_RADIUS_MILES = 5
MIN_TILE_HALF_WIDTH_MILES = 0.2
MILES_PER_DEGREE = 69.0
PAUSE_BETWEEN_REQUESTS = (4, 7)
REQUESTS_PER_BREAK = 40
PAUSE_AT_BREAK = (30, 60)
DEFAULT_MAX_BLOCK_WAIT_MINUTES = 30
CARD_SELECTOR = '[data-testid="serp-ia-card"]'
PRICE_LEVELS = {"$": "Low", "$$": "Medium", "$$$": "High", "$$$$": "Very High"}
RESULT_COLUMNS = ["url", "name", "rating", "review_count", "review_count_is_exact", "price", "distance_miles", "categories"]
REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = Path.home() / ".cache" / "best-restaurants" / "chrome-profile"

PARSE_CARDS_JS = r"""
const parseCards = (doc) => Array.from(doc.querySelectorAll('[data-testid="serp-ia-card"]')).map((card) => {
  const nameLink = card.querySelector('h3 a');
  const ratingEl = card.querySelector('[aria-label*="star rating"]');
  const text = card.textContent || '';
  const reviewMatch = text.match(/\(([\d,.]+k?)\s+reviews?\)/i);
  const priceEl = Array.from(card.querySelectorAll('span, div')).find((el) => el.children.length === 0 && /^\${1,4}$/.test(el.textContent.trim()));
  const distanceMatch = text.match(/(\d+(?:\.\d+)?)\s+Miles?/);
  const distanceRaw = distanceMatch ? distanceMatch[1].replace(/^(19|20)\d\d(?=\d)/, '') : null;
  const categories = Array.from(card.querySelectorAll('a[href^="/search?find_desc="]')).map((a) => a.textContent.trim()).filter(Boolean);
  return {
    name: nameLink ? nameLink.textContent.trim() : null,
    href: nameLink ? nameLink.getAttribute('href') : null,
    rating: ratingEl ? parseFloat(ratingEl.getAttribute('aria-label')) : null,
    review_count_raw: reviewMatch ? reviewMatch[1] : null,
    price: priceEl ? priceEl.textContent.trim() : '',
    distance_miles: distanceRaw ? parseFloat(distanceRaw) : null,
    sponsored: !!card.querySelector('a[href^="/adredir"]'),
    categories,
  };
});
"""

FETCH_SEARCH_PAGE_JS = "async ([path]) => {" + PARSE_CARDS_JS + r"""
  const response = await fetch(path, {headers: {Accept: 'text/html'}});
  const html = await response.text();
  const totalMatch = html.match(/"totalResults":\s*(\d+)/);
  const doc = new DOMParser().parseFromString(html, 'text/html');
  return {status: response.status, total: totalMatch ? Number(totalMatch[1]) : null, cards: parseCards(doc)};
}"""

FETCH_BUSINESS_JS = r"""async ([path]) => {
  const response = await fetch(path, {headers: {Accept: 'text/html'}});
  const html = await response.text();
  const titleMatch = html.match(/<title[^>]*>([^<]*)<\/title>/);
  const title = titleMatch ? titleMatch[1] : '';
  const titleCount = title.match(/(\d[\d,]*)\s+Reviews?/i);
  const bodyCount = html.match(/Photos &amp; (\d[\d,]*) Reviews/);
  return {status: response.status, title, count: titleCount ? titleCount[1] : bodyCount ? bodyCount[1] : null};
}"""


def log(message):
    print(message, file=sys.stderr, flush=True)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def search_path(what, location, start, bounds=None):
    path = f"/search?find_desc={quote_plus(what)}&find_loc={quote_plus(location)}&sortby=rating&start={start}"
    return f"{path}&l={quote_plus(bounds)}" if bounds else path


def search_center(page):
    presets = page.evaluate("() => Array.from(document.querySelectorAll('input[name=\"l\"]')).map((input) => input.value)")
    boxes = [preset[2:].split(",") for preset in presets if preset.startswith("g:")]
    if not boxes:
        return None
    west, south, east, north = (float(value) for value in boxes[0])
    return ((south + north) / 2, (west + east) / 2)


def tile_bounds(center, half_width_miles):
    lat, lon = center
    dlat = half_width_miles / MILES_PER_DEGREE
    dlon = half_width_miles / (MILES_PER_DEGREE * math.cos(math.radians(lat)))
    return f"g:{lon - dlon:.6f},{lat - dlat:.6f},{lon + dlon:.6f},{lat + dlat:.6f}"


def quarter_tiles(center, half_width_miles):
    lat, lon = center
    quarter = half_width_miles / 2
    dlat = quarter / MILES_PER_DEGREE
    dlon = quarter / (MILES_PER_DEGREE * math.cos(math.radians(lat)))
    return [((lat + dy * dlat, lon + dx * dlon), quarter) for dy in (1, -1) for dx in (-1, 1)]


def pause(low, high):
    time.sleep(random.uniform(low, high))


def parse_review_count(raw):
    if not raw:
        return None
    raw = raw.replace(",", "")
    if raw.endswith("k"):
        return int(round(float(raw[:-1]) * 1000))
    return int(raw)


def business_path(href):
    return href.split("?")[0]


class BotCheckFailed(Exception):
    pass


def page_state(page):
    if page.locator(CARD_SELECTOR).count():
        return "results"
    if page.locator("text=You have been blocked").count():
        return "blocked"
    if any("captcha" in frame.url for frame in page.frames) or page.locator("text=not a robot").count():
        return "captcha"
    return "loading"


def open_search(page, search_url, wait_seconds):
    page.goto(search_url, wait_until="domcontentloaded")
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        state = page_state(page)
        if state == "results":
            return True
        if state == "blocked":
            raise BotCheckFailed("blocked")
        time.sleep(1)
    return False


def open_search_patiently(page, search_url, max_wait_seconds):
    log(f"Opening {search_url}")
    waited = 0
    backoff = 30
    attempts = 0
    while True:
        if open_search(page, search_url, 45):
            return
        waited += 45
        attempts += 1
        if page_state(page) == "captcha":
            log("Yelp is showing a captcha, solve it in the Chrome window or wait for the block on this network to lift")
        if waited >= max_wait_seconds:
            screenshot = PROFILE_DIR.parent / "last_bot_check.png"
            page.screenshot(path=str(screenshot))
            raise BotCheckFailed(f"Yelp never rendered results after {waited // 60} minutes, screenshot at {screenshot}")
        log(f"Trying the search page again in {backoff}s ({waited // 60} of {max_wait_seconds // 60} minutes waited)")
        time.sleep(backoff)
        waited += backoff
        backoff = min(backoff * 2, 600)
        if attempts % 2 == 0:
            log("Clearing Yelp's cookies so the next load starts as a fresh visitor")
            page.context.clear_cookies()


class YelpSession:
    def __init__(self, page, search_url, max_block_wait_seconds):
        self.page = page
        self.search_url = search_url
        self.max_block_wait_seconds = max_block_wait_seconds
        self.requests = 0

    def open(self):
        open_search_patiently(self.page, self.search_url, self.max_block_wait_seconds)

    def fetch(self, js, path):
        failures = 0
        waited = 0
        while True:
            result = self.page.evaluate(js, [path])
            self.requests += 1
            if result["status"] == 200:
                self.rest()
                return result
            if result["status"] not in (403, 429):
                raise RuntimeError(f"Yelp answered {path} with HTTP {result['status']}")
            failures += 1
            wait = min(30 * 2 ** (failures - 1), 600)
            if waited + wait > self.max_block_wait_seconds:
                raise BotCheckFailed(f"Yelp kept answering {path} with HTTP {result['status']} for {waited // 60} minutes")
            log(f"HTTP {result['status']} for {path}, Yelp's bot check tripped, waiting {wait}s and reloading the search page")
            time.sleep(wait)
            waited += wait
            self.open()

    def rest(self):
        if self.requests % REQUESTS_PER_BREAK:
            pause(*PAUSE_BETWEEN_REQUESTS)
            return
        log(f"{self.requests} requests so far, taking a break and reloading the search page")
        pause(*PAUSE_AT_BREAK)
        self.open()


class RunState:
    def __init__(self, run, pages_file):
        self.run = run
        self.pages_file = pages_file
        self.tiles_by_bounds = {tile["bounds"]: tile for tile in run["tiles"]}
        self.capped = set(run["capped"])

    def save(self):
        self.pages_file.write_text(json.dumps(self.run, indent=1, ensure_ascii=False))

    def add_tile(self, tile):
        self.run["tiles"].append(tile)
        self.tiles_by_bounds[tile["bounds"]] = tile
        self.save()

    def mark_capped(self, bounds):
        self.run["capped"].append(bounds)
        self.capped.add(bounds)
        self.save()

    def add_exact_count(self, path, count):
        self.run["exact_counts"][path] = count
        self.save()


def new_run(what, location, radius_miles):
    return {
        "location": location,
        "what": what,
        "radius_miles": radius_miles,
        "center": None,
        "bounds": None,
        "search_url": None,
        "page_title": None,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched_at": None,
        "complete": False,
        "tiles": [],
        "capped": [],
        "exact_counts": {},
    }


def load_run(pages_file):
    run = json.loads(pages_file.read_text())
    if "tiles" not in run:
        run["tiles"] = [{"bounds": run.get("bounds"), "center": None, "half_width_miles": None, "total": None, "pages": run.pop("pages")}]
    run.setdefault("capped", [])
    return run


def load_unfinished_run(pages_file, what, location, radius_miles):
    if not pages_file.exists():
        return None
    run = load_run(pages_file)
    same_search = run.get("what") == what and run.get("location") == location and (run.get("radius_miles") or 0) == (radius_miles or 0)
    if run.get("complete", True) or not same_search:
        return None
    log(f"Resuming the unfinished run started {run['started_at']}: {len(run['tiles'])} tiles and {len(run['exact_counts'])} exact counts already saved")
    return run


def fetch_search_pages(session, what, location, bounds, first_page):
    pages = []
    result = first_page
    start = 0
    limit = YELP_RESULT_CAP
    while True:
        if result["total"]:
            limit = min(limit, result["total"])
        organic = [card for card in result["cards"] if not card["sponsored"]]
        pages.append({"start": start, "total": result["total"], "cards": result["cards"]})
        log(f"  start={start}: {len(organic)} organic of {len(result['cards'])} cards")
        start += PAGE_SIZE
        if not organic or start >= limit:
            return pages
        result = session.fetch(FETCH_SEARCH_PAGE_JS, search_path(what, location, start, bounds))


def fetch_tile(session, state, center, half_width_miles):
    what, location = state.run["what"], state.run["location"]
    bounds = tile_bounds(center, half_width_miles)
    label = f"{2 * half_width_miles:g} mile tile at {center[0]:.4f}, {center[1]:.4f}"
    if bounds in state.tiles_by_bounds:
        log(f"{label}: already saved, {state.tiles_by_bounds[bounds]['total']} results")
        return
    if bounds in state.capped:
        log(f"{label}: already known to be capped, splitting into quarters")
        for quarter_center, quarter_half_width in quarter_tiles(center, half_width_miles):
            fetch_tile(session, state, quarter_center, quarter_half_width)
        return
    first_page = session.fetch(FETCH_SEARCH_PAGE_JS, search_path(what, location, 0, bounds))
    total = first_page["total"] or 0
    if total >= YELP_RESULT_CAP and half_width_miles > MIN_TILE_HALF_WIDTH_MILES:
        log(f"{label}: Yelp caps it at {YELP_RESULT_CAP}, splitting into quarters")
        state.mark_capped(bounds)
        for quarter_center, quarter_half_width in quarter_tiles(center, half_width_miles):
            fetch_tile(session, state, quarter_center, quarter_half_width)
        return
    if total >= YELP_RESULT_CAP:
        log(f"{label}: still capped at {YELP_RESULT_CAP} at the smallest tile size, keeping what Yelp shows")
    else:
        log(f"{label}: {total} results")
    pages = fetch_search_pages(session, what, location, bounds, first_page)
    state.add_tile({"bounds": bounds, "center": list(center), "half_width_miles": half_width_miles, "total": first_page["total"], "pages": pages})


def fetch_yelp_area(session, state):
    what, location = state.run["what"], state.run["location"]
    if None in state.tiles_by_bounds:
        log("Yelp's own search area: already saved")
        return
    first_page = session.fetch(FETCH_SEARCH_PAGE_JS, search_path(what, location, 0))
    log(f"Yelp's own search area: {first_page['total']} results")
    pages = fetch_search_pages(session, what, location, None, first_page)
    state.add_tile({"bounds": None, "center": None, "half_width_miles": None, "total": first_page["total"], "pages": pages})


def organic_cards(tiles):
    for tile in tiles:
        for page_data in tile["pages"]:
            for card in page_data["cards"]:
                if not card["sponsored"] and card["href"]:
                    yield card


def unique_paths(cards):
    return list(dict.fromkeys(business_path(card["href"]) for card in cards))


def resolve_exact_counts(session, state):
    cards = organic_cards(state.run["tiles"])
    abbreviated = unique_paths(card for card in cards if (card["review_count_raw"] or "").endswith("k"))
    pending = [path for path in abbreviated if path not in state.run["exact_counts"]]
    log(f"{len(abbreviated)} places show abbreviated review counts, {len(pending)} still to fetch")
    for index, path in enumerate(pending, 1):
        for attempt in range(2):
            result = session.fetch(FETCH_BUSINESS_JS, path)
            if result["count"]:
                state.add_exact_count(path, int(result["count"].replace(",", "")))
                break
            log(f"no review count on {path} (title {result['title'][:80]!r}), trying again")
        log(f"exact count {index}/{len(pending)}: {path} -> {state.run['exact_counts'].get(path)}")


def launch_chrome(playwright, channel):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        channel=channel,
        headless=False,
        no_viewport=True,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )


def scrape_with_browser(state, channel, max_block_wait_seconds):
    run = state.run
    search_url = f"{YELP}{search_path(run['what'], run['location'], 0)}"
    with sync_playwright() as playwright:
        context = launch_chrome(playwright, channel)
        page = context.pages[0] if context.pages else context.new_page()
        session = YelpSession(page, search_url, max_block_wait_seconds)
        try:
            session.open()
            run["page_title"] = page.title()
            log(f"Yelp page title: {run['page_title']}")
            radius_miles = run["radius_miles"]
            center = tuple(run["center"]) if run["center"] else (search_center(page) if radius_miles else None)
            if radius_miles and not center:
                log("Yelp gave no map bounds for this location, falling back to its own search area")
                run["radius_miles"] = None
            if center:
                run["center"] = list(center)
                run["bounds"] = tile_bounds(center, radius_miles)
                log(f"Searching within {radius_miles:g} miles of {center[0]:.4f}, {center[1]:.4f}, splitting any tile Yelp caps at {YELP_RESULT_CAP}")
                fetch_tile(session, state, center, radius_miles)
            else:
                fetch_yelp_area(session, state)
            places = len(unique_paths(organic_cards(run["tiles"])))
            log(f"{places} unique places across {len(run['tiles'])} tiles, now fetching exact review counts")
            resolve_exact_counts(session, state)
        finally:
            context.close()
    run["search_url"] = f"{YELP}{search_path(run['what'], run['location'], 0, run['bounds'])}"
    run["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run["complete"] = True
    run["requests"] = run.get("requests", 0) + session.requests
    state.save()


def scrape(state, channel, max_block_wait_seconds):
    try:
        scrape_with_browser(state, channel, max_block_wait_seconds)
    except BotCheckFailed as error:
        if str(error) != "blocked":
            raise
        log("Yelp blocked this Chrome profile, wiping it and trying once more with a fresh one")
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        scrape_with_browser(state, channel, max_block_wait_seconds)


def describe_area(run):
    if run.get("radius_miles"):
        return f"within {run['radius_miles']:g} miles of {run['location']}"
    return f"near {run['location']}"


def build_results(tiles, exact_counts):
    rows = []
    seen = set()
    for card in organic_cards(tiles):
        path = business_path(card["href"])
        if path in seen:
            continue
        seen.add(path)
        approximate = parse_review_count(card["review_count_raw"])
        abbreviated = (card["review_count_raw"] or "").endswith("k")
        rows.append(
            {
                "url": f"{YELP}{path}",
                "name": card["name"],
                "rating": card["rating"],
                "review_count": exact_counts.get(path, approximate),
                "review_count_is_exact": path in exact_counts or not abbreviated,
                "price": card["price"],
                "distance_miles": card["distance_miles"],
                "categories": ", ".join(card["categories"]),
            }
        )
    rows.sort(key=lambda row: (row["rating"] or 0, row["review_count"] or 0), reverse=True)
    df = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    df["review_count"] = df["review_count"].astype("Int64")
    return df


def plot_ratings_by_price(df, title, output_path):
    priced = df[df["price"].fillna("").str.len() > 0].copy()
    if priced.empty:
        return False
    priced["price_category"] = priced["price"].map(PRICE_LEVELS)
    plt.figure(figsize=(10, 6))
    sns.boxplot(data=priced, x="price_category", y="rating", order=list(PRICE_LEVELS.values()))
    plt.title(title, fontsize=14)
    plt.xlabel("Price Level", fontsize=12)
    plt.ylabel("Rating", fontsize=12)
    plt.grid(True, axis="y", linestyle="--", alpha=0.7)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    return True


def print_summary(df, run, out_dir):
    print(f"{run['what']} {describe_area(run)} ({run['page_title']})")
    print(f"results: {len(df)} from {len(run['tiles'])} search tiles, exact review counts: {int(df['review_count_is_exact'].sum())} of {len(df)}")
    print(f"saved to {out_dir}: results.csv, ratings_by_price.png, search_pages.json")
    print()
    print(df.head(15)[["name", "rating", "review_count", "price", "categories"]].to_string(index=False))
    priced = df[df["price"].fillna("").str.len() > 0]
    if not priced.empty:
        print()
        grouped = priced.groupby(priced["price"].map(PRICE_LEVELS))["rating"].agg(["count", "median", "mean"])
        print(grouped.reindex(list(PRICE_LEVELS.values())).dropna(how="all").round(2))


def main():
    parser = argparse.ArgumentParser(description="Pull every place Yelp lists for a search term around an address or city")
    parser.add_argument("location", help="an address or a city, e.g. '682 MacArthur Dr, Daly City' or 'Santa Barbara, CA'")
    parser.add_argument("--what", default="Restaurants", help="Yelp search term (default: Restaurants)")
    parser.add_argument(
        "--radius",
        type=float,
        default=DEFAULT_RADIUS_MILES,
        help=f"miles from the location to search, 0 for Yelp's own area and its {YELP_RESULT_CAP} result cap (default: {DEFAULT_RADIUS_MILES})",
    )
    parser.add_argument("--out-dir", type=Path, help="output folder (default: results/<location slug> in the repo)")
    parser.add_argument("--channel", default="chrome", help="Playwright browser channel: chrome, msedge, or chromium")
    parser.add_argument(
        "--max-block-wait",
        type=float,
        default=DEFAULT_MAX_BLOCK_WAIT_MINUTES,
        help=f"minutes to keep waiting for Yelp's bot check to clear before giving up (default: {DEFAULT_MAX_BLOCK_WAIT_MINUTES})",
    )
    parser.add_argument("--fresh", action="store_true", help="ignore an unfinished run saved in the output folder instead of resuming it")
    parser.add_argument("--rebuild", action="store_true", help="rebuild results.csv and the plot from the saved search_pages.json")
    args = parser.parse_args()

    out_dir = args.out_dir or REPO_ROOT / "results" / slugify(args.location)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages_file = out_dir / "search_pages.json"

    if args.rebuild:
        run = load_run(pages_file)
    else:
        run = None if args.fresh else load_unfinished_run(pages_file, args.what, args.location, args.radius)
        state = RunState(run or new_run(args.what, args.location, args.radius), pages_file)
        try:
            scrape(state, args.channel, args.max_block_wait * 60)
        except BotCheckFailed as error:
            state.save()
            sys.exit(f"{error}. Progress is saved in {pages_file}, run the same command again to resume")
        run = state.run

    df = build_results(run["tiles"], run["exact_counts"])
    df.to_csv(out_dir / "results.csv", index=False)
    plot_ratings_by_price(df, f"{run['what']} Ratings by Price Level {describe_area(run)}", out_dir / "ratings_by_price.png")
    print_summary(df, run, out_dir)


if __name__ == "__main__":
    main()
