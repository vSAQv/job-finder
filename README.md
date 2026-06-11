# 🤖 HH Job Automation Bot

> **A highly reliable, automated, pull-only job matchmaking assistant designed for HeadHunter (HH.ru).**
>
> It operates directly through Chromium (Playwright) and OpenRouter LLMs to scrape, filter, and prepare tailored cover letters.

<p align="left">
  <img src="https://img.shields.io/badge/Ban_Probability-0%25-green?style=for-the-badge&logo=shield" alt="0% Ban Probability">
</p>

To guarantee a **0% ban probability**, the bot operates in a strictly passive (**Read-Only**) mode. It does not perform automated applications, messaging, or profile mutations on HH.ru.

---

## 📂 1. Directory Structure

The following file structure is expected in the project root:

```directory
hh-auto-apply/
├── .env                  # Excluded from Git. Contains secret tokens and keys
├── .gitignore            # Excludes config.yaml, applied.db, and .env from Git
├── applied.db            # Local SQLite database (auto-generated)
├── auth_setup.py         # One-time browser session authorization script
├── config.yaml           # Excluded from Git. Search profiles configuration
├── Dockerfile            # Multi-stage Playwright Docker builder
├── main.py               # Main long-running service entrypoint
├── requirements.txt      # Python dependencies list
├── shell.nix             # (Optional, for NixOS users) Nix development environment
└── resumes/              # Directory containing resume .txt files (auto-generated)
```

---

## 🚀 2. Host Bootstrapping & File Initialization

Before launching the Docker container on your host, you must create the persistent database file manually to prevent the Docker daemon from mapping it as a directory.

Run these commands in your host terminal from the project root:

```bash
# An empty database file is initialized
touch applied.db

# Correct file permissions are granted to the database
chmod 666 applied.db
```

---

## ⚙️ 3. Configuration Setup

### `.env` File
Create a `.env` file in the root directory and configure your tokens:

```env
OPENROUTER_API_KEY=your_openrouter_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here

# The primary model used by OpenRouter (Gemma 4 is recommended for Russian reasoning)
OPENROUTER_MODEL=google/gemma-4-31b-it:free
```

### `config.yaml` File
The service features a self-healing bootstrap. If `config.yaml` is missing on start, the bot will automatically generate an empty skeleton. You can then configure your profiles directly via the Telegram Bot or write them manually matching this structure:

<details>
<summary><b>📖 Click to expand config.yaml template</b></summary>

```yaml
profiles:
  - name: "project_manager_profile"
    enabled: true
    resume_id: "your_resume_hash_id" # Must be wrapped in quotes
    
    # WARNING: This MUST match the exact, case-sensitive title of your resume on the HH.ru desktop UI!
    resume_title: "Менеджер проектов" 
    
    resume_file: "resumes/pm_resume.txt"
    contact_info: "Name: ...\nPhone: ...\nEmail: ...\nTelegram: @..."
    pages_to_scrape: 2
    
    # Global filters are applied to every query block consistently (uncomment needed values)
    global_filters:
      work_format: ["REMOTE"] # Options: REMOTE, IN_OFFICE, COMBINED
      # experience: ["noExperience"] # Options: noExperience, between1And3, between3And6, moreThan6
      # salary: 100000 # Minimum salary as integer (not recommended: hides unstated salary vacancies)
      # currency: ["RUR"] # Options: RUR, USD, EUR, BYR
      # search_period: 3 # Options: 1 (24 hours), 3 (3 days), 30 (month)
      # label: ["not_from_agency"] # Hides vacancy agencies, shows direct employers only

    # Queries are rotated sequentially and merged with global_filters (additive OR logic)
    queries:
      - employment_form: ["PART", "PROJECT"] # Options: PART, PROJECT, FULL
      - work_schedule_by_days: ["FLEXIBLE", "TWO_ON_TWO_OFF"] # Options: FLEXIBLE, TWO_ON_TWO_OFF, THREE_ON_THREE_OFF
        working_hours: ["HOURS_4"] # Options: HOURS_2, HOURS_3, HOURS_4, HOURS_5, HOURS_6
        
    strict_requirements: |
      1. HOURS & SCHEDULE:
         - PASS: Part-time, flexible hours, project-based work, or an explicit workload cap of 30 hours per week or less.
         - FAIL: Strictly full-time (40+ hours per week, e.g., 9:00 to 18:00 in-office requirements).

      2. ROLE FOCUS:
         - PASS: Business Assistant, Project Assistant, Junior PM, Operations Assistant, Process Coordinator. The role must focus on business processes, research, coordination, or technical support.
         - FAIL: Personal Assistant / Executive Secretary (handling personal tasks like buying groceries, ordering private flights, booking family tables, dry cleaning). Cold Sales / Cold Calling roles (monotonous phone sales).

      3. NATURE OF TASKS:
         - PASS: Requires analytical thinking, data structuring, research (OSINT), team coordination, automation of routines, or integration of AI tools.
         - FAIL: Purely repetitive manual data entry (brainless copy-pasting) with zero opportunity for optimization or code automation.

      4. OPTIMIZATION POTENTIAL:
         - PASS: Any business vertical (E-commerce, Real Estate, Education, Startups, Manufacturing) where the management requires building structured operational systems, automating workflows, implementing AI/LLM tools, or writing scripts to cut down manual labor.
         - FAIL: Rigid traditional businesses with strictly manual, unchangeable administrative routines that explicitly reject any technical optimization or workflow automation.

      5. HOURLY RATE (SALARY):
         - PASS: If the salary is specified, the hourly rate must be equivalent to 17.5 BYN (approx. $5.4 USD or 500 RUR) per hour or higher. For example, a workload of 30 hours per week must pay at least 2100 BYN (approx. $650 USD or 60,000 RUR) per month. A workload of 20 hours per week must pay at least 1400 BYN (approx. $430 USD or 40,000 RUR) per month. If the salary is NOT specified, you MUST accept (PASS) the vacancy.
         - FAIL: Only reject (FAIL) the vacancy if the salary is explicitly stated AND falls below the 17.5 BYN ($5.4 USD / 500 RUR) per hour threshold.

      6. CONTRACT & EMPLOYMENT TYPE:
         - PASS: Self-employed (самозанятость), short-term independent contractor agreements (подрядческий контракт / ГПХ) that can be easily terminated, or if the employment/contract type is NOT mentioned at all.
         - FAIL: Strictly long-term corporate contracts (трудовой договор) requiring a commitment of more than 6 months.
```
</details>

