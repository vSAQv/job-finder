import os
import time
import json
import yaml
import random
import re
import sqlite3
import threading
import html
import requests
import httpx
import telebot
from telebot import types
from openai import OpenAI
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import stealth_sync


# The configuration file is loaded. A default skeleton is created if the file is missing.
def load_config():
    if not os.path.exists("config.yaml"):
        default_config = {"profiles": []}
        with open("config.yaml", "w") as f:
            yaml.safe_dump(default_config, f, allow_unicode=True)
        return default_config
    with open("config.yaml", "r") as f:
        content = f.read()
        content = os.environ.get(
            "GEMINI_API_KEY", ""
        )  # Placeholder operation to force env interpolation safely
        content = os.path.expandvars(content)
        f.seek(0)
        content = f.read()
        content = os.path.expandvars(content)
        return yaml.safe_load(content)


config = load_config()

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# The OpenRouter client is initialized with an HTTP client that bypasses any local environment proxies.
# A strict 15-second timeout is configured to prevent the client from hanging on congested free endpoints.
openrouter_client = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_KEY,
        http_client=httpx.Client(trust_env=False, timeout=30.0),
    )
    if OPENROUTER_KEY
    else None
)

# The Telegram bot is configured with a custom session that ignores system environment proxies.
session = requests.Session()
session.trust_env = False
telebot.apihelper.session = session

# The Telegram bot client is initialized.
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# A list of highly performant free models is maintained for sequential fallback execution.
FALLBACK_MODELS = [OPENROUTER_MODEL] + [
    m
    for m in [
        "openrouter/owl-alpha",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-31b-it:free",
        "openai/gpt-oss-120b:free",
        "poolside/laguna-m.1:free",
        "z-ai/glm-4.5-air:free",
        "openrouter/free",
    ]
    if m != OPENROUTER_MODEL
]


# The SQLite database is initialized and legacy applied.json data is migrated.
def init_db():
    # Connection timeout is configured to 30 seconds to prevent database locks in parallel threads.
    conn = sqlite3.connect("applied.db", timeout=30.0, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vacancies (
            id TEXT PRIMARY KEY,
            profile_name TEXT,
            title TEXT,
            link TEXT,
            cover_letter TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_read INTEGER DEFAULT 0,
            fingerprint TEXT UNIQUE
        )
    """
    )
    conn.commit()

    # The legacy JSON data is migrated to the database if the file exists.
    if os.path.exists("applied.json"):
        print("[SYSTEM] Migrating legacy applied.json to SQLite database...")
        try:
            with open("applied.json", "r") as f:
                legacy_ids = json.load(f)
                for vid in legacy_ids:
                    cursor.execute(
                        "INSERT OR IGNORE INTO vacancies (id, profile_name, title, link, cover_letter, is_read) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            vid,
                            "legacy_migration",
                            "Migrated Vacancy",
                            f"https://hh.ru/vacancy/{vid}",
                            "Migrated from JSON",
                            1,
                        ),
                    )
            conn.commit()

            # Truncating file instead of renaming/deleting to avoid Docker bind mount locks.
            with open("applied.json", "w") as f:
                json.dump([], f)

            print("[SYSTEM] Migration completed successfully. applied.json truncated.")
        except Exception as e:
            print(f"[SYSTEM ERROR] Migration failed: {e}")

    conn.close()


init_db()


def is_vacancy_processed(vacancy_id, fingerprint):
    # The database is checked for existing IDs or identical company-vacancy fingerprints processed in the last 30 days.
    conn = sqlite3.connect("applied.db", timeout=30.0, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT 1 FROM vacancies 
           WHERE id = ? OR (fingerprint = ? AND created_at >= datetime('now', '-30 days'))""",
        (vacancy_id, fingerprint),
    )
    result = cursor.fetchone()
    conn.close()
    return result is not None


def save_vacancy(vacancy_id, profile_name, title, link, cover_letter, fingerprint):
    # Processed vacancy details are saved to the database.
    conn = sqlite3.connect("applied.db", timeout=30.0, check_same_thread=False)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO vacancies (id, profile_name, title, link, cover_letter, fingerprint) VALUES (?, ?, ?, ?, ?, ?)",
            (vacancy_id, profile_name, title, link, cover_letter, fingerprint),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()


