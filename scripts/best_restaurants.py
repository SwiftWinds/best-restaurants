import argparse
import json
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
MAX_RESULTS = 240
CARD_SELECTOR = '[data-testid="serp-ia-card"]'
PRICE_LEVELS = {"$": "Low", "$$": "Medium", "$$$": "High", "$$$$": "Very High"}
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
  const heading = nameLink ? nameLink.closest('h3') : null;
  const rankMatch = heading ? heading.textContent.match(/^\s*(\d+)\.\s/) : null;
  const categories = Array.from(card.querySelectorAll('a[href^="/search?find_desc="]')).map((a) => a.textContent.trim()).filter(Boolean);
  return {
    name: nameLink ? nameLink.textContent.trim() : null,
    href: nameLink ? nameLink.getAttribute('href') : null,
    rating: ratingEl ? parseFloat(ratingEl.getAttribute('aria-label')) : null,
    review_count_raw: reviewMatch ? reviewMatch[1] : null,
    price: priceEl ? priceEl.textContent.trim() : '',
    distance_miles: distanceRaw ? parseFloat(distanceRaw) : null,
    rank: rankMatch ? parseInt(rankMatch[1], 10) : null,
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


def search_path(what, location, start):
    return f"/search?find_desc={quote_plus(what)}&find_loc={quote_plus(location)}&sortby=rating&start={start}"


def pause(low=1.2, high=2.2):
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


def open_search(page, search_url, timeout_seconds):
    log(f"Opening {search_url}")
    page.goto(search_url, wait_until="domcontentloaded")
    log("Waiting for Yelp to render results (solve the captcha in the Chrome window if one appears)")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if page.locator(CARD_SELECTOR).count():
            return
        if page.locator("text=You have been blocked").count():
            raise BotCheckFailed("blocked")
        time.sleep(1)
    screenshot = PROFILE_DIR.parent / "last_bot_check.png"
    page.screenshot(path=str(screenshot))
    raise BotCheckFailed(f"Yelp never rendered results, screenshot at {screenshot}")


def fetch_in_page(page, js, path, search_url, attempts=3):
    for attempt in range(1, attempts + 1):
        result = page.evaluate(js, [path])
        if result["status"] == 200:
            return result
        log(f"HTTP {result['status']} for {path}, attempt {attempt} of {attempts}")
        pause(4, 8)
        if result["status"] in (403, 429):
            open_search(page, search_url, 300)
    raise RuntimeError(f"Yelp kept refusing {path}")


def fetch_search_pages(page, what, location, search_url):
    pages = []
    limit = MAX_RESULTS
    start = 0
    while start < limit:
        result = fetch_in_page(page, FETCH_SEARCH_PAGE_JS, search_path(what, location, start), search_url)
        organic = [card for card in result["cards"] if not card["sponsored"]]
        if result["total"]:
            limit = min(limit, result["total"])
        pages.append({"start": start, "total": result["total"], "cards": result["cards"]})
        log(f"start={start}: {len(organic)} organic of {len(result['cards'])} cards, total={result['total']}")
        if not organic:
            break
        start += PAGE_SIZE
        pause()
    return pages


def resolve_exact_counts(page, pages, search_url):
    abbreviated = []
    for page_data in pages:
        for card in page_data["cards"]:
            if card["sponsored"] or not card["href"] or not (card["review_count_raw"] or "").endswith("k"):
                continue
            path = business_path(card["href"])
            if path not in abbreviated:
                abbreviated.append(path)
    exact = {}
    for index, path in enumerate(abbreviated, 1):
        for attempt in range(2):
            result = fetch_in_page(page, FETCH_BUSINESS_JS, path, search_url)
            if result["count"]:
                exact[path] = int(result["count"].replace(",", ""))
                break
            log(f"no review count on {path} (title {result['title'][:80]!r}), trying again")
            pause(3, 5)
        log(f"exact count {index}/{len(abbreviated)}: {path} -> {exact.get(path)}")
        pause()
    return exact


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


def scrape_with_browser(what, location, channel, search_url):
    with sync_playwright() as playwright:
        context = launch_chrome(playwright, channel)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            open_search(page, search_url, 300)
            page_title = page.title()
            log(f"Yelp page title: {page_title}")
            pages = fetch_search_pages(page, what, location, search_url)
            exact_counts = resolve_exact_counts(page, pages, search_url)
        finally:
            context.close()
    return {
        "location": location,
        "what": what,
        "search_url": search_url,
        "page_title": page_title,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages": pages,
        "exact_counts": exact_counts,
    }


def scrape(what, location, channel):
    search_url = f"{YELP}{search_path(what, location, 0)}"
    try:
        return scrape_with_browser(what, location, channel, search_url)
    except BotCheckFailed as error:
        if str(error) != "blocked":
            raise
        log("Yelp blocked this Chrome profile, wiping it and trying once more with a fresh one")
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        return scrape_with_browser(what, location, channel, search_url)


def build_results(pages, exact_counts):
    rows = []
    seen = set()
    for page_data in pages:
        for card in page_data["cards"]:
            if card["sponsored"] or not card["href"]:
                continue
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
                    "yelp_rank": card["rank"],
                    "categories": ", ".join(card["categories"]),
                }
            )
    rows.sort(key=lambda row: (row["rating"] or 0, row["review_count"] or 0), reverse=True)
    return pd.DataFrame(rows)


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
    print(f"{run['what']} near {run['location']} ({run['page_title']})")
    print(f"results: {len(df)}, exact review counts: {int(df['review_count_is_exact'].sum())} of {len(df)}")
    print(f"saved to {out_dir}: results.csv, ratings_by_price.png, search_pages.json")
    print()
    print(df.head(15)[["name", "rating", "review_count", "price", "distance_miles"]].to_string(index=False))
    priced = df[df["price"].fillna("").str.len() > 0]
    if not priced.empty:
        print()
        grouped = priced.groupby(priced["price"].map(PRICE_LEVELS))["rating"].agg(["count", "median", "mean"])
        print(grouped.reindex(list(PRICE_LEVELS.values())).dropna(how="all").round(2))


def main():
    parser = argparse.ArgumentParser(description="Pull Yelp's full ranked list of places near an address or city")
    parser.add_argument("location", help="an address or a city, e.g. '682 MacArthur Dr, Daly City' or 'Santa Barbara, CA'")
    parser.add_argument("--what", default="Restaurants", help="Yelp search term (default: Restaurants)")
    parser.add_argument("--out-dir", type=Path, help="output folder (default: results/<location slug> in the repo)")
    parser.add_argument("--channel", default="chrome", help="Playwright browser channel: chrome, msedge, or chromium")
    parser.add_argument("--rebuild", action="store_true", help="rebuild results.csv and the plot from the saved search_pages.json")
    args = parser.parse_args()

    out_dir = args.out_dir or REPO_ROOT / "results" / slugify(args.location)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages_file = out_dir / "search_pages.json"

    if args.rebuild:
        run = json.loads(pages_file.read_text())
    else:
        run = scrape(args.what, args.location, args.channel)
        pages_file.write_text(json.dumps(run, indent=1, ensure_ascii=False))

    df = build_results(run["pages"], run["exact_counts"])
    df.to_csv(out_dir / "results.csv", index=False)
    plot_ratings_by_price(df, f"{run['what']} Ratings by Price Level near {run['location']}", out_dir / "ratings_by_price.png")
    print_summary(df, run, out_dir)


if __name__ == "__main__":
    main()
