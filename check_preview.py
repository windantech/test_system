"""Dry-run a build plan's live preview without touching VS Code, Chrome or the mouse.

  check_preview.py SOURCE PLAN WORKDIR [--exclude ...]

Copies the files into WORKDIR in plan order, really runs the plan's terminal commands
(on separate ports and a separate Docker container), and at every 'browse' step loads
the page over HTTP, filling and submitting forms the way the browse actions describe.
Reports which previews would show an error at that point of the build.
"""
import argparse
import base64
import html.parser
import http.cookiejar
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import code_replay as cr  # noqa: E402

BASH = r"C:\Program Files\Git\bin\bash.exe"
SUBS = [("fpc-dev-db", "fpc-check-db"), ("3307", "3317"), ("127.0.0.1:8080", "127.0.0.1:8090")]
ERROR_RE = re.compile(r"(Fatal error|Uncaught|Parse error|Warning:</b>|Warning: |Notice: |"
                      r"Deprecated: |Stack trace|undefined function|Call to undefined)", re.I)


def sub(s):
    for a, b in SUBS:
        s = s.replace(a, b)
    return s


class Forms(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms, self.cur = [], None
        self.title, self._in_title = "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.cur = {"action": a.get("action", ""), "method": a.get("method", "get").lower(),
                        "hidden": [], "fields": []}
            self.forms.append(self.cur)
        elif tag in ("input", "textarea", "select") and self.cur is not None and a.get("name"):
            if a.get("type") == "hidden":
                self.cur["hidden"].append((a["name"], a.get("value", "")))
            elif a.get("type") not in ("submit", "button", "checkbox", "radio", "file"):
                self.cur["fields"].append(a["name"])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "form":
            self.cur = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


