# 🤖 HH Job Finder

[🇬🇧 English](README.md) · [🇷🇺 Русский](README.ru.md)

Pull-only ассистент для поиска вакансий на [HH.ru](https://hh.ru/).

<p align="left">
  <img src="https://img.shields.io/badge/Ban_Probability-0%25-green?style=for-the-badge&logo=shield" alt="0% Ban Probability">
</p>

HH Job Finder использует настоящий браузер Chromium через Playwright для поиска вакансий, отдельные правила для каждого профиля, LLM через OpenRouter для оценки вакансий и генерации сопроводительных писем, SQLite для истории обработки и Telegram как интерфейс управления и просмотра результатов.

Проект изначально спроектирован вокруг принципа **0% вероятности бана**: он только читает данные с HH.ru и никогда не отправляет отклики, сообщения работодателям и не изменяет профиль на HH.ru.

> **Kubernetes не требуется.** Основной переносимый способ запуска — Docker. Kubernetes поддерживается как дополнительный вариант для homelab/GitOps.

## Как это работает

```text
Аутентифицированная сессия HH.ru
               │
               ▼
       Поиск по резюме
               │
               ▼
  Фильтры профиля + query blocks
               │
               ▼
       Список вакансий
               │
               ▼
Описание вакансии + рейтинг работодателя
               │
               ▼
       LLM judge: YES / NO
          │              │
        NO             YES
          │              │
          ▼              ▼
       сохранить       LLM writer
       как обработанную    │
                           ▼
                     История SQLite
                           │
                           ▼
                    Просмотр в Telegram
```

Скрапер работает в отдельном background thread. Telegram polling работает независимо в основном потоке, поэтому бот остаётся доступным, пока идёт цикл сканирования.

После обработки всех активных профилей скрапер засыпает на 4 часа и затем начинает следующий цикл.

## Что уже умеет

### Сбор вакансий

- Ищет вакансии на HH.ru через сохранённую аутентифицированную browser session.
- Обрабатывает каждый включённый профиль отдельно.
- Строит URL поиска HH.ru из `resume_id`, `global_filters` и query blocks профиля.
- Поддерживает несколько query blocks на один профиль; блоки обрабатываются последовательно и используют общие глобальные фильтры.
- Просматривает до `pages_to_scrape` страниц для каждого query block.
- Сортирует результаты HH.ru по времени публикации.
- Извлекает название вакансии, ID, URL, компанию, число откликов и fingerprint `компания + название`.
- Открывает ещё не обработанные вакансии и читает полное описание.
- Читает рейтинг работодателя, когда HH.ru его показывает.
- Использует ограниченные human-like задержки и прокрутку между действиями браузера.
- Перед подробной проверкой сортирует собранные вакансии по числу откликов.

### Фильтрация по профилю

У каждого профиля свои:

- `resume_id` на HH.ru;
- текст резюме;
- поисковые фильтры и query blocks;
- strict requirements;
- контактная информация;
- состояние `enabled`;
- лимит страниц.

Поэтому один сервис может одновременно использовать разные стратегии поиска, например профиль для PM/операционки и профиль для DevOps/инфраструктуры.

### LLM judge

Judge получает описание вакансии и strict requirements выбранного профиля и решает, соответствует ли вакансия всем заданным критериям.

Ответ валидируется как один токен `YES` / `NO`. Русские `ДА` / `НЕТ` также распознаются нормализатором.

Если запрос к модели завершился ошибкой или ответ имеет неверный формат, цикл не останавливается: система пробует следующую модель из fallback pool.

### LLM writer

Для вакансий, прошедших judge, запускается отдельная задача writer.

Writer получает:

1. выбранное резюме кандидата;
2. точное название вакансии;
3. описание вакансии;
4. контактную информацию профиля.

Текущие правила writer намеренно узкие:

- ровно 3 коротких абзаца;
- прямое начало с фокусом на конкретную вакансию;
- 1–2 самых релевантных факта из резюме вместо пересказа всего CV;
- сухой, прямой и естественный русский язык;
- без AI-клише, корпоративного новояза и пустых формальностей;
- без выдуманных личных или профессиональных фактов;
- контактная информация добавляется из данных профиля.

Дополнительно работает лёгкий Russian-sanity validator. Если результат не проходит проверку, запускается следующая модель.

### Маршрутизация моделей и fallback

В репозитории используется небольшой curated free-model pool вместо динамического прогона большого количества моделей на каждом цикле.

Текущие модели:

```text
qwen/qwen3.8-27b:free
thinkingmachines/inkling-small:free
thinkingmachines/inkling:free
nvidia/nemotron-3-ultra-550b-a55b:free
```

Один и тот же curated набор сейчас доступен и для `judge`, и для `writer`. Разное поведение обеспечивается runtime-валидацией:

- judge → проверка ответа как `YES` / `NO`;
- writer → проверка русского текста сопроводительного письма.

Модель, которая падает во время работы, временно демотируется в памяти на 30 минут. Это позволяет не пытаться постоянно использовать сломанный free endpoint и не требует дополнительного probing traffic.

### `OPENROUTER_MODEL`

`OPENROUTER_MODEL` — необязательный runtime override и намеренно используется **одинаково для judge и writer**.

Если переменная задана, эта модель пробуется первой для обеих задач. При ошибке inference или невалидном ответе система переходит к обычному curated pool.

Если переменная не задана, используется встроенное значение:

```text
OPENROUTER_MODEL=google/gemma-4-31b-it:free
```

Название модели не является секретом, поэтому Kubernetes manifest не обязан его содержать.

### SQLite: история и дедупликация

Обработанные вакансии сохраняются в SQLite с такими логическими полями:

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

Текущее поведение:

- ID вакансии считается обработанным навсегда;
- одинаковый fingerprint компании + названия блокирует дубликат на 30 дней;
- непрочитанные принятые вакансии доступны через Telegram 3 дня;
- отклонённые вакансии сохраняются как обработанные, но не показываются в unread review;
- сопроводительное письмо хранится вместе с записью вакансии.

### Текущее ограничение истории

Сейчас `id` вакансии является **глобальным** primary key. Поэтому одну и ту же вакансию пока нельзя сохранить как две независимые записи для двух разных профилей.

Это известное ограничение и оно находится в разделе **Planned** ниже.

## Telegram-бот

Telegram — основной runtime-интерфейс управления сервисом.

| Действие | Назначение |
| :--- | :--- |
| `🟢/🔴 Toggle Profiles` | Включить или выключить отдельные профили. |
| `📂 View Unread (3 Days)` | Просмотреть принятые вакансии и сопроводительные письма за последние 3 дня. Просмотр помечает записи как прочитанные. |
| `⚙️ Edit Strict Requirements` | Заменить strict requirements выбранного профиля. |
| `📄 Upload Resume` | Загрузить `.txt` резюме в смонтированную директорию `resumes/`. |
| `➕ Add Profile via YAML` | Добавить профиль, отправив один YAML mapping. |
| `❌ Delete Profile` | Удалить профиль из `config.yaml`. |
| `🔙 Cancel` | Отменить многошаговое действие. |

### Add Profile via YAML

Бот ожидает **один mapping профиля**, а не весь список `profiles:`.

Корректная форма:

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

Не отправляй:

```yaml
profiles:
  - name: "devops"
```

и не отправляй YAML-list, начинающийся с `- name:`. Telegram handler разбирает сообщение как один объект профиля и затем добавляет его в `config.yaml`.

## Настройка профилей

`config.yaml` — persistent runtime configuration. Если файла нет, приложение создаёт пустой каркас `profiles`, но для реального поиска нужен хотя бы один включённый профиль.

Основные поля:

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

`resume_id` — идентификатор резюме на HH.ru, который используется для private resume-based search URL. `resume_title` — метаданные профиля; для самого поиска используется `resume_id`.

`global_filters` применяются к каждому query block. Query blocks обрабатываются независимо, поэтому один профиль может одновременно охватывать несколько вариантов поиска без копирования общих фильтров.

## Авторизация HH.ru

Для работы скрапер использует аутентифицированный `state.json`, созданный Playwright.

Сессия не является вечной. HH.ru может инвалидировать старую browser session, особенно после долгого периода бездействия. В таком случае нужно снова запустить `auth_setup.py` и войти вручную.

### NixOS / devenv

```bash
devenv shell
python auth_setup.py
```

### Другие системы

```bash
pip install -r requirements.txt
playwright install chromium
python auth_setup.py
```

Скрипт открывает Chromium, после ручной авторизации сохраняет browser state.

`state.json` содержит чувствительные данные сессии и никогда не должен попадать в Git.

### Почему нужна свежая сессия

При истёкшей сессии resume-based search HH.ru может вернуть страницу ошибки вместо обычной выдачи вакансий. Тогда scraper не видит карточки вакансий и может выглядеть так, будто он возвращает ноль результатов. Обновление `state.json` восстанавливает аутентифицированное состояние браузера.

## Развёртывание

Приложение специально рассчитано на запуск без Kubernetes.

### 🐳 Обычный Docker

Требования:

- Docker;
- аккаунт HH.ru;
- ключ OpenRouter;
- Telegram bot token и chat ID;
- валидный аутентифицированный `state.json`.

Клонируй репозиторий и подготовь runtime-файлы:

```bash
git clone <repository-url>
cd job-finder

mkdir -p resumes
touch config.yaml applied.db
```

Создай `.env`:

```env
OPENROUTER_API_KEY=your_openrouter_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

# Optional. Attempted first for both judge and writer.
OPENROUTER_MODEL=google/gemma-4-31b-it:free
```

Получи или обнови browser session:

```bash
python auth_setup.py
```

Собери и запусти контейнер:

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

Логи:

```bash
docker logs -f job-finder
```

Приложение не открывает HTTP-порт.

### Docker Compose

В репозитории есть `docker-compose.yml`, но checked-in compose-файл ориентирован на homelab-сеть автора. Его **не следует считать универсальной standalone-конфигурацией**.

Для обычного Docker-only запуска используй пример `docker run` выше либо адаптируй compose под свою сеть.

Главные persistent mounts:

```text
./config.yaml:/app/config.yaml
./resumes:/app/resumes
./applied.db:/app/applied.db
./state.json:/app/state.json:ro
```

### ☸️ Kubernetes (опционально)

Kubernetes — дополнительный вариант развёртывания для homelab/GitOps. Само приложение не зависит от Kubernetes и не имеет HTTP-сервера.

Текущий deployment использует:

```text
Image: ghcr.io/vsaqv/job-finder:latest
Replicas: 1
imagePullPolicy: Always
```

Pod работает с UID `1000`, GID `100`, а для Chromium используется tmpfs `/dev/shm` размером 1 GiB. Текущие ресурсы:

```text
requests: 200m CPU / 512Mi memory
limits:   1 CPU  / 2Gi memory
```

Kubernetes получает следующие секреты из `homelab-secrets`:

```text
OPENROUTER_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

`OPENROUTER_MODEL` не нужен в manifest. Если переменной нет в окружении pod, приложение использует встроенный default.

Persistent host-side state монтируется как:

```text
config.yaml
applied.db
state.json
resumes/
```

`state.json` внутри pod доступен только для чтения. `applied.db` должен быть доступен контейнерному пользователю на запись; поэтому права и владелец hostPath-файла имеют значение.

В deployment намеренно нет `Service` и HTTP readiness/liveness probes, потому что приложение не слушает TCP-порт.

### Обновление K8s image

GitHub Actions публикует `ghcr.io/vsaqv/job-finder:latest` при push в `main`.

Так как Deployment использует `imagePullPolicy: Always`, после публикации нового image можно выполнить:

```bash
kubectl rollout restart deployment/job-finder
kubectl rollout status deployment/job-finder
kubectl logs -f deployment/job-finder
```

Сам по себе push нового image не изменяет Deployment spec, поэтому running pod нужно явно перезапустить (или выполнить эквивалентное GitOps-действие), чтобы он получил свежий `latest`.

## Файлы проекта

```text
job-finder/
├── .github/
│   └── workflows/
│       └── docker.yml       # Build и publish image в GHCR
├── tests/
│   └── test_model_router.py
├── main.py                  # Scraper + Telegram bot
├── model_router.py          # Model pools, validation, fallback/demotion
├── auth_setup.py            # Ручная настройка HH.ru session
├── Dockerfile
├── docker-compose.yml       # Homelab-oriented Docker Compose example
├── requirements.txt
├── devenv.nix
├── devenv.yaml
├── .envrc
└── LICENSE
```

Runtime-файлы обычно хранятся вне Git:

```text
config.yaml
applied.db
resumes/
state.json
.env
```

В текущей конфигурации они игнорируются Git.

## Разработка и тесты

В репозитории есть offline unit tests для model routing и валидации ответов.

Через `devenv`:

```bash
devenv shell -- python -m unittest discover -s tests -v
```

Проверка компиляции Python:

```bash
python -m py_compile main.py model_router.py tests/test_model_router.py
```

Локальная сборка image:

```bash
docker build -t job-finder:local-test .
```

## CI / публикация image

`.github/workflows/docker.yml` запускается при push в `main` и собирает/публикует:

```text
ghcr.io/vsaqv/job-finder:latest
```

Workflow использует временный GitHub `GITHUB_TOKEN` с правом публикации packages. Секреты приложения в репозиторий не помещаются.

## Операционные и security notes

- **0% вероятность бана** — центральный принцип работы проекта: HH.ru используется только в pull-only/read-only режиме.
- Бот никогда автоматически не отправляет отклики, сообщения работодателям и не редактирует профиль HH.ru.
- `state.json` содержит authenticated browser state и должен считаться sensitive.
- `.env` содержит credentials и не должен коммититься.
- Нельзя одновременно запускать два long-polling экземпляра с одним Telegram bot token. Одновременный локальный и Kubernetes экземпляр вызовут Telegram API `409 Conflict`.
- SQLite — persistent runtime state. Перед удалением/пересозданием базы делай backup.
- В Kubernetes SQLite-файл должен быть writable для UID `1000` / GID `100`; иначе возможна ошибка `sqlite3.OperationalError: attempt to write a readonly database`.
- Новый `state.json` используется только при создании нового browser context. После замены файла запущенному long-running контейнеру может потребоваться restart.

## Planned

### Раздельная история вакансий по профилям / резюме

Сейчас SQLite рассматривает `id` вакансии как глобальный primary key. Планируется изменить схему так, чтобы история и дедупликация были привязаны к конкретному профилю/резюме.

Целевое поведение:

```text
PM profile     → vacancy 123 → отдельная история / unread / read
DevOps profile → vacancy 123 → отдельная история / unread / read
```

### Управление моделями через Telegram

Планируется добавить в Telegram настройку моделей без изменения source code и без redeploy приложения.

Предполагаемый интерфейс должен позволять менять:

- override `OPENROUTER_MODEL`;
- fallback model selection / pools для отдельных задач, где это имеет смысл.

Runtime validation и fallback останутся на месте независимо от того, как пользователь редактирует выбор моделей.

## License

MIT
