"""Reorder a build plan so the video starts by building sign-in.

  login_first.py SOURCE build_plan.txt login_first_plan.txt

Stage 1 is only what signing in needs: the files the front controller, the auth
pages and the setup scripts require (followed transitively), plus the first two
migrations. Everything else keeps its feature order afterwards, and project
documentation (*.md) moves to the very end.
"""
import re
import sys
from pathlib import Path

SRC, PLAN_IN, PLAN_OUT = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

START = [
    ".gitignore", "composer.json", ".env.example", ".htaccess",
    "public/.htaccess", "public/index.php", "config/routes.php",
    "migrations/0001_foundation.php", "migrations/0002_organisation_roles_permissions.php",
    "modules/auth/pages/login.php", "modules/auth/pages/logout.php",
    "modules/auth/pages/reset.php", "modules/auth/pages/forgot.php",
    "modules/auth/pages/change.php", "modules/auth/pages/profile.php",
    "scripts/migrate.php", "scripts/create_admin.php", "scripts/seed.php",
    "scripts/generate_key.php",
    # loaded by name through config(), so not visible as require()s
    "config/app.php", "config/database.php", "config/features.php",
    "lib/forms.php", "lib/crypto.php",
]
REQ_RE = re.compile(r"require(?:_once)?\s*\(?\s*(APP_ROOT|__DIR__)\s*\.\s*['\"]([^'\"]+)['\"]")


def requires(f):
    text = (SRC / f).read_text(encoding="utf-8", errors="replace")
    out = []
    for anchor, r in REQ_RE.findall(text):
        parts = [p for p in Path(f).parent.as_posix().split("/") if p not in ("", ".")] \
            if anchor == "__DIR__" else []
        for seg in r.split("/"):
            if seg == "..":
                parts and parts.pop()
            elif seg not in (".", ""):
                parts.append(seg)
        out.append("/".join(parts))
    return out


need, todo = set(), list(START)
while todo:
    f = todo.pop()
    if f in need or not (SRC / f).is_file():
        continue
    need.add(f)
    if f.endswith(".php"):
        todo += requires(f)

lines = PLAN_IN.read_text(encoding="utf-8").splitlines()
order = [l.strip() for l in lines if l.strip() and not l.startswith(("#", "run "))]
order_index = {f: i for i, f in enumerate(order)}
stage1 = sorted((f for f in need if f in order_index), key=lambda f: order_index[f])
docs = [f for f in order if f.endswith(".md")]
moved = set(stage1) | set(docs)

# stage 1 in a natural order: setup, config/bootstrap/db, lib, migrations, pages, wiring, scripts
def rank(f):
    for i, p in enumerate((("." ,"composer.json"), ("config/", "bootstrap/", "db/"), ("lib/",),
                           ("migrations/",), ("modules/",), ("public/",), ("scripts/",))):
        if f.startswith(p) and not (f == "config/routes.php" and i == 1):
            return i
    return 5 if f == "config/routes.php" else 7


stage1.sort(key=lambda f: (rank(f), order_index[f]))
out = ["# Login-first build plan: sign-in is built and working first, then each feature.",
       "", "## Sign-in: configuration, database, router, layout and authentication"]
out += stage1
out += ["run php -l lib/auth.php", "run php -l modules/auth/pages/login.php"]

for l in lines:
    s = l.strip()
    if s in moved or (s.startswith("#") and not s.startswith("## ")):
        continue
    out.append(l)

# collapse empty milestones (heading followed directly by another heading / end)
RENAME = {"## Core: configuration, database layer, HTTP, router, layout, test harness":
          "## Test harness: a test runner and the first unit tests"}
clean = []
for l in out:
    l = RENAME.get(l, l)
    if l.startswith("## "):
        while clean and not clean[-1].strip():
            clean.pop()
        if clean and clean[-1].startswith("## "):
            clean.pop()                       # previous milestone ended up empty
        clean.append("")
    clean.append(l)
while clean and (clean[-1].startswith("## ") or not clean[-1].strip()):
    clean.pop()
clean += ["", "## Project documentation"] + docs
PLAN_OUT.write_text("\n".join(clean) + "\n", encoding="utf-8")

chars = sum((SRC / f).stat().st_size for f in stage1)
print(f"Stage 1 (sign-in): {len(stage1)} files, {chars:,} bytes")
for f in stage1:
    print("   ", f)
