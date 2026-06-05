import os
import time
import json
import yaml
import random
import re
import httpx
from google import genai
from openai import OpenAI
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import stealth_sync

# The configuration file is loaded and environment variables are interpolated.
with open("config.yaml", "r") as f:
    config_content = f.read()
    config_content = os.path.expandvars(config_content)
    config = yaml.safe_load(config_content)

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# The proxy environment variables are configured globally if GLUETUN_PROXY is provided.
gluetun_proxy = os.environ.get("GLUETUN_PROXY")
if gluetun_proxy:
    os.environ["HTTP_PROXY"] = gluetun_proxy
    os.environ["HTTPS_PROXY"] = gluetun_proxy

gemini_client = genai.Client(api_key=GEMINI_KEY)

# The OpenRouter client is initialized with an HTTP client that bypasses the global environment proxies.
openrouter_client = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_KEY,
        http_client=httpx.Client(trust_env=False),
    )
    if OPENROUTER_KEY
    else None
)


def load_applied():
    # The state database of processed vacancy IDs is loaded.
    if os.path.exists("applied.json"):
        with open("applied.json", "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []


def save_applied(applied_ids):
    # The state database of processed vacancy IDs is saved.
    with open("applied.json", "w") as f:
        json.dump(applied_ids, f)


def send_telegram_notification(profile_name, title, link, cover_letter):
    # Vacancy details and the generated cover letter are transmitted to Telegram.
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram configuration missing in environment.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    text = (
        f"📌 <b>New vacancy: {title}</b>\n"
        f"👤 Profile: <code>{profile_name}</code>\n"
        f"🔗 Link: {link}\n\n"
        f"📝 <b>Cover letter:</b>\n"
        f"<code>{cover_letter}</code>"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        # A direct connection is established to avoid VPN blocks by ignoring system environment proxies.
        with httpx.Client(trust_env=False) as client:
            response = client.post(url, json=payload, timeout=10.0)
            if response.status_code == 200:
                print(f"[TG] Notification sent for vacancy {link}")
                return True
            else:
                print(
                    f"[TG ERROR] Failed to send: {response.status_code} - {response.text}"
                )
                return False
    except Exception as e:
        print(f"[TG ERROR] Exception sending to Telegram: {e}")
        return False


def call_llm(prompt, system_instruction=None):
    # The LLM is queried to process prompts with a fallback mechanism.
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"[WARN] Gemini failed: {e}. Falling back to OpenRouter...")
        if not openrouter_client:
            raise Exception("OpenRouter client not configured.")

        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        # The free model router is utilized as a fallback.
        response = openrouter_client.chat.completions.create(
            model="openrouter/free",
            messages=messages,
        )
        return response.choices[0].message.content.strip()


def evaluate_vacancy(vacancy_desc, requirements):
    # The vacancy description is validated against strict criteria.
    prompt = f"""
    Analyze the following vacancy description.
    Vacancy: {vacancy_desc}
    Strict Requirements: {requirements}
    Does the vacancy meet ALL strict requirements? 
    Reply ONLY with 'YES' or 'NO'. No other text.
    """
    result = call_llm(prompt).upper()
    return "YES" in result


def generate_cover_letter(resume_text, vacancy_desc):
    # A professional cover letter is drafted using the resume text.
    prompt = f"""
    Write a cover letter for this vacancy based on my resume.
    Resume: {resume_text}
    Vacancy: {vacancy_desc}
    Rules:
    - Strict, professional tone.
    - Highlight relevant experience.
    - Max 3 short paragraphs.
    - Ready to send, no placeholders.
    """
    return call_llm(prompt)


def human_delay(min_sec=2.0, max_sec=5.0):
    # A pseudo-random pause is introduced to mimic human behavior.
    time.sleep(random.uniform(min_sec, max_sec))


def extract_applicant_count(text):
    # Numbers are extracted from text elements using regex.
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else 0


def check_for_captcha(page):
    # Captcha pages are detected to prevent account blocks.
    if "captcha" in page.url.lower():
        print("[CRITICAL] Captcha detected. Stopping execution to prevent ban.")
        exit(1)


def process_profile(page, profile, applied):
    # Profiles are sequentially processed to evaluate relevant vacancies.
    print(f"--- Starting profile: {profile['name']} ---")

    resume_file = profile.get("resume_file")
    print(f"[DEBUG] Attempting to read resume file: {resume_file}")
    try:
        with open(resume_file, "r") as f:
            resume_text = f.read()
        print(f"[DEBUG] Resume file successfully read ({len(resume_text)} characters).")
    except Exception as e:
        print(f"[DEBUG ERROR] Error reading resume file: {e}")
        return

    resume_id = profile.get("resume_id")
    print(f"[DEBUG] Received resume_id: '{resume_id}'")
    if not resume_id or resume_id == "None" or "$" in str(resume_id):
        print(f"[DEBUG ERROR] resume_id is invalid or failed to parse from env.")
        return

    url = f"https://hh.ru/search/vacancy?resume={resume_id}"
    print(f"[DEBUG] Navigating to URL: {url}")

    try:
        # The page navigation is attempted with a direct timeout of 20 seconds.
        page.goto(url, timeout=20000, wait_until="domcontentloaded")
        print("[DEBUG] Navigation successful. Checking for captcha...")
    except Exception as e:
        print(f"[DEBUG ERROR] Error or timeout during page.goto: {e}")
        try:
            page.screenshot(path="debug_goto_error.png")
            print("[DEBUG] Navigation error screenshot saved.")
        except Exception as se:
            print(f"[DEBUG ERROR] Failed to capture screenshot: {se}")
        return

    check_for_captcha(page)
    print("[DEBUG] Captcha not detected. Waiting for vacancy selector...")

    try:
        page.wait_for_selector('[data-qa="vacancy-serp__vacancy"]', timeout=10000)
        print("[DEBUG] Vacancy selector found.")
    except PlaywrightTimeout:
        print(f"[ERROR] Vacancies not found on the page. Saving debug.png")
        page.screenshot(path="debug.png")
        return
    except Exception as e:
        print(f"[DEBUG ERROR] Unexpected error waiting for selector: {e}")
        return

    print("[DEBUG] Simulating page scrolling...")
    for i in range(random.randint(2, 4)):
        print(f"[DEBUG] Scrolling step {i+1}...")
        page.mouse.wheel(0, random.randint(1000, 2500))
        human_delay(1, 3)

    print("[DEBUG] Collecting vacancy elements...")
    vacancy_elements = page.locator('[data-qa="vacancy-serp__vacancy"]').all()
    print(f"[DEBUG] Elements found on the page: {len(vacancy_elements)}")

    vacancies_data = []
    for el in vacancy_elements:
        try:
            # Current HeadHunter selectors are used to extract information.
            title_el = el.locator('[data-qa="serp-item__title-text"]').first
            title_text = title_el.inner_text(timeout=2000).strip()

            link_el = el.locator('a[data-qa="serp-item__title"]').first
            link = link_el.get_attribute("href", timeout=2000)

            vid_match = re.search(r"/vacancy/(\d+)", link)
            if not vid_match:
                continue
            vid = vid_match.group(1)

            if vid in applied:
                continue

            stats_text = el.inner_text()
            app_count = extract_applicant_count(stats_text)

            vacancies_data.append(
                {
                    "id": vid,
                    "title": title_text,
                    "link": f"https://hh.ru/vacancy/{vid}",
                    "app_count": app_count,
                }
            )
        except Exception as e:
            # Errors are logged to the console without interrupting execution.
            print(f"[DEBUG ERROR] Error parsing individual card: {e}")
            continue

    vacancies_data.sort(key=lambda x: x["app_count"])
    print(f"Found {len(vacancies_data)} new vacancies to evaluate.")

    for vac in vacancies_data:
        vid = vac["id"]
        print(f"Evaluating: {vac['title']} ({vid}) (Applicants: {vac['app_count']})")

        page.goto(vac["link"], timeout=60000, wait_until="domcontentloaded")
        check_for_captcha(page)
        human_delay(1, 3)

        try:
            desc_el = page.locator('[data-qa="vacancy-description"]')
            if not desc_el.is_visible():
                print(f"[WARN] Description element not found for {vid}")
                continue
            desc = desc_el.inner_text()

            # The vacancy description is checked for validity.
            if not evaluate_vacancy(desc, profile.get("strict_requirements", "")):
                print(f"[-] Rejected by LLM filter: {vid}")
                applied.append(vid)
                save_applied(applied)
                continue

            print(f"[+] Accepted by LLM. Generating cover letter...")

            cover_letter = generate_cover_letter(resume_text, desc)

            # The Telegram notification is dispatched.
            if send_telegram_notification(
                profile["name"], vac["title"], vac["link"], cover_letter
            ):
                applied.append(vid)
                save_applied(applied)

            human_delay(3, 7)

        except PlaywrightTimeout:
            print(f"[ERROR] Timeout while loading vacancy {vid}")
        except Exception as e:
            print(f"[ERROR] Exception processing {vid}: {e}")


def main():
    # The main loop coordinates browser initialization and profile processing.
    applied = load_applied()

    # The proxy environment variables are temporarily removed from the parent process environment.
    # This prevents the Playwright driver and the spawned Chromium subprocess from inheriting them.
    http_proxy_backup = os.environ.pop("HTTP_PROXY", None)
    https_proxy_backup = os.environ.pop("HTTPS_PROXY", None)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                # The Chromium instance is forced to bypass system proxies to prevent blocking.
                "--no-proxy-server",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        # The proxy environment variables are restored in the Python process so that
        # the lazy-loaded Gemini client can utilize them for API requests.
        if http_proxy_backup:
            os.environ["HTTP_PROXY"] = http_proxy_backup
        if https_proxy_backup:
            os.environ["HTTPS_PROXY"] = https_proxy_backup

        if not os.path.exists("state.json"):
            raise FileNotFoundError("state.json not found. Run auth_setup.py first.")

        context = browser.new_context(
            storage_state="state.json",
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )

        page = context.new_page()
        stealth_sync(page)

        for profile in config.get("profiles", []):
            process_profile(page, profile, applied)

        browser.close()


if __name__ == "__main__":
    main()
