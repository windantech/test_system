"""Build the recording plan: setup pre-loaded, then the system built and tested on screen.

  build_video_plan.py SOURCE login_first_plan.txt video_plan.txt

Structure of the result
  1. Pre-loaded (placed instantly before recording): .env, .env.local, .env.example,
     .gitignore, composer.json, .htaccess files, config/*.php
  2. Development environment: MySQL started in Docker from the terminal
  3. Sign-in built (core, auth, first migrations, auth pages)
  4. First run: migrations, reference data, dev server, admin, sign-in in Chrome
  5. Every feature: its files, its migration, its tests typed and run, its pages in Chrome
  6. Full test suite, a last look around, documentation

If video_plan.probe.json / video_plan.runs.json exist (written by check_preview.py),
pages are opened at the first milestone where they work, and only test runs that
passed at that point are kept, each with a wait matching how long it really took.
"""
import json
import re
import sys
from pathlib import Path

SRC, PLAN_IN, PLAN_OUT = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
PROBE = PLAN_OUT.with_suffix(".probe.json")
RUNS = PLAN_OUT.with_suffix(".runs.json")
probe = json.loads(PROBE.read_text(encoding="utf-8")) if PROBE.exists() else None
runs = json.loads(RUNS.read_text(encoding="utf-8")) if RUNS.exists() else None

EMAIL, PASSWORD = "admin@fpc.test", "Dev-Preview-2026!"
LOGIN = f"/login?next=/profile | type {EMAIL} | tab | type {PASSWORD} | enter"
DB_WAIT = ("until docker exec fpc-dev-db mysqladmin ping -h127.0.0.1 -uroot -pdevpass --silent; "
           "do sleep 3; done; echo database ready")
SERVE = "php -S 127.0.0.1:8080 -t public public/index.php"

PRELOAD = [".env.example", ".gitignore", "composer.json", ".htaccess", "public/.htaccess"]
PRELOAD += sorted(p.relative_to(SRC).as_posix() for p in (SRC / "config").glob("*.php"))
# test data the test suite reads (fixtures) and the empty storage folders: data, not code
PRELOAD += sorted(p.relative_to(SRC).as_posix() for p in (SRC / "tests/fixtures").rglob("*")
                  if p.is_file())
PRELOAD += sorted(p.relative_to(SRC).as_posix() for p in (SRC / "storage").rglob(".gitkeep"))
PRELOAD = [p for p in PRELOAD if (SRC / p).is_file()]

ENV = """APP_NAME="FPC Eval360"
APP_ENV=development
APP_DEBUG=true
APP_URL=http://127.0.0.1:8080
APP_TIMEZONE=Africa/Blantyre
APP_KEY={{APP_KEY}}

CLIENT_SLUG=fpc
CLIENT_NAME="FPC"
CLIENT_FULL_NAME="Future Planning for the Child"
CLIENT_COUNTRY=MW
CLIENT_CURRENCY=MWK

DB_HOST=127.0.0.1
DB_PORT=3307
DB_NAME=fpc_dev
DB_USER=root
DB_PASS=devpass
DB_CHARSET=utf8mb4

SESSION_SECURE_COOKIE=false
SESSION_IDLE_MIN=240
SESSION_ABSOLUTE_HOURS=12

RATE_LIMIT_LOGIN=5
RATE_LIMIT_LOCKOUT_MINUTES=15
MAIL_ENABLED=false

STORAGE_BACKEND=local
STORAGE_PATH=storage/uploads
STORAGE_MAX_UPLOAD_MB=20
LOG_LEVEL=debug"""

# ---------------------------------------------------------------- read input plan
blocks, cur = [], None
for l in PLAN_IN.read_text(encoding="utf-8").splitlines():
    if l.startswith("## "):
        cur = [l[3:].strip(), []]
        blocks.append(cur)
    elif cur is not None and l.strip() and not l.startswith("#"):
        s = l.strip()
        if s in PRELOAD:
            continue
        if s.startswith("run ") and ("tests/run.php" in s or "test.sh" in s):
            continue                      # test runs are re-planned below
        cur[1].append(s)
blocks = [b for b in blocks if any(not x.startswith("run ") for x in b[1])]

# --------------------------------------------------------------- helpers
by_ms = {}
for url, ms in (probe or {}).items():
    by_ms.setdefault(ms, []).append(url)
backlog = []