def call_llm(prompt, system_instruction=None):
    # The LLM is queried sequentially across multiple fallback models to ensure fault tolerance.
    if not openrouter_client:
        raise Exception("OpenRouter client not configured.")

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})

    last_error = None
    for model in FALLBACK_MODELS:
        try:
            print(f"[LLM] Attempting inference with model: {model}")
            response = openrouter_client.chat.completions.create(
                model=model,
                messages=messages,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"[LLM WARN] Model {model} failed: {e}. Trying next fallback...")
            last_error = e
            continue

    raise Exception(f"All fallback models failed. Last error: {last_error}")


def evaluate_vacancy(vacancy_desc, requirements):
    # The vacancy description is validated against strict criteria.
    prompt = f"""
    You are a highly selective job seeker's assistant.
    Analyze the following vacancy description carefully.
    
    Vacancy Description:
    {vacancy_desc}
    
    Candidate's Strict Requirements:
    {requirements}
    
    Task:
    Determine if the vacancy meets ALL strict requirements. If there is even a slight violation of the requirements, or if the vacancy context implies tasks the candidate explicitly wants to avoid, you MUST answer NO.
    
    Reply ONLY with 'YES' or 'NO'. No other text.
    """
    result = call_llm(prompt).upper()
    return "YES" in result


