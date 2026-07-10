"""
Instacart Retailer Verification — Purely Elizabeth
====================================================
Queries the IDP API for retailers across many ZIP codes, then for each
unique retailer_key visits the store's Instacart search page and checks
whether any of our known PE product IDs appear in GraphQL responses.

Outputs:
  verified_retailers.json  — list of {retailer_key, name} objects
  VERIFIED_KEYS constant   — ready to paste into index.html

Setup:
    pip install playwright requests
    python3 -m playwright install chromium

Run:
    python3 verify_retailers.py
"""

import asyncio
import json
import re
import time
import requests
from playwright.async_api import async_playwright

# ── Config ─────────────────────────────────────────────────────────────────────
IDP_KEY  = "keys.VEcDoDj0RNmIni5MffpBh7LoWC24Jk8tXTha5qSgICk"
IDP_BASE = "https://connect.instacart.com/idp/v1"

# ZIP codes spread across US regions to capture all retailer chains
SAMPLE_ZIPS = [
    "10001",  # NYC
    "90001",  # LA
    "60601",  # Chicago
    "77001",  # Houston
    "85001",  # Phoenix
    "19101",  # Philadelphia
    "78201",  # San Antonio
    "92101",  # San Diego
    "75201",  # Dallas
    "95101",  # San Jose
    "30301",  # Atlanta
    "98101",  # Seattle
    "02101",  # Boston
    "80201",  # Denver
    "20001",  # DC
    "33101",  # Miami
    "55401",  # Minneapolis
    "63101",  # St Louis
    "97201",  # Portland
    "84101",  # Salt Lake City
]

# Known PE product IDs from our scraped CSV (2026-07-10)
PE_PRODUCT_IDS = {
    "55766632", "41662372", "41662624", "105690200", "109829841",
    "105690571", "17633601", "17632106", "104771", "74974608",
    "75086124", "135058", "41052867", "27924696", "75086264",
    "75086131", "41662621", "41662623", "41662370", "55766633",
    "55766631", "105690569", "105690572", "109829843", "109829844",
}

SCROLL_PAUSE = 2000   # ms
MAX_SCROLLS  = 5      # enough to see first batch of results
# ──────────────────────────────────────────────────────────────────────────────


def idp_retailers_for_zip(zip_code: str) -> list[dict]:
    """Call IDP /retailers for one ZIP, return list of {name, retailer_key, ...}."""
    try:
        r = requests.get(
            f"{IDP_BASE}/retailers",
            params={"postal_code": zip_code, "country_code": "US"},
            headers={"Authorization": f"Bearer {IDP_KEY}"},
            timeout=15,
        )
        data = r.json()
        return data.get("retailers", [])
    except Exception as e:
        print(f"  IDP error for {zip_code}: {e}")
        return []


def collect_all_retailer_keys() -> dict[str, dict]:
    """Query IDP for all sample ZIPs, de-duplicate by retailer_key."""
    found: dict[str, dict] = {}
    for zip_code in SAMPLE_ZIPS:
        retailers = idp_retailers_for_zip(zip_code)
        new_keys = [r for r in retailers if r.get("retailer_key") and r["retailer_key"] not in found]
        for r in new_keys:
            found[r["retailer_key"]] = {"name": r["name"], "retailer_key": r["retailer_key"]}
        print(f"  ZIP {zip_code}: {len(retailers)} retailers, {len(new_keys)} new  (total {len(found)})")
        time.sleep(0.4)   # gentle rate-limit
    return found


async def check_retailer_carries_pe(page, retailer_key: str, retailer_name: str) -> bool:
    """
    Visit instacart.com/store/{key}/s?k=purely+elizabeth
    Return True if any PE product ID appears in GraphQL response.
    """
    found_pe = False

    async def on_response(response):
        nonlocal found_pe
        if found_pe:
            return
        url = response.url
        if "instacart.com/graphql" not in url:
            return
        if "Items" not in url and "Search" not in url and "search" not in url.lower():
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            body = await response.json()
            body_str = json.dumps(body)
            for pid in PE_PRODUCT_IDS:
                if pid in body_str:
                    found_pe = True
                    return
        except Exception:
            pass

    search_url = f"https://www.instacart.com/store/{retailer_key}/s?k=purely+elizabeth"
    page.on("response", on_response)
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(SCROLL_PAUSE)
        # One scroll to trigger more results
        for _ in range(MAX_SCROLLS):
            if found_pe:
                break
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
    except Exception as e:
        print(f"    ⚠️  {retailer_name} ({retailer_key}): navigation error — {e}")
    finally:
        page.remove_listener("response", on_response)

    return found_pe


async def run():
    print("Step 1: Collecting all retailer keys from IDP across 20 ZIP codes…\n")
    all_retailers = collect_all_retailer_keys()
    print(f"\nFound {len(all_retailers)} unique retailer keys.\n")

    print("Step 2: Checking each retailer's PE search page for product IDs…\n")
    verified: list[dict] = []
    failed:   list[str]  = []

    keys = list(all_retailers.values())

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        # Reuse one page to avoid opening 100 tabs
        page = await context.new_page()

        for i, r in enumerate(keys):
            key  = r["retailer_key"]
            name = r["name"]
            print(f"  [{i+1}/{len(keys)}] {name} ({key}) … ", end="", flush=True)
            carries = await check_retailer_carries_pe(page, key, name)
            if carries:
                print("✅ carries PE")
                verified.append({"retailer_key": key, "name": name})
            else:
                print("✗  no PE found")
                failed.append(key)
            # Brief pause between retailers
            await asyncio.sleep(0.8)

        await browser.close()

    # ── Write outputs ──────────────────────────────────────────────────────────
    with open("verified_retailers.json", "w", encoding="utf-8") as f:
        json.dump(verified, f, indent=2)

    print(f"\n✅  {len(verified)} retailers confirmed to carry PE")
    print(f"✗   {len(failed)} retailers with no PE products\n")
    print("Saved → verified_retailers.json\n")

    # Print the JS constant ready to paste
    key_list = ", ".join(f"'{r['retailer_key']}'" for r in verified)
    print("── Paste this into index.html ────────────────────────────────────────")
    print(f"const VERIFIED_KEYS = new Set([{key_list}]);")
    print("──────────────────────────────────────────────────────────────────────")


if __name__ == "__main__":
    asyncio.run(run())