---

## 🔑 4. Browser Session Authorization (`state.json`)

To navigate matching search pages, the bot requires an active authorized session on HH.ru. You must generate `state.json` once on your workstation.

### For NixOS Hosts:
Use the provided `shell.nix` to load Playwright dependencies natively:

```bash
# Enter the Nix development shell
nix-shell

# Run the authorization setup script
python auth_setup.py
```

### For non-NixOS Hosts:
Ensure Playwright is installed locally:

```bash
pip install playwright && playwright install chromium
python auth_setup.py
```

### Steps:
1. A Chromium window will open. Log in manually to your HH.ru account and solve any captchas.
2. Navigate to your resume list.
3. Return to the terminal and press `<kbd>ENTER</kbd>`.
4. The `state.json` file is generated in the root folder.

---

## 🐳 5. Integrating with an Existing Homelab Stack

If your homelab services reside in a single monolithic `docker-compose.yml` file, do not run a separate docker-compose command. Integrate the `hh-bot` service directly into your existing configuration.

### Deployment steps:
1. Place the `hh-auto-apply` repository folder inside your homelab directory (e.g., `/home/user/homelab/hh-auto-apply`).
2. Open your main homelab `/home/user/homelab/docker-compose.yml`.
3. Append the service block below, ensuring the `build:` context and volume mappings point correctly to the relative path of the `./hh-auto-apply` subdirectory:

```yaml
services:
  # ... Your existing homelab services (e.g., portainer, nextcloud, pihole) ...

  hh-bot:
    build: ./hh-auto-apply  # Relative path to the cloned repository folder
    container_name: hh-bot
    restart: unless-stopped
    ipc: host  # Shares host shared memory to prevent Chromium out-of-memory crashes
    env_file:
      - ./hh-auto-apply/.env
    networks:
      - homelab_network  # Joins your existing homelab Docker network
    volumes:
      # Ensure write permissions are enabled by removing :ro flags from config and resumes
      - ./hh-auto-apply/config.yaml:/app/config.yaml
      - ./hh-auto-apply/resumes:/app/resumes
      - ./hh-auto-apply/applied.db:/app/applied.db  # Database persistence volume mapping
      - ./hh-auto-apply/state.json:/app/state.json:ro

networks:
  homelab_network:
    external: true
    name: homelab_default  # Replace with the actual name of your homelab Docker network
```

4. Rebuild and launch your monolithic stack from your homelab root folder:
```bash
sudo docker compose up -d --build
```

---

## 💬 6. Telegram Bot Interface Usage

Once deployed, open your Telegram Bot and send `/start` or `/menu` to initialize the graphical sticky keyboard menu.

| Button / Action | Description |
| :--- | :--- |
| **🟢/🔴 Toggle Profiles** | Lists all profiles stored in `config.yaml` with their active status. Click on a profile name to toggle it on or off. |
| **📂 View Unread (3 Days)** | Pulls all newly evaluated matching vacancies and their prepared cover letters generated in the last 3 days. Vacancies shown here are automatically marked as read in the database. |
| **⚙️ Edit Strict Requirements** | Lists profiles. Selecting a profile displays the current strict requirements in a copyable code block, unifies the input prompt, and lets you overwrite the requirements by sending a new text message. |
| **📄 Upload Resume** | Puts the bot in file-reception mode. Drop any `.txt` resume file into the chat, and the bot automatically saves it to the `resumes/` folder on your server. |
| **➕ Add Profile via YAML** | Sends a formatted template snippet. Fill it out and send it back as a message. The bot validates the fields, checks if the referred resume exists, and appends the new profile block to `config.yaml`. |
| **❌ Delete Profile** | Displays a list of active profiles. Clicking one permanently deletes it from `config.yaml`. |
| **🔙 Cancel Button** | Present in all input prompts. Clicking it instantly terminates any active next-step input state, preventing the bot from hanging. |
```
