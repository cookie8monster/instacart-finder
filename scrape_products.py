"""
Instacart Product Scraper — Purely Elizabeth
============================================
Intercepts Instacart's internal API calls while browsing the search page,
extracts all PE product data, and saves to a timestamped CSV.

Setup (one-time):
    pip install playwright
    playwright install chromium

Run:
    python scrape_products.py

If Instacart blocks you, set USE_SCRAPERAPI = True below.
"""

import asyncio
import csv
import json
import re
from datetime import datetime
from playwright.async_api import async_playwright

# ── Config ───────────────────────────────────────────────────────────────────
SEARCH_URL     = "https://www.instacart.com/store/s?k=Purely+Elizabeth"
SCRAPERAPI_KEY = "283f31e82e295ab278a6efb498c9c0b4"
USE_SCRAPERAPI = False   # flip to True if you get blocked / CAPTCHA

MAX_SCROLLS    = 20      # how many times to scroll before stopping
SCROLL_PAUSE   = 2500    # ms between scrolls
# ─────────────────────────────────────────────────────────────────────────────

products: dict[str, dict] = {}   # keyed by instacart_id to auto-dedupe
raw_responses: list       = []   # dump for debugging


def dig(data, *keys, default=""):
    """Safe nested key getter: dig(obj, 'a', 'b', 'c')"""
    for k in keys:
        if isinstance(data, dict):
            data = data.get(k)
        elif isinstance(data, list) and isinstance(k, int):
            data = data[k] if k < len(data) else None
        else:
            return default
        if data is None:
            return default
    return data or default


def extract_from_json(data, source_url=""):
    """Recursively walk JSON looking for Instacart product objects."""
    if isinstance(data, dict):
        # Instacart product objects typically have: id (int), name, size/unit
        pid_raw = data.get("id") or data.get("product_id")
        name    = data.get("name") or data.get("display_name") or ""

        if pid_raw and name:
            pid = str(pid_raw)
            if pid.isdigit() and "purely elizabeth" in name.lower():
                # Image — several possible locations
                img = (
                    data.get("image_url")
                    or data.get("large_image_url")
                    or dig(data, "image", "url")
                    or dig(data, "images", 0, "url")
                    or ""
                )
                # Price
                price = (
                    data.get("display_price")
                    or data.get("price")
                    or dig(data, "pricing", "display_string")
                    or dig(data, "purchasable_selection", "display_string")
                    or ""
                )
                # Size / unit
                size = (
                    data.get("size")
                    or data.get("package_size")
                    or data.get("unit_of_measurement_label")
                    or ""
                )

                products[pid] = {
                    "instacart_id": pid,
                    "name":         name,
                    "brand":        data.get("brand_name") or data.get("brand") or "Purely Elizabeth",
                    "size":         size,
                    "price":        price,
                    "image_url":    img,
                    "product_url":  f"https://www.instacart.com/products/{pid}",
                }

        # Recurse into values
        for v in data.values():
            if isinstance(v, (dict, list)):
                extract_from_json(v, source_url)

    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                extract_from_json(item, source_url)


def extract_from_dom(html: str):
    """
    Fallback: pull product IDs from href links in the rendered HTML.
    Pattern: /products/12345678-product-name
    """
    matches = re.findall(r'/products/(\d+)-([a-z0-9-]+)', html)
    for pid, slug in matches:
        if pid not in products:
            name = slug.replace("-", " ").title()
            if "purely" in name.lower() or "elizabeth" in name.lower() or True:
                products[pid] = {
                    "instacart_id": pid,
                    "name":         name,
                    "brand":        "",
                    "size":         "",
                    "price":        "",
                    "image_url":    "",
                    "product_url":  f"https://www.instacart.com/products/{pid}",
                }


async def run():
    async with async_playwright() as p:
        launch_opts = {
            "headless": False,   # keep visible — easier to spot CAPTCHAs
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

        # ── Intercept JSON responses ─────────────────────────────────────────
        async def on_response(response):
            url = response.url
            if "instacart.com" not in url:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await response.json()
                before = len(products)
                extract_from_json(body, url)
                found = len(products) - before
                if found:
                    print(f"  +{found} products from {url[:80]}")
                raw_responses.append({"url": url, "body": body})
            except Exception:
                pass

        page.on("response", on_response)

        # ── Navigate ─────────────────────────────────────────────────────────
        print(f"Opening {SEARCH_URL} …")
        try:
            await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30_000)
        except Exception:
            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)

        await page.wait_for_timeout(3000)

        # ── Scroll to load more products ─────────────────────────────────────
        print(f"\nScrolling to load more (max {MAX_SCROLLS} scrolls)…")
        prev_count = 0
        stale = 0
        for i in range(MAX_SCROLLS):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(SCROLL_PAUSE)
            count = len(products)
            print(f"  Scroll {i+1:2d}: {count} PE products collected")
            if count == prev_count:
                stale += 1
                if stale >= 3:
                    print("  No new products in 3 scrolls — done.")
                    break
            else:
                stale = 0
            prev_count = count

        # ── DOM fallback ─────────────────────────────────────────────────────
        html = await page.content()
        dom_before = len(products)
        extract_from_dom(html)
        dom_new = len(products) - dom_before
        if dom_new:
            print(f"\n  DOM fallback added {dom_new} additional product IDs")

        await browser.close()

    # ── Save CSV ──────────────────────────────────────────────────────────────
    if not products:
        print("\n❌ No products found. Try setting USE_SCRAPERAPI = True or run with headless=False and check for a CAPTCHA.")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_file  = f"pe_instacart_products_{timestamp}.csv"
    fields    = ["instacart_id", "name", "brand", "size", "price", "image_url", "product_url"]

    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for prod in sorted(products.values(), key=lambda x: x["name"].lower()):
            writer.writerow(prod)

    print(f"\n✅  {len(products)} products saved → {csv_file}")

    # Also dump raw JSON responses for debugging (in case fields need adjusting)
    debug_file = f"pe_instacart_debug_{timestamp}.json"
    with open(debug_file, "w", encoding="utf-8") as f:
        json.dump(raw_responses, f, indent=2, default=str)
    print(f"📄  Raw API responses → {debug_file}  (for debugging field mapping)")


if __name__ == "__main__":
    asyncio.run(run())
