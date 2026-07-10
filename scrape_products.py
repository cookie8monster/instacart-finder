"""
Instacart Product Scraper — Purely Elizabeth
============================================
Intercepts Instacart's internal GraphQL 'Items' calls while scrolling
through the cross-retailer search page, extracts PE product data,
and saves a timestamped CSV.

Setup (one-time):
    pip install playwright
    python3 -m playwright install chromium

Run:
    python3 scrape_products.py

If Instacart blocks you (CAPTCHA / blank page), set USE_SCRAPERAPI = True.
"""

import asyncio
import csv
import json
import re
from datetime import datetime
from playwright.async_api import async_playwright

# ── Config ────────────────────────────────────────────────────────────────────
SEARCH_URL     = "https://www.instacart.com/store/s?k=Purely+Elizabeth"
SCRAPERAPI_KEY = "283f31e82e295ab278a6efb498c9c0b4"
USE_SCRAPERAPI = False   # flip to True if you hit a CAPTCHA or bot block
MAX_SCROLLS    = 25
SCROLL_PAUSE   = 2200    # ms between scrolls
# ─────────────────────────────────────────────────────────────────────────────

products: dict[str, dict] = {}   # keyed by productId


def extract_price(item: dict) -> tuple[str, str]:
    """Return (current_price, original_price) strings from a GraphQL item dict."""
    price_str  = ""
    orig_str   = ""
    # Price lives in the sibling ItemPrice node; here we dig tracking_properties
    tp = item.get("trackingProperties") or {}
    if isinstance(tp, dict):
        price_str = tp.get("price", "")
    # Also try direct field
    if not price_str:
        price_str = item.get("displayPrice") or item.get("priceString") or ""
    return price_str, orig_str


def process_item(item: dict, retailer_name: str = ""):
    """Extract fields from a single GraphQL item node."""
    pid = str(item.get("productId") or "")
    if not pid:
        return
    name = item.get("name") or item.get("display_name") or ""
    if "purely elizabeth" not in name.lower():
        return

    price, orig = extract_price(item)
    evergreen   = item.get("evergreenUrl") or ""
    product_url = f"https://www.instacart.com/products/{evergreen}" if evergreen else f"https://www.instacart.com/products/{pid}"

    entry = {
        "instacart_product_id": pid,
        "item_compound_id":     item.get("id", ""),          # e.g. items_25677-75086264
        "name":                 name,
        "brand":                item.get("brandName") or "Purely Elizabeth",
        "size":                 item.get("size") or "",
        "price":                price,
        "retailer":             retailer_name,
        "product_url":          product_url,
        "evergreen_url":        evergreen,
    }

    # Keep entry with most data if we see same product from multiple retailers
    existing = products.get(pid)
    if not existing or (not existing["price"] and price):
        products[pid] = entry


def walk_json(data, retailer_name=""):
    """Recursively walk GraphQL JSON to find item nodes."""
    if isinstance(data, dict):
        # Instacart item nodes always have productId + name together
        if data.get("productId") and data.get("name"):
            process_item(data, retailer_name)
        # Peek for retailer name clue
        rname = data.get("retailerName") or retailer_name
        for v in data.values():
            if isinstance(v, (dict, list)):
                walk_json(v, rname)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                walk_json(item, retailer_name)


def dom_fallback(html: str):
    """Pull any PE product IDs we missed from rendered href links."""
    before = len(products)
    for pid, slug in re.findall(r'/products/(\d+)-(purely-elizabeth[a-z0-9-]*)', html):
        if pid not in products:
            name = slug.replace("-", " ").title()
            products[pid] = {
                "instacart_product_id": pid,
                "item_compound_id":     "",
                "name":                 name,
                "brand":                "Purely Elizabeth",
                "size":                 "",
                "price":                "",
                "retailer":             "(from DOM)",
                "product_url":          f"https://www.instacart.com/products/{pid}",
                "evergreen_url":        f"{pid}-{slug}",
            }
    added = len(products) - before
    if added:
        print(f"  DOM fallback: +{added} additional product IDs")


async def run():
    async with async_playwright() as p:
        launch_opts: dict = {
            "headless": False,   # keep visible to spot CAPTCHAs
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        }
        if USE_SCRAPERAPI:
            launch_opts["proxy"] = {
                "server":   "http://proxy-server.scraperapi.com:8001",
                "username": "scraperapi",
                "password": SCRAPERAPI_KEY,
            }

        browser = await p.chromium.launch(**launch_opts)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=USE_SCRAPERAPI,
        )
        page = await context.new_page()

        # ── Intercept GraphQL Items responses ─────────────────────────────────
        async def on_response(response):
            url = response.url
            if "instacart.com/graphql" not in url:
                return
            if "Items" not in url and "Search" not in url:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await response.json()
                before = len(products)
                walk_json(body)
                found = len(products) - before
                if found:
                    op = re.search(r'operationName=([^&]+)', url)
                    print(f"  +{found} products  [{op.group(1) if op else 'graphql'}]")
            except Exception:
                pass

        page.on("response", on_response)

        # ── Navigate ──────────────────────────────────────────────────────────
        print(f"Opening {SEARCH_URL} …")
        try:
            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"  Warning: {e}")

        print("Waiting for initial products to load…")
        await page.wait_for_timeout(6000)

        # ── Scroll ────────────────────────────────────────────────────────────
        print(f"Scrolling to load more (up to {MAX_SCROLLS} scrolls)…\n")
        prev_count = 0
        stale      = 0
        for i in range(MAX_SCROLLS):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(SCROLL_PAUSE)
            count = len(products)
            print(f"  Scroll {i+1:2d}: {count} PE products so far")
            if count == prev_count:
                stale += 1
                if stale >= 3:
                    print("  No new products in 3 consecutive scrolls — stopping.")
                    break
            else:
                stale = 0
            prev_count = count

        # ── DOM fallback ──────────────────────────────────────────────────────
        html = await page.content()
        dom_fallback(html)

        await browser.close()

    # ── Write CSV ─────────────────────────────────────────────────────────────
    if not products:
        print("\n❌  No products found.")
        print("    Try setting USE_SCRAPERAPI = True, or check for a CAPTCHA in the browser window.")
        return

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_file = f"pe_instacart_products_{ts}.csv"
    fields   = [
        "instacart_product_id", "item_compound_id", "name", "brand",
        "size", "price", "retailer", "product_url", "evergreen_url",
    ]

    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for prod in sorted(products.values(), key=lambda x: x["name"].lower()):
            w.writerow(prod)

    print(f"\n✅  {len(products)} products  →  {csv_file}")

    # Debug dump
    debug_file = f"pe_instacart_debug_{ts}.json"
    with open(debug_file, "w", encoding="utf-8") as f:
        json.dump(list(products.values()), f, indent=2)
    print(f"📄  Full data dump  →  {debug_file}")


if __name__ == "__main__":
    asyncio.run(run())