class Browser:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, url, data=None):
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None)
        try:
            r = self.op.open(req, timeout=30)
            return r.status, r.geturl(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, url, e.read().decode("utf-8", "replace")
        except Exception as e:                       # connection refused etc.
            return 0, url, str(e)


def run_cmd(cmd, cwd, env):
    cmd = sub(cmd)
    clip = None
    if cmd.rstrip().endswith("| clip"):
        cmd = cmd.rstrip()[: -len("| clip")].rstrip()
        clip = True
    p = subprocess.run([BASH, "-lc", cmd], cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=600)
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out, (p.stdout.strip().splitlines()[-1] if clip and p.stdout.strip() else None)


def wait_port(port, seconds=20):
    for _ in range(seconds * 10):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("plan")
    ap.add_argument("work")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--stop-after", type=int, default=0, help="stop after N browse steps")
    ap.add_argument("--run-tests", action="store_true",
                    help="also run the Docker test suites (bash scripts/test.sh ...)")
    ap.add_argument("--probe", help="also try every page in this routes file at the end of "
                                    "each milestone and write the first passing milestone "
                                    "of each to PROBE.json")
    a = ap.parse_args()
    probe_urls, first_ok = [], {}
    if a.probe:
        import json
        for path in re.findall(r"^\s*'(/[^'{]*)'\s*=>", Path(a.probe).read_text(encoding="utf-8"), re.M):
            if not re.search(r"/(new|edit|delete|remove|download|logout|switch-project|password|login)", path):
                probe_urls.append(path)

    # resolve() expands Windows 8.3 short names (e.g. SIRWIN~1), which Docker cannot mount
    src, work = Path(a.source).resolve(), Path(a.work).resolve()
    work.parent.mkdir(parents=True, exist_ok=True)
    work = work.parent.resolve() / work.name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    steps, _ = cr.build_plan(src, a.exclude, 300, a.plan, cr.MAX_LINE_LEN, only_plan=False)

    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    subprocess.run(["docker", "rm", "-f", "fpc-check-db"], capture_output=True)
    server = None
    browser = Browser("http://127.0.0.1:8090")
    clipboard = None
    milestone = ""
    problems = 0
    visits = 0
    run_results = {}
    key = "base64:" + base64.b64encode(os.urandom(32)).decode()
    try:
        def probe():
            if not (probe_urls and server and milestone):
                return
            for u in probe_urls:
                if u in first_ok:
                    continue
                status, final, body = browser.get("http://127.0.0.1:8090" + u)
                if status == 200 and not ERROR_RE.search(body) and "/login" not in final:
                    first_ok[u] = milestone

        for kind, k, val in steps + [("milestone", None, "")]:
            if kind == "milestone":
                probe()
                milestone = val
                continue
            if kind in ("type", "preload"):
                dst = work / val
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / val, dst)
            elif kind in ("newfile", "preloadfile"):
                path, content = val
                (work / path).write_text(sub(content.replace("{{APP_KEY}}", key)), encoding="utf-8")
            elif kind == "run":
                wait, cmd = cr.parse_run(val)
                if "test.sh" in cmd and not a.run_tests:
                    continue
                t0 = time.time()
                code, out, clip = run_cmd(cmd, work, env)
                rk = f"{milestone}|{cmd}"              # stable across plan regeneration
                ok = code == 0
                if "test.sh" in cmd or "tests/run.php" in cmd:
                    # a crash during start-up can still exit 0: only a summary proves a pass
                    ok = ok and re.search(r"\b[1-9]\d* passed, 0 failed", out) is not None
                code = 0 if ok else (code or 1)
                run_results[rk] = {"cmd": cmd, "ok": ok, "secs": round(time.time() - t0),
                                   "milestone": milestone,
                                   "tail": out.strip().splitlines()[-6:]}
                if "test.sh" in cmd or "tests/run.php" in cmd:
                    summary = next((l for l in reversed(out.splitlines()) if "passed" in l), "")
                    print(f"  [{'ok ' if code == 0 else 'ERR'}] {milestone[:38]:38} $ {cmd}  "
                          f"{summary.strip()}  ({run_results[rk]['secs']} s)")
                if clip:
                    clipboard = clip
                if "docker run" in cmd:
                    time.sleep(5)
                    for _ in range(60):          # wait for MySQL to accept connections
                        r = subprocess.run(["docker", "exec", "fpc-check-db", "mysql", "-uroot",
                                            "-pdevpass", "-e", "SELECT 1"], capture_output=True)
                        if r.returncode == 0:
                            break
                        time.sleep(1)
                flag = "ok " if code == 0 else "ERR"
                if code != 0 and "php -l" not in cmd and "tests/run.php" not in cmd:
                    problems += 1
                if code != 0 or "migrate" in cmd or "create_admin" in cmd:
                    tail = " / ".join(out.strip().splitlines()[-2:])[:160]
                    print(f"  [{flag}] $ {cmd[:70]}   {tail}")
            elif kind == "serve":
                server = subprocess.Popen([BASH, "-lc", sub(val)], cwd=work, env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print("  [ok ] server " + ("up" if wait_port(8090) else "NOT RESPONDING"))
            elif kind == "browse":
                visits += 1
                parts = [p.strip() for p in val.split("|")]
                target = parts[0]
                url = clipboard if target == "clipboard" else \
                    sub(target if "://" in target else "http://127.0.0.1:8080/" + target.lstrip("/"))
                if not url:
                    print(f"  [ERR] browse {val}: nothing on the clipboard")
                    problems += 1
                    continue
                status, final, body = browser.get(url)
                fields, idx, form = {}, 0, None
                for act in parts[1:]:
                    word, _, arg = act.partition(" ")
                    if form is None:
                        fp = Forms()
                        fp.feed(body)
                        form = next((f for f in fp.forms if f["fields"]), None)
                    if form is None:
                        break
                    if word == "type":
                        fields[form["fields"][idx]] = arg
                    elif word == "tab":
                        idx = min(idx + 1, len(form["fields"]) - 1)
                    elif word == "enter":
                        data = dict(form["hidden"])
                        data.update(fields)
                        action = urllib.parse.urljoin(final, form["action"] or final)
                        status, final, body = browser.get(action, data)
                        form, fields, idx = None, {}, 0
                fp = Forms()
                fp.feed(body)
                err = ERROR_RE.search(body)
                bad = status != 200 or err is not None
                problems += bad
                where = urllib.parse.urlparse(final).path
                detail = ""
                if err:
                    i = err.start()
                    detail = re.sub(r"<[^>]+>|\s+", " ", body[max(0, i - 40): i + 220])
                elif status != 200:
                    detail = re.sub(r"<[^>]+>|\s+", " ", body)[:200]
                print(f"  [{'ERR' if bad else 'ok '}] {milestone[:38]:38} browse {val[:45]:45} "
                      f"-> {status} {where} '{fp.title.strip()[:40]}' {detail[:200]}")
                if a.stop_after and visits >= a.stop_after:
                    break
    finally:
        if server:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(server.pid)], capture_output=True)
        subprocess.run(["docker", "rm", "-f", "fpc-check-db"], capture_output=True)
    print(f"\n{visits} preview visits checked, {problems} problem(s).")
    import json
    runs_out = Path(a.plan).with_suffix(".runs.json")
    runs_out.write_text(json.dumps(run_results, indent=1), encoding="utf-8")
    tests = [r for r in run_results.values() if "test" in r["cmd"]]
    print(f"Test runs: {sum(r['ok'] for r in tests)} passed, "
          f"{sum(not r['ok'] for r in tests)} failed. Wrote {runs_out}")
    if a.probe:
        import json
        out = Path(a.plan).with_suffix(".probe.json")
        out.write_text(json.dumps(first_ok, indent=1), encoding="utf-8")
        never = [u for u in probe_urls if u not in first_ok]
        print(f"Probe: {len(first_ok)} pages work at some point; never working: {never}")
        print(f"Wrote {out}")


if __name__ == "__main__":
    main()
