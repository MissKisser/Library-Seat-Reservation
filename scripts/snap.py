"""Capture web panel screenshots for visual regression checks.

Usage:
    python scripts/snap.py [--base-url http://localhost:8080] [--out-dir docs/screenshots]

Requires:
    playwright (already in .venv)
    a running seatbot web server reachable at --base-url.

If the server is unreachable, every page is reported as skipped and the
process exits with a non-zero status so CI can flag the regression gap.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

PAGES = [
    ("dashboard", "/"),
    ("seat-config", "/seat-config"),
    ("accounts", "/accounts"),
    ("coverage", "/coverage"),
    ("tasks", "/tasks"),
    ("seats", "/seats"),
    ("logs", "/logs"),
]

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 375, "height": 812},
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Capture web panel screenshots")
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument("--out-dir", default="docs/screenshots")
    return p.parse_args()


def check_server(base_url: str) -> bool:
    """Best-effort reachability check. A False return never aborts capture
    logic itself — each page request will fail individually and be skipped."""
    try:
        with urlopen(base_url, timeout=3):
            return True
    except (URLError, OSError):
        return False


def status_for(url: str) -> int:
    """Return HTTP status for url, or -1 if unreachable."""
    try:
        with urlopen(url, timeout=3) as resp:
            return resp.status
    except (URLError, OSError):
        return -1


def capture_viewport(
    p, label: str, base_url: str, out_dir: Path
) -> tuple[set[str], set[str]]:
    """Capture all pages for a single viewport label. Returns
    (captured_names, skipped_names)."""
    captured: set[str] = set()
    skipped: set[str] = set()
    browser = p.chromium.launch()
    try:
        context = browser.new_context(viewport=VIEWPORTS[label])
        page = context.new_page()
        for name, route in PAGES:
            url = f"{base_url.rstrip('/')}{route}"
            status = status_for(url)
            if status < 200 or status >= 300:
                skipped.add(name)
                print(f"  [SKIP] {label}/{name}: HTTP {status}")
                continue
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=10000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except PlaywrightTimeoutError:
                    # networkidle may never settle for live/dynamic pages;
                    # proceed with whatever has loaded so far.
                    pass
                out_path = out_dir / label / f"{name}.png"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out_path), full_page=True)
                captured.add(name)
                print(f"  [OK]   {label}/{name}")
            except PlaywrightError as exc:
                skipped.add(name)
                print(f"  [SKIP] {label}/{name}: playwright error: {exc}")
        context.close()
    finally:
        browser.close()
    return captured, skipped


def print_summary(results: dict) -> None:
    print()
    print("=" * 60)
    print(f"{'viewport':<10} {'page':<14} {'status':<10}")
    print("-" * 60)
    for label in ("desktop", "mobile"):
        for name, _ in PAGES:
            if name in results[label]["captured"]:
                status = "captured"
            elif name in results[label]["skipped"]:
                status = "skipped"
            else:
                status = "unknown"
            print(f"{label:<10} {name:<14} {status:<10}")
    print("=" * 60)


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    base_url = args.base_url.rstrip("/")

    print(f"Base URL: {base_url}")
    print(f"Output:   {out_dir}")
    print()

    reachable = check_server(args.base_url)
    if not reachable:
        print(f"WARNING: server at {args.base_url} is unreachable — all pages will be skipped.")

    results: dict = {
        label: {"captured": [], "skipped": []} for label in VIEWPORTS
    }
    overall_fail = False

    with sync_playwright() as p:
        for label in VIEWPORTS:
            print(f"--- {label} ({VIEWPORTS[label]['width']}x{VIEWPORTS[label]['height']}) ---")
            captured, skipped = capture_viewport(p, label, args.base_url, out_dir)
            results[label]["captured"] = captured
            results[label]["skipped"] = skipped
            if skipped:
                overall_fail = True

    print_summary(results)

    manifest = {
        "desktop": sorted(results["desktop"]["captured"]),
        "mobile": sorted(results["mobile"]["captured"]),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, separators=(",", ": ")),
        encoding="utf-8",
    )
    print(f"\nManifest: {manifest_path}")

    total_captured = len(manifest["desktop"]) + len(manifest["mobile"])
    total_possible = 2 * len(PAGES)
    print(f"Captured {total_captured}/{total_possible} screenshots.")

    if overall_fail or total_captured == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
