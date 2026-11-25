"""Generate a development-order build plan for the FPC PHP project.

Order follows the project's real history: its numbered migrations. Every file is
placed in the group of the latest migration whose tables it touches, after any
file it require()s, so each feature is built migration -> lib -> pages -> tests.
"""
import re
import sys
from pathlib import Path

SRC = Path(sys.argv[1])
PLAN_IN = Path(sys.argv[2])       # replay_plan.txt from --dry-run (files to type)
OUT = Path(sys.argv[3])

files = [l.strip() for l in PLAN_IN.read_text(encoding="utf-8").splitlines()
         if l.strip() and not l.startswith("#")]
text = {f: (SRC / f).read_text(encoding="utf-8", errors="replace") for f in files}

migs = sorted(f for f in files if f.startswith("migrations/"))
mig_no = {m: int(Path(m).name[:4]) for m in migs}
table_mig = {}
for m in migs:
    for t in re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?", text[m], re.I):
        table_mig.setdefault(t.lower(), mig_no[m])

SQL_RE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+`?(\w+)`?", re.I)
HELPER_RE = re.compile(r"\bdb_(?:insert|update|delete|upsert)\w*\(\s*['\"](\w+)['\"]")


def table_group(f):
    """Main feature of a file: the migration creating most of the tables it uses."""
    names = {n.lower() for n in SQL_RE.findall(text[f]) + HELPER_RE.findall(text[f])}
    counts = {}
    for n in names:
        if n in table_mig:
            counts[table_mig[n]] = counts.get(table_mig[n], 0) + 1
    if not counts:
        return 0
    if any(g > 2 for g in counts):
        counts = {g: c for g, c in counts.items() if g > 2}
    best = max(counts.values())
    return min(g for g, c in counts.items() if c == best)


REQ_RE = re.compile(r"require(?:_once)?\s*\(?\s*(APP_ROOT|__DIR__)\s*\.\s*['\"]([^'\"]+)['\"]")


def requires(f):
    out = []
    for anchor, r in REQ_RE.findall(text[f]):
        start = Path(f).parent.as_posix().split("/") if anchor == "__DIR__" else []
        parts = [p for p in start if p not in ("", ".")]
        for seg in r.split("/"):
            if seg == "..":
                if parts:
                    parts.pop()
            elif seg not in (".", ""):
                parts.append(seg)
        p = "/".join(parts)
        if p in text:
            out.append(p)
    return out


# Manual anchors for files whose place is about architecture, not tables.
SETUP = [".gitignore", ".env.example", "composer.json", ".htaccess", "CLAUDE.md"]
CORE = [
    "bootstrap/helpers.php", "config/env.php", "config/app.php", "config/database.php",
    "config/features.php", "bootstrap/init.php", "db/connection.php", "db/query.php",
    "db/migrate.php", "scripts/generate_key.php", "scripts/migrate.php",
    "lib/http.php", "lib/router.php", "lib/crypto.php", "lib/forms.php",
    "DESIGN_SYSTEM.md", "lib/layout.php", "lib/coming_soon.php",
    "tests/lib/assert.php", "tests/run.php", "tests/unit/EnvLoaderTest.php",
    "tests/docker/Dockerfile", "scripts/test.sh",
]
LAST = ["config/routes.php", "public/.htaccess", "public/index.php"]
TAIL_DOCS = [f for f in files if f.startswith("docs/")] + ["README.md"]

fixed = set(SETUP + CORE + LAST + TAIL_DOCS)
group = {}
for f in files:
    if f in fixed:
        continue
    if f.startswith("migrations/"):
        group[f] = mig_no[f]
    else:
        group[f] = table_group(f)

# tests/lib helpers belong with the harness in the foundation.
for f in files:
    if f.startswith("tests/lib/") and f not in fixed:
        group[f] = 1

def is_lib_mod(f):
    return f.startswith("modules/") and "/lib/" in f


def module_of(f):
    parts = f.split("/")
    return parts[1] if f.startswith("modules/") and len(parts) > 2 else None


for f, g in group.items():
    if g == 0:
        group[f] = 1

# Pages are built with (or after) their module's code.
mod_group = {}
for f in group:
    m = module_of(f)
    if m and "/lib/" in f:
        main = f"modules/{m}/lib/{m}.php"
        if f == main or m not in mod_group:
            mod_group[m] = group[f]
