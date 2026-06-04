import os
import time
import json
import yaml
import random
import re
from google import genai
from openai import OpenAI
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import stealth_sync

with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")

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
            return json.load(f)
    return []


def save_applied(applied_ids):
    with open("applied.json", "w") as f:
        json.dump(applied_ids, f)


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


def simulate_mouse_movement(page):
    """Simulates random human-like mouse movements"""
    x = random.randint(100, 800)
    y = random.randint(100, 800)
    page.mouse.move(x, y, steps=random.randint(5, 15))
    human_delay(0.5, 1.5)


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
    target_resume_title = profile["resume_title"]

    url = f"https://hh.ru/resume/{resume_id}/similar_vacancies"
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    check_for_captcha(page)

    try:
        page.wait_for_selector('[data-qa="vacancy-serp__vacancy"]', timeout=15000)
    except PlaywrightTimeout:
        print(f"[ERROR] Элементы вакансий не появились в DOM. Сохраняю debug.png")
        page.screenshot(path="debug.png")
        return

    for _ in range(random.randint(2, 4)):
        page.mouse.wheel(0, random.randint(1000, 2500))
        human_delay(1, 3)

    vacancy_elements = page.locator('[data-qa="vacancy-serp__vacancy"]').all()
    vacancies_data = []

    for el in vacancy_elements:
        try:
            title_el = el.locator('[data-qa="vacancy-serp__vacancy-title"]')
            link = title_el.get_attribute("href")
            vid_match = re.search(r"/vacancy/(\d+)", link)
            if not vid_match:
                continue
            vid = vid_match.group(1)

            if vid in applied:
                continue

            stats_text = el.inner_text()
            app_count = extract_applicant_count(stats_text)

            vacancies_data.append({"id": vid, "link": link, "app_count": app_count})
        except Exception:
            continue

    vacancies_data.sort(key=lambda x: x["app_count"])
    print(f"Found {len(vacancies_data)} new vacancies.")

    for vac in vacancies_data:
        vid = vac["id"]
        print(f"Processing: {vid} (Applicants: {vac['app_count']})")

        page.goto(vac["link"], timeout=60000, wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        check_for_captcha(page)
        simulate_mouse_movement(page)

        try:
            desc_el = page.locator('[data-qa="vacancy-description"]')
            if not desc_el.is_visible():
                print(f"[WARN] Description not found for {vid}")
                continue
            desc = desc_el.inner_text()

            if not evaluate_vacancy(desc, profile.get("strict_requirements", "")):
                print(f"[-] Rejected by LLM filter: {vid}")
                applied.append(vid)
                save_applied(applied)
                continue

            print(f"[+] Accepted by LLM filter. Generating cover letter...")
            cover_letter = generate_cover_letter(resume_text, desc)

            # Locate apply button (can be 'a' or 'button')
            apply_btn = page.locator('css=[data-qa="vacancy-response-link-top"]')
            if not apply_btn.is_visible():
                print(f"[WARN] Apply button not found: {vid}")
                applied.append(vid)
                save_applied(applied)
                continue

            simulate_mouse_movement(page)
            apply_btn.click()
            human_delay(2, 4)

            resume_select_btn = page.locator('[data-qa="resume-selector"]')
            if resume_select_btn.is_visible():
                resume_select_btn.click()
                human_delay(1, 2)
                page.locator(f'text="{target_resume_title}"').click()
                human_delay(1, 2)

            add_letter_btn = page.locator('[data-qa="vacancy-response-letter-toggle"]')
            if add_letter_btn.is_visible():
                add_letter_btn.click()
                human_delay(1, 2)

            letter_input = page.locator(
                '[data-qa="vacancy-response-popup-form-letter-input"]'
            )
            if letter_input.is_visible():
                # Simulate pasting text instead of instant DOM manipulation
                letter_input.click()
                page.keyboard.insert_text(cover_letter)
                human_delay(2, 4)

            submit_btn = page.locator('[data-qa="vacancy-response-submit-popup"]')
            if submit_btn.is_visible():
                simulate_mouse_movement(page)
                submit_btn.click()
                print(f"[SUCCESS] Applied to {vid}")
                applied.append(vid)
                save_applied(applied)
            else:
                print(f"[ERROR] Submit button not found in modal for {vid}")

            human_delay(7, 15)  # Strict anti-ban delay

        except PlaywrightTimeout:
            print(f"[ERROR] Timeout while processing {vid}")
        except Exception as e:
            print(f"[ERROR] Exception during processing {vid}: {e}")


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
        stealth_sync(page)  # Apply stealth patches

        for profile in config.get("profiles", []):
            process_profile(page, profile, applied)

        browser.close()


if __name__ == "__main__":
    main()