def pages(ms):
    if probe is None:
        return []
    backlog.extend(by_ms.get(ms, []))
    shown = backlog[:4]
    del backlog[:4]
    return [f"browse {u}" for u in shown]


def test_run(ms, cmd, default_wait):
    """A test command, kept only if it passed here in the last check."""
    if runs is not None:
        r = runs.get(f"{ms}|{cmd}")
        if r is None or not r["ok"]:
            return []
        return [f"run {int(r['secs'] * 1.5) + 30} {cmd}"]   # timings vary between runs
    return [f"run {default_wait} {cmd}"]


def tests_for(ms, files):
    out = []
    if any(f.startswith("tests/unit/") for f in files) or ms.startswith("Test harness"):
        out += test_run(ms, "php tests/run.php unit", 30)
    if any(f.startswith("tests/integration/") for f in files):
        out += test_run(ms, "bash scripts/test.sh integration", 240)
    if any(f.startswith("tests/http/") for f in files):
        out += test_run(ms, "bash scripts/test.sh http", 240)
    return out


# --------------------------------------------------------------- write plan
out = ["# Recording plan: setup is pre-loaded; the system is built, run and tested on screen.", ""]
out += ["## Pre-loaded setup (placed before recording, not typed)"]
out += [f"preload {p}" for p in PRELOAD]
out += ["preloadfile .env", *ENV.split("\n"), "endfile"]
out += ["preloadfile .env.local", "# Machine-specific overrides of .env (git-ignored).", "endfile"]

out += ["", "## Development environment: MySQL database in Docker",
        "run 45 docker run -d --name fpc-dev-db --restart unless-stopped -p 3307:3306 "
        "-e MYSQL_ROOT_PASSWORD=devpass -e MYSQL_DATABASE=fpc_dev mysql:8.4",
        f"run 60 {DB_WAIT}",
        "restore run 20 docker start fpc-dev-db",
        f"restore run 30 {DB_WAIT}"]

docs = []
first = True
for ms, items in blocks:
    if ms.startswith("Project documentation"):
        docs = items
        continue
    files = [x for x in items if not x.startswith("run ")]
    out += ["", f"## {ms}"] + items
    if first:                               # the sign-in stage: bring the app up
        first = False
        fr = "First run: migrations, dev server and signing in"
        out += ["", f"## {fr}",
                "run 25 php scripts/migrate.php",
                "run 15 php scripts/seed.php reference",
                f"serve {SERVE}",
                f"restore serve {SERVE}",
                "browse /login",
                f'run 10 out=$(php scripts/create_admin.php --email={EMAIL} --name="Dev Admin"); '
                "echo \"$out\"; echo \"$out\" | grep -o 'http[^ ]*' | clip",
                f"browse clipboard | type {PASSWORD} | tab | type {PASSWORD} | enter",
                f"browse {LOGIN}",
                f"restore browse {LOGIN}",
                "run 10 docker exec fpc-dev-db mysql -uroot -pdevpass fpc_dev -e \"INSERT IGNORE "
                "INTO user_roles (user_id, role_id, project_id) SELECT u.id, r.id, p.id FROM users u, "
                f"roles r, projects p WHERE u.email = '{EMAIL}' AND r.role_key IN "
                "('programs_manager', 'finance_officer', 'meal_officer', 'project_coordinator')\""]
        out += [p for p in pages(fr) if p != "browse /profile"]
        continue
    if any(f.startswith("migrations/") for f in files):
        out.append("run 20 php scripts/migrate.php")
    out += tests_for(ms, files)
    out += pages(ms)

out += ["", "## Full test suite: every test against a fresh database",
        "run 10 php tests/run.php unit"]
out += test_run("Full test suite: every test against a fresh database", "bash scripts/test.sh", 300) \
    if runs is not None else ["run 300 bash scripts/test.sh"]
out += ["", "## Finished system: a last look around"]
out += [f"browse {u}" for u in backlog] + ["browse /"]
if docs:
    out += ["", "## Project documentation"] + docs
PLAN_OUT.write_text("\n".join(out) + "\n", encoding="utf-8")

n = lambda p: sum(1 for l in out if l.startswith(p))  # noqa: E731
print(f"{PLAN_OUT}: {n('preload')} pre-loaded, {n('browse ')} Chrome visits, "
      f"{sum(1 for l in out if l.startswith('run') and 'test' in l)} test runs")
