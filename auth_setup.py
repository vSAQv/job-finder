from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://hh.ru/account/login")

    print(
        "Log in manually, pass captcha. Press ENTER here when you see your profile page."
    )
    input()

    context.storage_state(path="state.json")
    browser.close()
    print("state.json created.")
