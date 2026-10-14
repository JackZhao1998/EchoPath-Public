"""Re-run the case EchoPath actually failed on, with and without the Jev rung.

EchoPath learned "search a product, open the first result, add it to the cart"
and then could not replay it -- not even for the same search term -- because the
step it had to repeat was a click on a product tile, and it had memorised a
picture of the tile from the day it learned. Amazon rotates its sponsored
listings, the picture stopped matching, and replay stopped.

This asks whether reading the page as text and letting a decision model pick the
row gets past that step. Success is not a string comparison: it is whether the
cart count went up, which is true on any storefront and cannot be fudged by a
regex that happens to fit.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from typing import List, Optional, Tuple

import pyautogui

from echopath.project_paths import project_root
from echopath.jev_client import JevClient
from echopath.ocr_extractor import Row, front_window_region, read_screen

ROOT = project_root()

BROWSER = "Google Chrome"
CART_PATTERN = re.compile(r"^\s*(\d{1,2})\s*$")


def run_osa(script: str) -> str:
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return result.stdout.strip()


def open_page(url: str, *, fresh: bool = False) -> None:
    if fresh:
        run_osa(f'tell application "{BROWSER}" to quit')
        time.sleep(2.0)
        subprocess.Popen(["open", "-na", BROWSER, "--args", "--incognito", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(9.0)
    else:
        run_osa(f'tell application "{BROWSER}" to set URL of active tab of front window to "{url}"')
        time.sleep(5.0)
    run_osa(f'tell application "{BROWSER}" to activate')
    run_osa(f'tell application "{BROWSER}" to set bounds of front window to {{0, 25, 1800, 1100}}')
    time.sleep(1.5)


def cart_count() -> int:
    """Read the cart badge.

    The objective signal: whatever was clicked, the cart either grew or it did
    not. No string matching against product names, which is what let sidebar
    junk score as a success last time.
    """
    screen = read_screen(region=front_window_region(BROWSER))
    top_right = [
        r for r in screen.regions
        if r.y < screen.height * 0.18 and r.x > screen.width * 0.72
    ]
    for region in sorted(top_right, key=lambda r: -r.x):
        match = CART_PATTERN.match(region.text)
        if match:
            return int(match.group(1))
    return 0


def product_rows(min_y_fraction: float = 0.2) -> Tuple[List[Row], int]:
    screen = read_screen(region=front_window_region(BROWSER))
    rows = screen.rows_below(int(screen.height * min_y_fraction), min_regions=1)
    return rows, screen.ocr_ms


def click(point: Tuple[int, int]) -> None:
    pyautogui.moveTo(point[0], point[1], duration=0.2)
    time.sleep(0.2)
    pyautogui.click()
    time.sleep(3.5)


def attempt(query: str, client: Optional[JevClient]) -> dict:
    """One product: search, choose a row, open it, try to add it to the cart."""
    record = {"query": query, "mode": "ocr+jev" if client else "vanilla-replay",
              "cart_before": 0, "cart_after": 0, "added": False,
              "confidence": None, "picked": "", "cost": 0.0, "ms": 0, "note": ""}
    started = time.perf_counter()

    open_page(f"https://www.amazon.com/s?k={query.replace(' ', '+')}")
    record["cart_before"] = cart_count()

    rows, _ocr_ms = product_rows()
    if not rows:
        record["note"] = "no rows read from page"
        record["ms"] = int((time.perf_counter() - started) * 1000)
        return record

    if client is None:
        # What replay does today: go to the remembered spot. The memory was
        # built on a different product, so this is the first row on the page,
        # which on Amazon is reliably a filter or a sponsored banner.
        target = rows[0]
        record["note"] = "took the remembered position"
    else:
        offered = rows[:60]
        options = {r.key: f"position {i+1}: {r.text[:110]}"
                   for i, r in enumerate(offered)}
        decision = client.choose(
            instructions=(
                f"Which row is a real product listing for '{query}' that can be "
                "opened and bought? Ignore filters, sidebar options, sponsored "
                "banners, navigation and price-only fragments."
            ),
            options=options,
            state={"page": f"Amazon search results for {query}"},
        )
        target = next((r for r in offered if r.key == decision.choice), offered[0])
        record["confidence"] = decision.confidence
        record["cost"] = decision.cost
        record["note"] = f"jev picked at {decision.confidence:.2f}"

    record["picked"] = target.text[:90]
    click(target.center)

    # On the product page, find and press Add to Cart.
    rows_now, _ = product_rows(min_y_fraction=0.0)
    add = next((r for r in rows_now
                if re.search(r"add to cart", r.text, re.I)), None)
    if add is None:
        record["note"] += " | no Add to Cart on the opened page"
    else:
        click(add.center)
        record["note"] += " | pressed Add to Cart"

    record["cart_after"] = cart_count()
    record["added"] = record["cart_after"] > record["cart_before"]
    record["ms"] = int((time.perf_counter() - started) * 1000)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", nargs="*",
                        default=["paper towels", "coffee filters", "aa batteries"])
    args = parser.parse_args()

    client = JevClient.from_credentials(ROOT / "credentials" / "api_keys.yaml")
    results = []
    first = True
    for query in args.queries:
        for mode_client in (None, client):
            open_page("https://www.amazon.com", fresh=first)
            first = False
            record = attempt(query, mode_client)
            results.append(record)
            flag = "ADDED" if record["added"] else "  -  "
            print(f"{flag} {record['mode']:14} {query:15} "
                  f"cart {record['cart_before']}->{record['cart_after']}  "
                  f"{record['ms']:6}ms  conf={record['confidence']}")
            print(f"      picked: {record['picked'][:80]}")
            print(f"      {record['note']}")

    out = ROOT / "local_data" / "eval" / "failure_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    for mode in ("vanilla-replay", "ocr+jev"):
        rows = [r for r in results if r["mode"] == mode]
        if rows:
            added = sum(1 for r in rows if r["added"])
            print(f"{mode:16} added to cart {added}/{len(rows)}   "
                  f"avg {sum(r['ms'] for r in rows)//len(rows)}ms   "
                  f"${sum(r['cost'] for r in rows):.5f}")
    print(f"\nsaved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
