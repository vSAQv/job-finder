from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

STATE_FILE = Path(__file__).resolve().with_name("state.json")
LOGIN_URL = "https://hh.ru/account/login"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    try:
        # Do not wait for every image, analytics request, and other resource to finish.
        response = page.goto(LOGIN_URL, wait_until="commit", timeout=60_000)
        if response:
            print(f"HH response: HTTP {response.status}")
    except PlaywrightTimeoutError:
        # Keep the browser open so manual login can still be attempted.
        print("Navigation did not commit within 60 seconds.")
        print(f"Current URL: {page.url}")

    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except PlaywrightTimeoutError:
        print("DOMContentLoaded did not fire in time; check the visible browser window.")

    print("Log in manually and complete any verification.")
    print("When you see your logged-in HH profile page, return here and press ENTER.")

    try:
        input()
        print(f"Current URL: {page.url}")
        print(f"Page title: {page.title()}")
        context.storage_state(path=str(STATE_FILE))
        print(f"Saved browser state to: {STATE_FILE}")
    finally:
        browser.close()
