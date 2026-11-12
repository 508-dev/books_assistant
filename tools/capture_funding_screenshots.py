"""Capture sample UI previews, not ERP runtime verification.

Requires Playwright and an installed Chrome/Chromium. No packages are installed.
Run: python tools/capture_funding_screenshots.py
"""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import shutil

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    browser_path = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not browser_path:
        raise SystemExit("Install Chrome/Chromium before capturing screenshots.")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT)))
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=browser_path)
            page = browser.new_page(viewport={"width": 1440, "height": 1040}, device_scale_factor=1)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            for scenario in ("shortfall", "fronting", "snapshot"):
                page.goto(f"http://127.0.0.1:{server.server_port}/tools/funding_preview.html?scenario={scenario}")
                page.wait_for_function("window.preview_ready === true")
                if errors:
                    raise RuntimeError(errors)
                if scenario != "snapshot":
                    button = page.get_by_role("button", name="Approve and reserve funds")
                    assert button.is_disabled() == (scenario == "shortfall")
                else:
                    assert page.get_by_text("Bank Available Balance", exact=True).is_visible()
                output = ROOT / "docs/screenshots" / f"funding-{scenario}.png"
                page.screenshot(path=str(output), full_page=True)
                print(output.relative_to(ROOT))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    main()
