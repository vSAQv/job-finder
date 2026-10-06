# 🤖 HH Job Finder

[🇬🇧 English](README.md) · [🇷🇺 Русский](README.ru.md)

A pull-only job-matching assistant for [HH.ru](https://hh.ru/).

<p align="left">
  <img src="https://img.shields.io/badge/Ban_Probability-0%25-green?style=for-the-badge&logo=shield" alt="0% Ban Probability">
</p>

HH Job Finder uses a real Chromium browser session through Playwright to search vacancies, profile-specific rules to narrow the search, OpenRouter LLMs to judge vacancies and generate tailored cover letters, SQLite to keep processing history, and Telegram as the control/review interface.

The project is designed around a **0% ban probability**: it only reads data from HH.ru and never submits applications, sends employer messages, or mutates the HH.ru profile.

> **No Kubernetes required.** Docker is the portable deployment path. Kubernetes is supported as an optional homelab/GitOps deployment.

## How it works

```text
HH.ru authenticated session
            │
            ▼
      Resume-based search
            │
            ▼
   Profile filters + query blocks
            │
            ▼
  Vacancy cards / candidate pool
            │
            ▼
 Vacancy details + employer rating
            │
            ▼
   LLM judge: YES / NO
       │             │
     NO            YES
       │             │
       ▼             ▼
  store as         LLM writer
  processed             │
                        ▼
                  SQLite history
                        │
                        ▼
                  Telegram review
```

The scraper runs in a background thread. Telegram polling runs independently in the main thread, so the bot remains available while a scraping cycle is in progress.

A scraping cycle processes every enabled profile, then sleeps for 4 hours before starting the next cycle.

## Current features

### Vacancy collection

- Searches HH.ru through a saved authenticated browser session.
- Processes every enabled profile independently.
- Builds HH.ru search URLs from the profile's `resume_id`, `global_filters`, and query blocks.
- Supports multiple query blocks per profile; blocks are processed sequentially and share the global filters.
- Paginates up to `pages_to_scrape` pages for every query block.
- Orders search results by publication time.
- Collects vacancy title, vacancy ID, URL, company name, applicant count, and a company/title fingerprint.
- Opens unseen vacancies and reads the full vacancy description.
- Reads the employer brand rating when HH.ru exposes it.
- Uses bounded human-like delays and scrolling between browser interactions.
- Sorts the collected candidate vacancies by applicant count before detailed evaluation.

### Profile-specific filtering

Each profile has its own:

- HH.ru resume ID;
- resume text file;
- search filters and query blocks;
- strict requirements;
- contact information;
- enabled/disabled state;
- page limit.

This lets the same service run different job-search strategies from different resumes, such as a project/operations profile and a DevOps/infrastructure profile.

### LLM judge

The judge receives the vacancy description and the selected profile's strict requirements and decides whether the vacancy satisfies all required criteria.

The response is validated as a single `YES` / `NO` token. Russian `ДА` / `НЕТ` is also accepted by the normalizer.

A malformed response or a request failure does not automatically stop the cycle: the next model in the fallback pool is tried instead.

### LLM writer

Accepted vacancies are passed to a separate writer task.

The writer receives:

1. the selected candidate resume;
2. the exact vacancy title;
3. the vacancy description;
4. the profile's contact information.

The current writer rules are intentionally narrow:

- exactly 3 short paragraphs;
- direct opening focused on the actual vacancy;
- 1–2 strongest relevant facts from the resume rather than a resume dump;
- practical, dry, human Russian;
- no AI/corporate clichés or generic filler;
- no invented personal or professional facts;
- contact information appended from the profile data.

The writer also has a lightweight Russian-sanity validator. If the generated output fails validation, the next model is attempted.

### Model routing and fallback

The repository contains a small curated free-model pool instead of dynamically probing a large list on every cycle.

The current curated models are:

```text
qwen/qwen3.8-27b:free
thinkingmachines/inkling-small:free
thinkingmachines/inkling:free
nvidia/nemotron-3-ultra-550b-a55b:free
```

The same curated set is currently available to both `judge` and `writer` tasks. Task-specific validation is what enforces different behavior:

- judge → exact `YES` / `NO` style output;
- writer → Russian cover-letter sanity checks.

A model that fails at runtime is temporarily demoted in memory for 30 minutes. This avoids repeatedly retrying a known-bad free endpoint while adding no extra probing traffic.

### `OPENROUTER_MODEL`

`OPENROUTER_MODEL` is an optional runtime override and is intentionally **shared by both judge and writer**.

When it is set, that model is attempted first for both tasks. If it fails inference or response validation, the normal curated pool is used as fallback.

When the variable is absent, the application uses its built-in default:

```text
google/gemma-4-31b-it:free
```

The model name itself is configuration, not a secret. The Kubernetes manifest does not need to contain it.

### SQLite history and deduplication

Processed vacancies are stored in SQLite using the following logical fields:

```sql
CREATE TABLE vacancies (
    id TEXT PRIMARY KEY,
    profile_name TEXT,
    title TEXT,
    link TEXT,
    cover_letter TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_read INTEGER DEFAULT 0,
    fingerprint TEXT UNIQUE
)
```

Current behavior:

- vacancy IDs are treated as processed permanently;
- an identical company/title fingerprint suppresses duplicates for 30 days;
- unread accepted results are available through Telegram for 3 days;
- rejected vacancies are stored as processed but are not shown in unread review;
- cover letters are stored with the vacancy record.

### Current history limitation

The SQLite schema currently uses the HH.ru vacancy ID as a **global** primary key. Therefore the same vacancy cannot yet be stored as an independent history entry for two different profiles.

This is a known limitation and is listed under **Planned** below.

## Telegram bot

The bot is the main runtime control surface.

| Action | Purpose |
| :--- | :--- |
| `🟢/🔴 Toggle Profiles` | Enable or disable individual profiles. |
| `📂 View Unread (3 Days)` | Review accepted vacancies and generated cover letters from the last 3 days. Opening them marks them as read. |
| `⚙️ Edit Strict Requirements` | Replace the strict requirements text of a selected profile. |
| `📄 Upload Resume` | Upload a `.txt` resume into the mounted `resumes/` directory. |
| `➕ Add Profile via YAML` | Add a new profile by sending one YAML mapping. |
| `❌ Delete Profile` | Remove a profile from `config.yaml`. |
| `🔙 Cancel` | Cancel a multi-step interaction. |

### Add Profile via YAML

The bot expects a **single profile mapping**, not the complete `profiles:` list.

Valid shape:

```yaml
name: "devops"
enabled: true
resume_id: "your_hh_resume_id"
resume_title: "DevOps Engineer / Infrastructure Engineer / Linux Administrator"
resume_file: "resumes/devops.txt"
contact_info: |
  Name: Your Name
  Email: you@example.com
  Telegram: @username
pages_to_scrape: 4

global_filters:
  work_format:
    - REMOTE

queries:
  - employment_form:
      - PART
      - PROJECT

  - work_schedule_by_days:
      - FLEXIBLE
      - TWO_ON_TWO_OFF
    working_hours:
      - HOURS_4
      - HOURS_2
      - HOURS_3

strict_requirements: |
  1. ...
  2. ...
```

Do **not** send:

```yaml
profiles:
  - name: "devops"
```

and do not send a top-level YAML list beginning with `- name:`. The Telegram handler parses the submitted message as one profile object and then appends it to `config.yaml`.

## Profile configuration

`config.yaml` is runtime configuration. A missing file is bootstrapped as an empty `profiles` skeleton, but useful scraping requires at least one enabled profile.

Main fields:

```yaml
profiles:
  - name: "example_profile"
    enabled: true

    resume_id: "your_hh_resume_id"
    resume_title: "Resume title"
    resume_file: "resumes/example.txt"

    contact_info: |
      Name: Your Name
      Email: you@example.com
      Telegram: @username

    pages_to_scrape: 4

    global_filters:
      work_format:
        - REMOTE

    queries:
      - employment_form:
          - PART
          - PROJECT

      - work_schedule_by_days:
          - FLEXIBLE
          - TWO_ON_TWO_OFF
        working_hours:
          - HOURS_4
          - HOURS_2
          - HOURS_3

    strict_requirements: |
      1. ...
      2. ...
```

`resume_id` is the HH.ru resume identifier used to build the private resume-based search URL. `resume_title` is profile metadata; the search itself uses `resume_id`.

`global_filters` are applied to every query block. Query blocks are processed independently, allowing one profile to cover multiple HH.ru search modes without duplicating the common filters.

## HH.ru browser authorization

The scraper depends on an authenticated `state.json` created by Playwright.

The session is not permanent. HH.ru can invalidate an old browser session, especially after long periods of inactivity. When that happens, refresh `state.json` by running `auth_setup.py` again and logging in manually.

### NixOS / devenv

```bash
devenv shell
python auth_setup.py
```

### Other systems

```bash
pip install -r requirements.txt
playwright install chromium
python auth_setup.py
```

The script opens Chromium, lets you authenticate manually, and saves the browser state.

`state.json` is sensitive runtime state and must never be committed.

### Why a fresh session matters

Resume-based HH.ru searches can return an error page instead of the normal vacancy SERP when the saved session has expired. The scraper then cannot see vacancy cards and may appear to return zero results. Refreshing `state.json` restores the authenticated browser state.

## Deployment

The application is designed to work without Kubernetes.

### 🐳 Standalone Docker

Requirements:

- Docker;
- an HH.ru account;
- an OpenRouter API key;
- a Telegram bot token and chat ID;
- a valid authenticated `state.json`.

Clone the repository and prepare runtime files:

```bash
git clone <repository-url>
cd job-finder

mkdir -p resumes
touch config.yaml applied.db
```

Create `.env`:

```env
OPENROUTER_API_KEY=your_openrouter_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

# Optional. Attempted first for both judge and writer.
OPENROUTER_MODEL=google/gemma-4-31b-it:free
```

Generate or refresh the HH.ru session:

```bash
python auth_setup.py
```

Build and run:

```bash
docker build -t job-finder:latest .

docker run -d \
  --name job-finder \
  --restart unless-stopped \
  --env-file .env \
  -v "$PWD/config.yaml:/app/config.yaml" \
  -v "$PWD/resumes:/app/resumes" \
  -v "$PWD/applied.db:/app/applied.db" \
  -v "$PWD/state.json:/app/state.json:ro" \
  job-finder:latest
```

Logs:

```bash
docker logs -f job-finder
```

The application does not expose an HTTP port.

### Docker Compose

The repository includes `docker-compose.yml`, but the checked-in compose file is configured around the author's homelab network. It should **not** be treated as the universal standalone configuration.

For a normal Docker-only installation, use the `docker run` example above or adapt the compose file to your own network setup.

The important persistent mounts are:

```text
./config.yaml:/app/config.yaml
./resumes:/app/resumes
./applied.db:/app/applied.db
./state.json:/app/state.json:ro
```

### ☸️ Kubernetes (optional)

Kubernetes is an optional deployment target used for the author's homelab/GitOps setup. The application itself does not depend on Kubernetes and has no HTTP server.

The current deployment uses:

```text
Image: ghcr.io/vsaqv/job-finder:latest
Replicas: 1
imagePullPolicy: Always
```

The pod runs as UID `1000`, GID `100`, with a 1 GiB `/dev/shm` tmpfs for Chromium. Current resources are:

```text
requests: 200m CPU / 512Mi memory
limits:   1 CPU  / 2Gi memory
```

Kubernetes injects these credentials from `homelab-secrets`:

```text
OPENROUTER_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

`OPENROUTER_MODEL` is not required in the manifest. If it is absent from the pod environment, the application falls back to its built-in default.

The deployment mounts persistent host-side runtime state:

```text
config.yaml
applied.db
state.json
resumes/
```

`state.json` is read-only in the pod. `applied.db` must be writable by the container user; host-side ownership and permissions therefore matter for SQLite.

The deployment intentionally has no Kubernetes `Service` and no HTTP readiness/liveness probe because the application does not listen on a network port.

### Updating the Kubernetes image

The GitHub Actions workflow publishes `ghcr.io/vsaqv/job-finder:latest` whenever `main` is pushed.

Because the Deployment uses `imagePullPolicy: Always`, a running pod can be updated with:

```bash
kubectl rollout restart deployment/job-finder
kubectl rollout status deployment/job-finder
kubectl logs -f deployment/job-finder
```

An image-only push does not change the Deployment spec by itself, so an explicit rollout restart (or equivalent GitOps action) is required to make a running pod pull the new `latest` image.

## Project files

```text
job-finder/
├── .github/
│   └── workflows/
│       └── docker.yml       # Build and publish image to GHCR
├── tests/
│   └── test_model_router.py
├── main.py                  # Scraper + Telegram bot
├── model_router.py          # Model pools, validation, fallback/demotion
├── auth_setup.py            # Manual HH.ru session setup
├── Dockerfile
├── docker-compose.yml       # Homelab-oriented Docker Compose example
├── requirements.txt
├── devenv.nix
├── devenv.yaml
├── .envrc
└── LICENSE
```

Runtime-only files are normally kept outside the Git repository in production:

```text
config.yaml
applied.db
resumes/
state.json
.env
```

They are ignored by Git in the current project setup.

## Development and tests

The repository contains offline unit tests for model routing and response validation.

With `devenv`:

```bash
devenv shell -- python -m unittest discover -s tests -v
```

Python compilation check:

```bash
python -m py_compile main.py model_router.py tests/test_model_router.py
```

Build the image locally:

```bash
docker build -t job-finder:local-test .
```

## CI / image publishing

`.github/workflows/docker.yml` runs on pushes to `main` and builds/publishes:

```text
ghcr.io/vsaqv/job-finder:latest
```

The workflow uses GitHub's ephemeral `GITHUB_TOKEN` with package-write permission. Application credentials are not stored in the repository.

## Operational and security notes

- **0% ban probability** is the project's central operating principle: HH.ru is accessed in pull-only/read-only mode.
- The bot never automatically applies, messages employers, or edits HH.ru profile data.
- `state.json` contains authenticated browser state and must be treated as sensitive.
- `.env` contains credentials and must not be committed.
- Do not run two long-polling instances with the same Telegram bot token. Running a local copy and a Kubernetes copy simultaneously causes Telegram API `409 Conflict` errors.
- The SQLite database is persistent runtime state. Back it up before deleting or recreating it.
- In Kubernetes, the SQLite file must be writable by UID `1000` / GID `100`; an incorrectly owned hostPath file can produce `sqlite3.OperationalError: attempt to write a readonly database`.
- A renewed `state.json` only affects newly created browser contexts. Restart the long-running container after replacing it if the current process already created its context.

## Planned

### Separate vacancy history per profile / resume

The current SQLite model treats `id` as the global primary key. The planned schema will scope vacancy history and deduplication to the profile/resume, allowing the same HH.ru vacancy to appear independently in multiple profile histories.

Example target behavior:

```text
PM profile    → vacancy 123 → stored / reviewed / marked read
DevOps profile → vacancy 123 → separately stored / reviewed / marked read
```

### Model management through Telegram

The planned Telegram interface will allow model configuration without editing source code or redeploying the application.

The intended control surface includes changing:

- the `OPENROUTER_MODEL` override;
- task-specific fallback model selection/pools, where appropriate.

The runtime validation and fallback mechanism will remain in place regardless of how model selection is edited.

## License

MIT
