"""Add a live preview to the FPC build plan.

  add_preview.py SOURCE build_plan.txt preview_plan.txt

Moves the front controller and route table up to the end of the foundation, then
inserts the steps a developer would take to see the app running:
  - write a development .env, start MySQL in Docker, run the migrations,
  - start PHP's built-in server, create the first administrator,
  - set its password and sign in through Chrome,
and after every later feature: run the new migration and open its pages.
"""
import re
import sys
from pathlib import Path

SRC, PLAN_IN, PLAN_OUT = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

EMAIL = "admin@fpc.test"
PASSWORD = "Dev-Preview-2026!"
# after signing in the app goes to ?next= (the dashboard '/' is only built near the end)
LOGIN = f"/login?next=/profile | type {EMAIL} | tab | type {PASSWORD} | enter"
PROBE = PLAN_OUT.with_suffix(".probe.json")     # written by check_preview.py --probe
WIRING = ["config/routes.php", "public/.htaccess", "public/index.php"]
# the milestone after which the preview starts (once sign-in can work)
FOUNDATION_END = sys.argv[4] if len(sys.argv) > 4 else "## 0002"

ENV = f"""APP_NAME="FPC Eval360"
APP_ENV=development
APP_DEBUG=true
APP_URL=http://127.0.0.1:8080
APP_TIMEZONE=Africa/Blantyre
APP_KEY={{{{APP_KEY}}}}

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

PREVIEW_SETUP = [
    "",
    "## Local preview: database, dev server and the first sign-in",
    "newfile .env", *ENV.split("\n"), "endfile",
    # config/env.php insists on this file in development (it may be empty)
    "newfile .env.local", "# Machine-specific overrides of .env (git-ignored).", "endfile",
    "run 45 docker run -d --name fpc-dev-db --restart unless-stopped -p 3307:3306 "
    "-e MYSQL_ROOT_PASSWORD=devpass -e MYSQL_DATABASE=fpc_dev mysql:8.4",
    # first start of MySQL initialises the data directory; wait until it answers on TCP
    "run 60 until docker exec fpc-dev-db mysqladmin ping -h127.0.0.1 -uroot -pdevpass --silent; "
    "do sleep 3; done; echo database ready",
    "run 25 php scripts/migrate.php",
    "run 15 php scripts/seed.php reference",
    "restore run 20 docker start fpc-dev-db",
    "restore run 30 until docker exec fpc-dev-db mysqladmin ping -h127.0.0.1 -uroot -pdevpass "
    "--silent; do sleep 3; done; echo database ready",
    "serve php -S 127.0.0.1:8080 -t public public/index.php",
    "restore serve php -S 127.0.0.1:8080 -t public public/index.php",
    "browse /login",
    f'run 10 out=$(php scripts/create_admin.php --email={EMAIL} --name="Dev Admin"); '
    "echo \"$out\"; echo \"$out\" | grep -o 'http[^ ]*' | clip",
    f"browse clipboard | type {PASSWORD} | tab | type {PASSWORD} | enter",
    f"browse {LOGIN}",
    f"restore browse {LOGIN}",
    # put the dev account on both seeded projects' teams (no safeguarding role)
    "run 10 docker exec fpc-dev-db mysql -uroot -pdevpass fpc_dev -e \"INSERT IGNORE INTO "
    "user_roles (user_id, role_id, project_id) SELECT u.id, r.id, p.id FROM users u, roles r, "
    f"projects p WHERE u.email = '{EMAIL}' AND r.role_key IN ('programs_manager', "
    "'finance_officer', 'meal_officer', 'project_coordinator')\"",
]

# route table: page file -> URLs without parameters
routes = {}
for path, page in re.findall(r"^\s*'(/[^'{]*)'\s*=>\s*\['([^']+)'",
                             (SRC / "config/routes.php").read_text(encoding="utf-8"), re.M):
    if re.search(r"/(new|edit|delete|remove|download|logout|switch-project|password)", path):
        continue
    mod, _, name = page.partition("/")
    routes.setdefault(f"modules/{mod}/pages/{name}.php", []).append(path)

lines = PLAN_IN.read_text(encoding="utf-8").splitlines()
lines = [l for l in lines if l.strip() not in WIRING]

# split into milestone blocks
blocks, cur = [], []
for l in lines:
    if l.startswith("## ") and cur:
        blocks.append(cur)
        cur = []
    cur.append(l)
blocks.append(cur)

# Which pages to open after each milestone. With a probe file (from check_preview.py
# --probe) a page is opened at the first milestone where it really works; without
# one, guess from the pages typed in that milestone.
import json
probe = json.loads(PROBE.read_text(encoding="utf-8")) if PROBE.exists() else None
by_milestone = {}
if probe:
    for url, ms in probe.items():
        by_milestone.setdefault(ms, []).append(url)


backlog = []          # pages that work already but were not shown yet (max 4 per stop)


def visits_for(head, files):
    if probe is not None:
        backlog.extend(by_milestone.get(head[3:].strip(), []))
        shown = backlog[:4]
        del backlog[:4]
        return shown
    return list(dict.fromkeys(u for f in files for u in routes.get(f, [])))[:3]


out, preview_on = [], False
for block in blocks:
    head = block[0] if block and block[0].startswith("## ") else ""
    files = [l.strip() for l in block if l.strip() and not l.startswith(("#", "run "))]
    body = list(block)
    if head.startswith(FOUNDATION_END):
        while body and not body[-1].strip():
            body.pop()
        body += WIRING                      # front controller + routes before first preview
        out += body + PREVIEW_SETUP
        setup_head = next(l for l in PREVIEW_SETUP if l.startswith("## "))
        out += [f"browse {u}" for u in visits_for(setup_head, []) if u != "/profile"]
        preview_on = True
        continue
    out += body
    if preview_on and head and not head.startswith(("## Wiring", "## Documentation")):
        while out and not out[-1].strip():
            out.pop()
        if any(f.startswith("migrations/") for f in files):
            out.append("run 20 php scripts/migrate.php")
        for u in visits_for(head, files):
            out.append(f"browse {u}")
        out.append("")

out += ["", "## Finished system: a last look around"]
out += [f"browse {u}" for u in backlog] + ["browse /"]
PLAN_OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
n_browse = sum(1 for l in out if l.startswith("browse "))
print(f"{PLAN_OUT}: {n_browse} preview visits in Chrome")
