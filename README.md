# HH Job Automation Bot

An automated, pull-only job matchmaking assistant designed for HeadHunter (HH.ru). It operates directly through Chromium (Playwright) and OpenRouter LLMs to scrape, filter, and write custom cover letters for recommended vacancies.

To guarantee a **0% ban probability**, the bot operates in a strictly passive (Read-Only) mode. It does not perform automated applications, messaging, or profile mutations on HH.ru.

---

## 1. Directory Structure

The following file structure is expected in the project root:

```text
hh-auto-apply/
├── .env                  # Excluded from Git. Contains secret tokens and keys
├── .gitignore            # Excludes config.yaml, applied.db, and .env from Git
├── applied.db            # Local SQLite database (auto-generated)
├── auth_setup.py         # One-time browser session authorization script
├── config.yaml           # Excluded from Git. Search profiles configuration
├── Dockerfile            # Multi-stage Playwright Docker builder
├── main.py               # Main long-running service entrypoint
├── requirements.txt      # Python dependencies list
├── shell.nix             # (Optional) Nix development environment for NixOS hosts
└── resumes/              # Directory containing resume .txt files (auto-generated)

2. Host Bootstrapping & File Initialization

Before launching the Docker container on your host, you must create the persistent database file manually to prevent the Docker daemon from mapping it as a directory.

Run these commands in your host terminal from the project root:

# An empty database file is initialized
touch applied.db

# Correct file permissions are granted to the database
chmod 666 applied.db

3. Configuration Setup
.env File

Create a .env file in the root directory and configure your tokens:
code Env
OPENROUTER_API_KEY=your_openrouter_api_key_hereTELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

OPENROUTER_API_KEY=your_openrouter_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here

# The primary model used by OpenRouter
OPENROUTER_MODEL=qwen/qwen-2.5-72b-instruct:free

config.yaml File

The service features a self-healing bootstrap. If config.yaml is missing on start, the bot will automatically generate an empty skeleton. You can then configure your profiles directly via the Telegram Bot or write them manually matching this structure:

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
      [Your strict multi-line prompt guidelines and criteria go here]

4. Browser Session Authorization (state.json)

To navigate matching search pages, the bot requires an active authorized session on HH.ru. You must generate state.json once on your workstation.
For NixOS Hosts:

Use the provided shell.nix to load Playwright dependencies natively:

# Enter the Nix development shell
nix-shell

# Run the authorization setup script
python auth_setup.py

For non-NixOS Hosts:

Ensure Playwright is installed locally:

pip install playwright && playwright install chromium
python auth_setup.py

Steps:

    A Chromium window will open. Log in manually to your HH.ru account and solve any captchas.

    Navigate to your resume list.

    Return to the terminal and press ENTER.

    The state.json file is generated in the root folder.

5. Integrating with an Existing Homelab Stack (Monolithic docker-compose.yml)

If your homelab services reside in a single monolithic docker-compose.yml file, do not run a separate docker-compose command. Integrate the hh-bot service directly into your existing configuration.
Deployment steps:

    Place the hh-auto-apply repository folder inside your homelab directory (e.g. /home/user/homelab/hh-auto-apply).

    Open your main homelab /home/user/homelab/docker-compose.yml.

    Append the service block below, ensuring the build: context and volume mappings point correctly to the relative path of the ./hh-auto-apply subdirectory:

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

Rebuild and launch your monolithic stack from your homelab root folder:

sudo docker compose up -d --build

Telegram Bot Interface Usage

Once deployed, open your Telegram Bot and send /start or /menu to initialize the graphical sticky keyboard menu.
Bot Commands & Buttons:

    🟢/🔴 Toggle Profiles: Lists all profiles stored in config.yaml with their active status. Click on a profile name to toggle it on or off.

    📂 View Unread (3 Days): Pulls all newly evaluated matching vacancies and their prepared cover letters generated in the last 3 days. Vacancies shown here are automatically marked as read in the database.

    ⚙️ Edit Strict Requirements: Lists profiles. Selecting a profile displays the current strict requirements in a copyable code block, unifies the input prompt, and lets you overwrite the requirements by sending a new text message.

    📄 Upload Resume: Puts the bot in file-reception mode. Drop any .txt resume file into the chat, and the bot automatically saves it to the resumes/ folder on your server.

    ➕ Add Profile via YAML: Sends a formatted template snippet. Fill it out and send it back as a message. The bot validates the fields, checks if the referred resume exists, and appends the new profile block to config.yaml.

    ❌ Delete Profile: Displays a list of active profiles. Clicking one permanently deletes it from config.yaml.

    🔙 Cancel Button: Present in all input prompts. Clicking it instantly terminates any active next-step input state, preventing the bot from hanging.