def generate_cover_letter(resume_text, vacancy_desc, contact_info):
    # A professional cover letter is drafted strictly in Russian from first-person singular perspective.
    prompt = f"""
    You are a direct, logical, and highly practical business copywriter writing a job cover letter in Russian. 

    ### INPUT DATA (VARIABLES INJECTED ONCE):
    Candidate Resume:
    {resume_text}

    Vacancy Description:
    {vacancy_desc}

    Contact Information:
    {contact_info}

    ### STRICT STYLE AND TONE RULES:
    1. NO AI CLICHÉS: Absolutely forbid typical AI-generated openings and endings. Do NOT write sentences like:
       - "Надеюсь, это письмо застанет вас в хорошем расположении духа..."
       - "Пишу вам, чтобы выразить свой искренний интерес к..."
       - "Спешу предложить свою кандидатуру на роль..."
       - "В ответ на вашу замечательную вакансию..."
       - "Буду рад внести свой вклад в ваш успех..."
    2. NO CORPORATE JARGON: Completely avoid dry, robotic, or pretentious corporate buzzwords. Do NOT use terms like: "синергия", "проактивность", "командный игрок", "стрессоустойчивость", "клиентоориентированность", "динамично развивающийся".
    3. CONCISENESS & CLARITY: Keep the tone professional, straightforward, and human. Write the way a real, confident specialist speaks — dryly, politely, but without servility or watery formalities.
    4. PERSONALIZATION ONLY: The recruiter must immediately see that this is a custom-written letter, not a template. Do not use any placeholders, draft brackets (like "[Имя]", "[Название компании]"), or generic phrases. If the company name is not mentioned in the vacancy description, refer to it as "Ваша команда" or "Ваш проект".

    ### STRUCTURAL REQUIREMENTS:
    Write strictly 3 short, punchy paragraphs followed by the direct contact info:

    - Paragraph 1: Direct opening. State clearly that you are interested in the part-time/project-based position of a Business Assistant / PM Assistant. Explicitly reference at least two specific tasks, requirements, or projects mentioned in the vacancy description to prove you have studied it thoroughly.
    - Paragraph 2: Solution matching. Do not just list your skills. Explicitly map your real accomplishments (from the candidate's resume) directly to the company's pain points (from the vacancy description). If they need automation, mention how you wrote JS tools for tracking. If they need scaling/management, mention how you scaled operations from 1 to 30+ assets. If they need research, mention your OSINT background. Keep it factual and metrics-oriented.
    - Paragraph 3: Schedule alignment & Call to action. State that your target workload is up to 30 hours per week (part-time). Propose a brief chat to discuss how your operations and automation background can free up the manager's time.

    ### OUTPUT FORMAT:
    Output ONLY the final text of the cover letter. Do not include any intro, outro, explanations, or quotes.

    [Your 3-paragraph letter here]

    С уважением,
    [Extract candidate's real name from the contact information block]
    [Extract real Telegram and Email from the contact information block]
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


def process_profile(context, page, profile):
    # Active profiles are processed using resume matchmaking.
    if not profile.get("enabled", True):
        print(f"[DEBUG] Profile {profile['name']} is disabled. Skipping.")
        return

    print(f"--- Starting profile: {profile['name']} ---")

    resume_file = profile.get("resume_file")
    print(f"[DEBUG] Attempting to read resume file: {resume_file}")
    try:
        with open(resume_file, "r") as f:
            resume_text = f.read()
    except Exception as e:
        print(f"[DEBUG ERROR] Error reading resume file: {e}")
        return

    resume_id = profile.get("resume_id")
    if not resume_id:
        print("[DEBUG ERROR] resume_id is not specified in the profile!")
        return

    # Filter arrays are parsed into sequential native search queries.
    queries_list = profile.get("queries") or [{}]
    global_filters = profile.get("global_filters") or {}
    pages_to_scrape = profile.get("pages_to_scrape", 1)
    vacancies_data = []

    # The scraper iterates over each filter query block defined in the profile (OR logic).
    for q_idx, query_filters in enumerate(queries_list):
        # The query parameters are constructed by merging global filters with the specific query block.
        merged_filters = {}
        for k, v in global_filters.items():
            merged_filters[k] = list(v) if isinstance(v, list) else [v]

        for k, v in query_filters.items():
            v_list = list(v) if isinstance(v, list) else [v]
            if k in merged_filters:
                # Lists are merged avoiding duplicates to leverage HH's native OR logic.
                merged_filters[k] = list(set(merged_filters[k] + v_list))
            else:
                merged_filters[k] = v_list

        print(
            f"[DEBUG] Processing query block {q_idx+1}/{len(queries_list)}: {merged_filters}"
        )

        # Standard search base URL is constructed with initial parameters mimicking desktop browser.
        base_url = (
            f"https://hh.ru/search/vacancy?"
            f"enable_snippets=true"
            f"&ored_clusters=true"
            f"&resume={resume_id}"
            f"&order_by=publication_time"
            f"&search_field=name"
            f"&search_field=company_name"
            f"&search_field=description"
        )

        if merged_filters:
            for key, values in merged_filters.items():
                for val in values:
                    base_url += f"&{key}={val}"

        # The scraper paginates through the results up to pages_to_scrape.
        for page_idx in range(pages_to_scrape):
            page_url = base_url + f"&page={page_idx}"
            print(f"[DEBUG] Navigating to page {page_idx}: {page_url}")

            try:
                page.goto(page_url, timeout=20000, wait_until="domcontentloaded")
                check_for_captcha(page)
            except Exception as e:
                print(f"[DEBUG ERROR] Error or timeout during page.goto: {e}")
                continue

            try:
                page.wait_for_selector(
                    '[data-qa="vacancy-serp__vacancy"]', timeout=5000
                )
            except PlaywrightTimeout:
                print(
                    f"[DEBUG] No more vacancies found on page {page_idx} for current query block."
                )
                break

            # Human-like scrolling is simulated to trigger dynamic content loading.
            for i in range(random.randint(1, 2)):
                page.mouse.wheel(0, random.randint(1000, 2000))
                human_delay(1, 2)

            vacancy_elements = page.locator('[data-qa="vacancy-serp__vacancy"]').all()
            print(f"[DEBUG] Elements found on page {page_idx}: {len(vacancy_elements)}")

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

                    # The company name is extracted to form a unique fingerprint.
                    try:
                        employer_el = el.locator(
                            '[data-qa="serp-item__employer"]'
                        ).first
                        company_name = employer_el.inner_text(timeout=2000).strip()
                    except Exception:
                        company_name = "Anonymous"

                    fingerprint = f"{company_name}:{title_text}"

                    if is_vacancy_processed(vid, fingerprint):
                        continue

                    stats_text = el.inner_text()
                    app_count = extract_applicant_count(stats_text)

                    vacancies_data.append(
                        {
                            "id": vid,
                            "title": title_text,
                            "link": f"https://hh.ru/vacancy/{vid}",
                            "app_count": app_count,
                            "fingerprint": fingerprint,
                        }
                    )
                except Exception as e:
                    continue

    # All collected results across different queries and pages are sorted in memory by applicant count.
    vacancies_data.sort(key=lambda x: x["app_count"])
    print(
        f"Found {len(vacancies_data)} total unique pre-filtered vacancies to evaluate."
    )

    for vac in vacancies_data:
        vid = vac["id"]
        print(f"Evaluating: {vac['title']} ({vid})")

        try:
            # The page navigation is attempted inside a protected block to prevent target crash failure.
            page.goto(vac["link"], timeout=60000, wait_until="domcontentloaded")
            check_for_captcha(page)
            human_delay(1, 3)

            desc_el = page.locator('[data-qa="vacancy-description"]')
            if not desc_el.is_visible():
                continue
            desc = desc_el.inner_text()

            # The employer brand rating is parsed if visible on the page.
            try:
                rating_el = page.locator('[data-qa="employer-rating-by-brand"]').first
                rating = rating_el.inner_text(timeout=2000).strip()
            except Exception:
                rating = "Not Specified"

            # The rating details are appended to the description context for LLM evaluation.
            full_description = f"Employer Rating: {rating}\n\nDescription:\n{desc}"

            # The vacancy description and rating are checked for validity.
            if not evaluate_vacancy(
                full_description, profile.get("strict_requirements", "")
            ):
                print(f"[-] Rejected by LLM filter: {vid}")
                save_vacancy(
                    vid,
                    profile["name"],
                    vac["title"],
                    vac["link"],
                    "Rejected by LLM filter",
                    vac["fingerprint"],
                )
                continue

            print(f"[+] Accepted by LLM. Generating cover letter...")
            cover_letter = generate_cover_letter(
                resume_text, desc, profile.get("contact_info", "")
            )

            # The vacancy is saved to the database. Push notification is skipped for Pull-only workflow.
            save_vacancy(
                vid,
                profile["name"],
                vac["title"],
                vac["link"],
                cover_letter,
                vac["fingerprint"],
            )

        except Exception as e:
            print(f"[ERROR] Exception processing {vid}: {e}")
            # If the browser page crashed or target closed, a new page is initialized to recover the context.
            if (
                "crash" in str(e).lower()
                or "close" in str(e).lower()
                or "target" in str(e).lower()
            ):
                print(
                    "[SYSTEM] Playwright page crashed or closed. Recovering context..."
                )
                try:
                    page.close()
                except Exception:
                    pass
                # A fresh page is initialized and configured with stealth patches to restore the execution environment.
                page = context.new_page()
                stealth_sync(page)


def run_scraping_cycle():
    # The scraping cycle iterates over all active profiles using a fresh browser context.
    global config
    config = load_config()

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

        context = browser.new_context(
            storage_state="state.json",
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )

        try:
            page = context.new_page()
            stealth_sync(page)

            for profile in config.get("profiles", []):
                if profile.get("enabled", True):
                    process_profile(context, page, profile)

        finally:
            # The browser context is explicitly closed in a finally block to prevent memory leaks.
            browser.close()


def scraper_worker():
    # The scraper background thread runs the cycle periodically.
    while True:
        print("[SCRAPER] Starting periodic scraping cycle...")
        try:
            run_scraping_cycle()
        except Exception as e:
            print(f"[SCRAPER ERROR] Scraping failed: {e}")
        # The thread sleeps for 4 hours before the next execution.
        time.sleep(14400)


# Telegram Bot Interface and Interactive Menus.
def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    keyboard.add(
        types.KeyboardButton("🟢/🔴 Toggle Profiles"),
        types.KeyboardButton("📂 View Unread (3 Days)"),
        types.KeyboardButton("⚙️ Edit Strict Requirements"),
        types.KeyboardButton("📄 Upload Resume"),
        types.KeyboardButton("➕ Add Profile via YAML"),
        types.KeyboardButton("❌ Delete Profile"),
    )
    return keyboard


def get_cancel_keyboard():
    # A standardized inline keyboard is generated to handle operation cancellations.
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="cancel_action"))
    return keyboard


def save_config_file(config_data):
    # The configuration dictionary is written back to config.yaml safely.
    with open("config.yaml", "w") as f:
        yaml.safe_dump(config_data, f, allow_unicode=True)


@bot.message_handler(commands=["start", "menu"])
def send_welcome(message):
    bot.send_message(
        message.chat.id,
        "Welcome to HH Job Automation Bot. Use the menu below to configure profiles and view results.",
        reply_markup=get_main_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "cancel_action")
def callback_cancel_action(call):
    # Active user input handlers are cleared and the main menu is sent.
    bot.clear_step_handler_by_chat_id(chat_id=call.message.chat.id)
    bot.send_message(
        call.message.chat.id, "Action cancelled.", reply_markup=get_main_keyboard()
    )
    bot.answer_callback_query(call.id)


@bot.message_handler(
    func=lambda message: message.text in ["/menu", "/start", "cancel", "Cancel"]
)
def handle_menu_cancellation(message):
    # Step handlers are cleared when a manual cancel command is sent.
    bot.clear_step_handler_by_chat_id(chat_id=message.chat.id)
    send_welcome(message)


@bot.message_handler(content_types=["document"])
def handle_document_upload_fallback(message):
    # Safe document fallback handler.
    if message.document.file_name.endswith(".txt"):
        bot.reply_to(
            message,
            "Please select the '📄 Upload Resume' menu button first to initiate file uploads.",
        )
    else:
        bot.reply_to(message, "Only .txt files are supported for resume uploads.")


def process_resume_upload(message):
    if message.text in ["/menu", "/start", "cancel", "Cancel"]:
        handle_menu_cancellation(message)
        return

    if not message.document or not message.document.file_name.endswith(".txt"):
        bot.send_message(
            message.chat.id,
            "[ERROR] You must upload a valid .txt file. Action cancelled.",
            reply_markup=get_main_keyboard(),
        )
        return

    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        os.makedirs("resumes", exist_ok=True)
        save_path = os.path.join("resumes", message.document.file_name)

        with open(save_path, "wb") as new_file:
            new_file.write(downloaded_file)

        bot.reply_to(
            message,
            f"Resume file saved successfully to <code>{save_path}</code>. You can now reference this in your profile.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
    except Exception as e:
        bot.reply_to(
            message,
            f"[ERROR] Failed to save file: {e}",
            reply_markup=get_main_keyboard(),
        )


@bot.message_handler(func=lambda message: message.text == "📄 Upload Resume")
def handle_resume_upload_start(message):
    # The resume upload process is initiated with a unified cancellation option.
    msg = bot.send_message(
        message.chat.id,
        "Please upload your resume as a <b>.txt</b> file now:",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(),
    )
    bot.register_next_step_handler(msg, process_resume_upload)


@bot.message_handler(func=lambda message: message.text == "🟢/🔴 Toggle Profiles")
def handle_toggle_profiles(message):
    global config
    config = load_config()
    keyboard = types.InlineKeyboardMarkup()
    for profile in config.get("profiles", []):
        status = "🟢" if profile.get("enabled", True) else "🔴"
        keyboard.add(
            types.InlineKeyboardButton(
                f"{status} {profile['name']}",
                callback_data=f"toggle_prof:{profile['name']}",
            )
        )
    keyboard.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="cancel_action"))
    bot.send_message(
        message.chat.id,
        "Select a profile to toggle active state:",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_prof:"))
def callback_toggle_profile(call):
    global config
    profile_name = call.data.split(":")[1]
    config = load_config()
    for profile in config.get("profiles", []):
        if profile["name"] == profile_name:
            current_state = profile.get("enabled", True)
            profile["enabled"] = not current_state
            break
    save_config_file(config)

    keyboard = types.InlineKeyboardMarkup()
    for profile in config.get("profiles", []):
        status = "🟢" if profile.get("enabled", True) else "🔴"
        keyboard.add(
            types.InlineKeyboardButton(
                f"{status} {profile['name']}",
                callback_data=f"toggle_prof:{profile['name']}",
            )
        )
    keyboard.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="cancel_action"))
    bot.edit_message_reply_markup(
        call.message.chat.id, call.message.message_id, reply_markup=keyboard
    )
    bot.answer_callback_query(call.id, f"Profile {profile_name} updated.")


@bot.message_handler(func=lambda message: message.text == "📂 View Unread (3 Days)")
def handle_view_unread(message):
    global config
    config = load_config()
    keyboard = types.InlineKeyboardMarkup()
    for profile in config.get("profiles", []):
        keyboard.add(
            types.InlineKeyboardButton(
                profile["name"], callback_data=f"view_unread:{profile['name']}"
            )
        )
    keyboard.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="cancel_action"))
    bot.send_message(
        message.chat.id,
        "Select a profile to view unread vacancies:",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("view_unread:"))
def callback_view_unread(call):
    profile_name = call.data.split(":")[1]
    conn = sqlite3.connect("applied.db", check_same_thread=False)
    cursor = conn.cursor()
    # Unread vacancies from the last 3 days are retrieved.
    cursor.execute(
        """SELECT id, title, link, cover_letter FROM vacancies 
           WHERE profile_name = ? AND is_read = 0 AND cover_letter != 'Rejected by LLM filter'
           AND created_at >= datetime('now', '-3 days')""",
        (profile_name,),
    )
    rows = cursor.fetchall()

    if not rows:
        bot.send_message(
            call.message.chat.id,
            f"No new unread vacancies found for {profile_name} in the last 3 days.",
        )
        conn.close()
        return

    for row in rows:
        vac_id, title, link, cover_letter = row

        # Special HTML characters are escaped to prevent Telegram markup parsing errors.
        safe_title = html.escape(title)
        safe_cover_letter = html.escape(cover_letter)

        text = (
            f"📌 <b>{safe_title}</b>\n"
            f"🔗 Link: {link}\n\n"
            f"📝 <b>Cover Letter:</b>\n"
            f"<code>{safe_cover_letter}</code>"
        )
        cursor.execute("UPDATE vacancies SET is_read = 1 WHERE id = ?", (vac_id,))
        conn.commit()

        bot.send_message(
            call.message.chat.id, text, parse_mode="HTML", disable_web_page_preview=True
        )
        time.sleep(0.5)

    conn.close()
    bot.answer_callback_query(call.id, "All listed vacancies marked as read.")


@bot.message_handler(func=lambda message: message.text == "⚙️ Edit Strict Requirements")
def handle_edit_requirements(message):
    global config
    config = load_config()
    keyboard = types.InlineKeyboardMarkup()
    for profile in config.get("profiles", []):
        keyboard.add(
            types.InlineKeyboardButton(
                profile["name"], callback_data=f"edit_req_start:{profile['name']}"
            )
        )
    keyboard.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="cancel_action"))
    bot.send_message(
        message.chat.id,
        "Select a profile to edit strict requirements:",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_req_start:"))
def callback_edit_req_start(call):
    profile_name = call.data.split(":")[1]

    # Current strict requirements are retrieved from the selected profile to facilitate copying.
    current_req = ""
    global config
    config = load_config()
    for profile in config.get("profiles", []):
        if profile["name"] == profile_name:
            current_req = profile.get("strict_requirements", "")
            break

    msg = bot.send_message(
        call.message.chat.id,
        f"The current strict requirements for profile <b>{profile_name}</b> are:\n\n"
        f"<code>{html.escape(current_req)}</code>\n\n"
        "Click the code block above to copy the requirements, edit the text, and send it to me. Or click Cancel below to abort.",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(),
    )
    bot.register_next_step_handler(msg, process_new_requirements, profile_name)
    bot.answer_callback_query(call.id)


def process_new_requirements(message, profile_name):
    if message.text in ["/menu", "/start", "cancel", "Cancel"]:
        handle_menu_cancellation(message)
        return

    global config
    new_req = message.text.strip()
    config = load_config()
    for profile in config.get("profiles", []):
        if profile["name"] == profile_name:
            profile["strict_requirements"] = new_req
            break
    save_config_file(config)
    bot.send_message(
        message.chat.id,
        f"Strict requirements for <b>{profile_name}</b> successfully updated.",
        parse_mode="HTML",
    )


@bot.message_handler(func=lambda message: message.text == "➕ Add Profile via YAML")
def handle_add_profile_yaml(message):
    template = (
        'name: "devops_mid"\n'
        "enabled: true\n"
        'resume_id: "your_resume_id_here"\n'
        'resume_title: "Resume Title"\n'
        'resume_file: "resumes/devops.txt"\n'
        'contact_info: "Name: ...\\nPhone: ..."\n'
        'strict_requirements: "Junior or Mid level..."\n'
        "pages_to_scrape: 1\n"
        "global_filters:\n"
        '  work_format: ["REMOTE"]\n'
        "queries:\n"
        '  - employment_form: ["PART"]\n'
        '  - work_schedule_by_days: ["FLEXIBLE", "TWO_ON_TWO_OFF"]\n'
        '    working_hours: ["HOURS_4"]'
    )
    msg = bot.send_message(
        message.chat.id,
        f"Send a YAML snippet matching the template below to add a new profile:\n\n<code>{template}</code>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(),
    )
    bot.register_next_step_handler(msg, process_add_profile_yaml)


def process_add_profile_yaml(message):
    if message.text in ["/menu", "/start", "cancel", "Cancel"]:
        handle_menu_cancellation(message)
        return

    global config
    yaml_text = message.text.strip()
    try:
        new_profile = yaml.safe_load(yaml_text)

        # Validation checks on the submitted YAML.
        required_fields = [
            "name",
            "resume_id",
            "resume_title",
            "resume_file",
            "strict_requirements",
            "contact_info",
        ]
        for field in required_fields:
            if field not in new_profile:
                bot.send_message(
                    message.chat.id,
                    f"[ERROR] Missing required field: '{field}'. Try again.",
                    reply_markup=get_main_keyboard(),
                )
                return

        resume_path = new_profile["resume_file"]
        if not os.path.exists(resume_path):
            bot.send_message(
                message.chat.id,
                f"[ERROR] Resume file not found on server at path: '{resume_path}'. Ensure you upload the .txt file directly to the bot first.",
                reply_markup=get_main_keyboard(),
            )
            return

        config = load_config()
        # Prevent profile name duplication.
        for existing in config.get("profiles", []):
            if existing["name"] == new_profile["name"]:
                bot.send_message(
                    message.chat.id,
                    f"[ERROR] Profile with name '{new_profile['name']}' already exists.",
                    reply_markup=get_main_keyboard(),
                )
                return

        config.setdefault("profiles", []).append(new_profile)
        save_config_file(config)
        bot.send_message(
            message.chat.id,
            f"Profile '{new_profile['name']}' successfully added to config.yaml.",
            reply_markup=get_main_keyboard(),
        )
    except Exception as e:
        bot.send_message(
            message.chat.id,
            f"[ERROR] Failed to parse YAML: {e}. Try again.",
            reply_markup=get_main_keyboard(),
        )


@bot.message_handler(func=lambda message: message.text == "❌ Delete Profile")
def handle_delete_profile(message):
    global config
    config = load_config()
    keyboard = types.InlineKeyboardMarkup()
    for profile in config.get("profiles", []):
        keyboard.add(
            types.InlineKeyboardButton(
                profile["name"], callback_data=f"del_prof:{profile['name']}"
            )
        )
    keyboard.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="cancel_action"))
    bot.send_message(
        message.chat.id, "Select a profile to delete:", reply_markup=keyboard
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("del_prof:"))
def callback_delete_profile(call):
    global config
    profile_name = call.data.split(":")[1]
    config = load_config()

    updated_profiles = [
        p for p in config.get("profiles", []) if p["name"] != profile_name
    ]
    config["profiles"] = updated_profiles
    save_config_file(config)

    bot.send_message(
        call.message.chat.id, f"Profile '{profile_name}' deleted from config.yaml."
    )
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda message: True)
def handle_all_other_messages(message):
    # This fallback handler ensures the persistent keyboard menu is always sent.
    bot.send_message(
        message.chat.id,
        "Please use the menu buttons below to interact with the bot.",
        reply_markup=get_main_keyboard(),
    )


if __name__ == "__main__":
    # The scraping loop is started in a separate daemon thread.
    threading.Thread(target=scraper_worker, daemon=True).start()

    # The Telegram bot starts polling with a self-healing retry structure.
    print("[SYSTEM] Starting interactive Telegram bot interface...")
    while True:
        try:
            bot.infinity_polling()
        except Exception as e:
            print(
                f"[SYSTEM ERROR] Telegram polling failed: {e}. Retrying in 10 seconds..."
            )
            time.sleep(10)