for f in group:
    m = module_of(f)
    if m and "/pages/" in f and m in mod_group:
        group[f] = max(group[f], mod_group[m])

# Name hints for tests and scripts (they call functions rather than query tables).
group["modules/capacity/lib/capacity.php"] = 13
for f in group:
    if f.startswith("modules/capacity/pages/"):
        group[f] = max(group[f], 13)
keys = {}
for m in migs:
    slug = Path(m).stem[5:].replace("_", "")
    keys[slug] = mig_no[m]
for f in group:
    if is_lib_mod(f):
        keys.setdefault(Path(f).stem.replace("_", ""), group[f])
        keys.setdefault(module_of(f), group[f])
keys.update({"notify": 15, "safeguardingmode": 12, "capacitydomains": 17,
             "indicatorrules": 19, "mealregisters": 20, "phase4": 9, "phase56": 14,
             "phases567": 15, "phase7": 15, "evidenceimport": 8, "fpcmodules": 21,
             "questionnaire": 22})
for f in ("modules/onboarding/lib/questionnaire.php", "modules/onboarding/pages/questionnaire.php"):
    if f in group:
        group[f] = 22
for f in group:
    if f.startswith(("tests/integration/", "tests/http/", "tests/unit/", "scripts/")):
        name = Path(f).stem.lower().replace("_", "").replace("httptest", "").replace("test", "")
        for k in sorted(keys, key=len, reverse=True):
            if k and k in name:
                group[f] = max(group[f], keys[k])
                break


# Shared lib/ code must exist before the first file that require()s it.
for _ in range(20):
    changed = False
    for f in list(group) + [x for x in files if x in fixed]:
        if not f.endswith(".php"):
            continue
        g = group.get(f, 1)
        for d in requires(f):
            if d.startswith("lib/") and d in group and group[d] > g:
                group[d] = g
                changed = True
    if not changed:
        break

# Files that touch no table: foundation (group 1), except obvious late tooling.
for f, g in group.items():
    if g == 0:
        group[f] = 1


def kind_rank(f):
    if f.startswith("migrations/"):
        return 0
    if f.startswith("lib/"):
        return 1
    if "/lib/" in f and f.startswith("modules/"):
        return 2
    if f.startswith("modules/"):
        return 3
    if f.startswith("scripts/"):
        return 4
    if f.startswith("tests/unit/"):
        return 5
    if f.startswith("tests/integration/"):
        return 6
    if f.startswith("tests/http/"):
        return 7
    return 8


PHASE_END = {2, 6, 8, 12, 13, 14, 22}
TITLES = {}
for m in migs:
    first = re.search(r"\*\s*\d{4}\s*\S+\s*(.+)", text[m])
    TITLES[mig_no[m]] = (first.group(1).strip().rstrip(",.") if first else Path(m).stem)[:70]

out = ["# Build plan - typed in this order. Lines: '## title' = milestone,",
       "# 'run <command>' = run in the VS Code terminal, anything else = a file to type.",
       "", "## Project setup"]
out += [f for f in SETUP if f in text]
out += ["", "## Core: configuration, database layer, HTTP, router, layout, test harness"]
out += [f for f in CORE if f in text]
out += ["run php -l bootstrap/init.php", "run php tests/run.php unit"]

for g in sorted(set(group.values())):
    members = sorted((f for f in group if group[f] == g), key=lambda f: (kind_rank(f), f))
    title = TITLES.get(g, "Foundation")
    out += ["", f"## {g:04d} - {title}"]
    out += members
    lint = [f for f in members if f.endswith(".php") and not f.startswith("tests/")][:3]
    out += [f"run php -l {f}" for f in lint]
    if g in PHASE_END:
        out.append("run php tests/run.php unit")

out += ["", "## Wiring: routes and front controller"] + [f for f in LAST if f in text]
out += ["run php -l public/index.php", "run php tests/run.php unit", "run bash scripts/test.sh"]
out += ["", "## Documentation"] + [f for f in TAIL_DOCS if f in text]

listed = {l for l in out if l and not l.startswith(("#", "run "))}
missing = [f for f in files if f not in listed]
if missing:
    out += ["", "## Remaining files"] + missing
OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"{len(listed | set(missing))} files, {len(set(group.values()))} feature groups -> {OUT}")
