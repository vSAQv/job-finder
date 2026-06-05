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

# Load and interpolate environment variables in configuration
with open("config.yaml", "r") as f:
    config_content = f.read()
    config_content = os.path.expandvars(config_content)
    config = yaml.safe_load(config_content)

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

gemini_client = genai.Client(api_key=GEMINI_KEY)

openrouter_client = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_KEY,
    )
    if OPENROUTER_KEY
    else None
)


def load_applied():
    if os.path.exists("applied.json"):
        with open("applied.json", "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []


def save_applied(applied_ids):
    with open("applied.json", "w") as f:
        json.dump(applied_ids, f)


def send_telegram_notification(profile_name, title, link, cover_letter):
    """Sends prepared vacancy details directly to Telegram using direct network (no proxy)"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram configuration missing in environment.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    text = (
        f"📌 <b>Новая вакансия: {title}</b>\n"
        f"👤 Профиль: <code>{profile_name}</code>\n"
        f"🔗 Ссылка: {link}\n\n"
        f"📝 <b>Сопроводительное письмо:</b>\n"
        f"<code>{cover_letter}</code>"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        # Explicitly bypass system proxies for Telegram API to avoid Cloudflare blocks on VPN
        with httpx.Client(proxies={}) as client:
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

        response = openrouter_client.chat.completions.create(
            model="google/gemini-2.5-flash:free",
            messages=messages,
        )
        return response.choices[0].message.content.strip()


def evaluate_vacancy(vacancy_desc, requirements):
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
    time.sleep(random.uniform(min_sec, max_sec))


def extract_applicant_count(text):
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else 0


def check_for_captcha(page):
    if "captcha" in page.url.lower():
        print("[CRITICAL] Captcha detected. Stopping execution to prevent ban.")
        exit(1)


def process_profile(page, profile, applied):
    print(f"--- Starting profile: {profile['name']} ---")

    with open(profile["resume_file"], "r") as f:
        resume_text = f.read()

    resume_id = profile["resume_id"]

    # Use actual frontend search URL by resume
    url = f"https://hh.ru/search/vacancy?resume={resume_id}"
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    check_for_captcha(page)

    try:
        page.wait_for_selector('[data-qa="vacancy-serp__vacancy"]', timeout=15000)
    except PlaywrightTimeout:
        print(f"[ERROR] No vacancies found. Saving debug.png for diagnostics.")
        page.screenshot(path="debug.png")
        return

    # Simulation of human scrolling
    for _ in range(random.randint(2, 4)):
        page.mouse.wheel(0, random.randint(1000, 2500))
        human_delay(1, 3)

    vacancy_elements = page.locator('[data-qa="vacancy-serp__vacancy"]').all()
    vacancies_data = []

    for el in vacancy_elements:
        try:
            title_el = el.locator('[data-qa="vacancy-serp__vacancy-title"]')
            title_text = title_el.inner_text()
            link = title_el.get_attribute("href")
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
        except Exception:
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

            # Step 1: Pre-filtering
            if not evaluate_vacancy(desc, profile.get("strict_requirements", "")):
                print(f"[-] Rejected by LLM filter: {vid}")
                applied.append(vid)
                save_applied(applied)
                continue

            print(f"[+] Accepted by LLM. Generating cover letter...")

            # Step 2: Cover letter generation
            cover_letter = generate_cover_letter(resume_text, desc)

            # Step 3: Telegram notification instead of automated application
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
    applied = load_applied()

    with sync_playwright() as p:
       browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--no-proxy-server",          
                "--disable-dev-shm-usage",   
                "--disable-gpu",            
            ],
        ) 

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
