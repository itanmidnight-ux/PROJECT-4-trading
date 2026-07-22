#!/usr/bin/env python3
"""
driver.py - minimal chromium-cli-alike REPL for driving the XAUUSD Scalper
dashboard from a headless container (chromium-cli itself isn't installed
here; this is the fallback the run-skill-generator's playwright.md example
tells you to build when that's the case).

Usage:
    .venv-driver/bin/python driver.py --session app <<'EOF'
    nav http://127.0.0.1:9001
    wait-for text=XAUUSD
    screenshot
    click text=Backtesting
    screenshot backtest_tab
    console --errors
    EOF

Commands (one per line, space-separated args):
    nav <url>                    goto a URL, waits for load
    wait-for text=<substring>    poll (10s) until page text contains substring
    wait-for <css-selector>      poll (10s) until selector is visible
    screenshot [name]            save PNG to sessions/<session>/screenshots/
    click <css-selector>         click first match
    click text=<substring>       click first element containing text
    fill <css-selector> <text>   fill an input (rest of line is the value)
    press <key>                  press a key (Enter, Tab, ...) on the page
    eval <js-expression>         run JS in page context, print the result
    get-text <css-selector>      print element.innerText
    sleep <ms>                   raw wait, use sparingly
    console --errors             print any console/page errors seen so far
    quit                         close the browser and exit

Screenshots land in sessions/<session>/screenshots/<NNN>-<name>.png; the
latest is also symlinked as screenshot.png.
"""
from __future__ import annotations

import argparse
import shlex
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="default")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    shots_dir = HERE / "sessions" / args.session / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    shot_count = 0
    console_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.on(
            "console",
            lambda msg: console_errors.append(f"[console:{msg.type}] {msg.text}")
            if msg.type == "error"
            else None,
        )
        page.on("pageerror", lambda exc: console_errors.append(f"[pageerror] {exc}"))

        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                parts = shlex.split(line)
            except ValueError as exc:
                print(f"! parse error: {exc}")
                continue
            cmd, rest = parts[0], parts[1:]
            try:
                if cmd == "nav":
                    page.goto(rest[0], wait_until="load", timeout=30000)
                    print(f"> nav {rest[0]} -> {page.title()!r}")
                elif cmd == "wait-for":
                    target = rest[0]
                    if target.startswith("text="):
                        needle = target[len("text=") :]
                        page.wait_for_function(
                            "needle => document.body.innerText.includes(needle)",
                            arg=needle,
                            timeout=10000,
                        )
                    else:
                        page.wait_for_selector(target, timeout=10000, state="visible")
                    print(f"> wait-for {target} ok")
                elif cmd == "screenshot":
                    shot_count += 1
                    name = rest[0] if rest else "shot"
                    fname = f"{shot_count:03d}-{name}.png"
                    dest = shots_dir / fname
                    page.screenshot(path=str(dest), full_page=True)
                    link = shots_dir / "screenshot.png"
                    if link.exists() or link.is_symlink():
                        link.unlink()
                    link.symlink_to(fname)
                    print(f"> screenshot {dest}")
                elif cmd == "click":
                    target = rest[0]
                    if target.startswith("text="):
                        page.get_by_text(target[len("text=") :]).first.click()
                    else:
                        page.click(target, timeout=10000)
                    print(f"> click {target} ok")
                elif cmd == "fill":
                    selector, value = rest[0], " ".join(rest[1:])
                    page.fill(selector, value, timeout=10000)
                    print(f"> fill {selector} = {value!r}")
                elif cmd == "press":
                    page.keyboard.press(rest[0])
                    print(f"> press {rest[0]}")
                elif cmd == "eval":
                    result = page.evaluate(" ".join(rest))
                    print(f"> eval -> {result!r}")
                elif cmd == "get-text":
                    text = page.inner_text(rest[0])
                    print(f"> get-text {rest[0]} -> {text!r}")
                elif cmd == "sleep":
                    time.sleep(int(rest[0]) / 1000.0)
                elif cmd == "console":
                    if console_errors:
                        print("\n".join(console_errors))
                    else:
                        print("> no console/page errors seen")
                elif cmd == "quit":
                    break
                else:
                    print(f"! unknown command: {cmd}")
            except Exception as exc:  # noqa: BLE001 - REPL: report and keep going
                print(f"! {cmd} failed: {exc}")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
