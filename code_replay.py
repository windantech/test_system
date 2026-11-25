#!/usr/bin/env python3
"""
Code Replay - re-types an existing codebase into VS Code the way a human
developer would, for recording "rebuilding the system from scratch" videos.

It drives the real VS Code and Chrome windows with real keyboard and mouse
input: opens each file with Ctrl+P, types it character by character (with
typos that get fixed, thinking pauses, saves), scrolls back to review code,
clicks around, and now and then switches to Chrome to look something up.

Hotkeys while running:
    F9   pause / resume
    F10  stop (progress is saved; run again with --resume)
    Move the mouse into the top-left corner of the screen = emergency stop.

Windows only. Requires:  pip install pynput
"""

import argparse
import ctypes
import fnmatch
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# --------------------------------------------------------------------------
# File selection
# --------------------------------------------------------------------------

IGNORE_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", ".idea", ".vscode", "target", "bin", "obj",
    ".gradle", "coverage", ".pytest_cache", ".mypy_cache", ".tox", ".cache",
}

# Generated files nobody types by hand: copied at the end instead.
GENERATED_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Pipfile.lock", "composer.lock", "Cargo.lock", "go.sum", ".DS_Store",
}

MANIFEST_NAMES = {
    ".gitignore", ".editorconfig", ".env.example", "package.json", "requirements.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "pom.xml", "build.gradle",
    "settings.gradle", "go.mod", "cargo.toml", "composer.json", "gemfile",
    "tsconfig.json", "dockerfile", "docker-compose.yml", "makefile",
}

MAX_LINE_LEN = 3000         # longer lines => minified / data file => copied (see --max-line)


def classify(path: Path, max_kb: int, max_line: int = MAX_LINE_LEN):
    """Return (kind, reason) where kind is 'type' or 'copy'."""
    name = path.name
    if name in GENERATED_NAMES:
        return "copy", "generated file"
    if name.endswith((".min.js", ".min.css", ".map")):
        return "copy", "minified"
    try:
        size = path.stat().st_size
    except OSError:
        return "copy", "unreadable"
    if size > max_kb * 1024:
        return "copy", f"larger than {max_kb} KB"
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return "copy", "binary"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "copy", "not UTF-8 text"
    if any(len(l) > max_line for l in text.splitlines()):
        return "copy", "very long lines"
    if not text.strip():
        return "copy", "empty"
    return "type", ""


def priority(rel: Path):
    name = rel.name.lower()
    parts = [p.lower() for p in rel.parts]
    if name in MANIFEST_NAMES and len(parts) == 1:
        group = 0
    elif name.startswith("readme"):
        group = 3
    elif any(p in ("test", "tests", "__tests__", "spec") for p in parts[:-1]) or \
            name.startswith("test_") or ".test." in name or ".spec." in name:
        group = 2
    else:
        group = 1
    return (group, [p for p in parts[:-1]], name)


def build_plan(src: Path, excludes, max_kb, order_file, max_line=MAX_LINE_LEN,
               only_plan=False):
    typed, copied = [], []
    for root, dirs, files in os.walk(src):
        rroot = Path(root).relative_to(src)
        dirs[:] = sorted(
            d for d in dirs
            if d not in IGNORE_DIRS and not is_excluded(rroot / d, excludes)
        )
        for f in sorted(files):
            rel = rroot / f
            if is_excluded(rel, excludes):
                continue
            kind, reason = classify(src / rel, max_kb, max_line)
            (typed if kind == "type" else copied).append((rel, reason))

    typed_paths = [r for r, _ in typed]
    typed_paths.sort(key=priority)
    if not order_file:
        return [("type", p.as_posix(), p) for p in typed_paths], copied

    # Plan file:  '## title' = milestone,  'run [seconds] <command>' = terminal
    # command,  any other non-comment line = a file to type.
    index = {p.as_posix(): p for p in typed_paths}
    used, steps = set(), []
    for n, line in enumerate(Path(order_file).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if line.startswith("## "):
            steps.append(("milestone", None, line[3:].strip()))
        elif not line or line.startswith("#"):
            continue
        elif line.startswith("run "):
            steps.append(("run", f"run#{n}", line[4:].strip()))
        else:
            k = Path(line).as_posix()
            if k in index and k not in used:
                steps.append(("type", k, index[k]))
                used.add(k)
    rest = [p for p in typed_paths if p.as_posix() not in used]
    if rest and not only_plan:
        steps.append(("milestone", None, "Remaining files"))
        steps += [("type", p.as_posix(), p) for p in rest]
    return steps, copied


GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
_docker_ok = None


def command_unavailable(cmd):
    """Why a terminal step cannot run here, or None if it can."""
    global _docker_ok
    prog = cmd.split()[0]
    if prog == "bash":
        if not GIT_BASH.exists():
            return "Git Bash is not installed"
    elif not shutil.which(prog):
        return f"{prog} is not installed"
    if "test.sh" in cmd and "unit" not in cmd.split():
        if _docker_ok is None:
            try:
                _docker_ok = subprocess.run(["docker", "info"], capture_output=True,
                                            timeout=30).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                _docker_ok = False
        if not _docker_ok:
            return "Docker is not running"
    return None


def parse_run(cmd):
    """'run 120 bash x.sh' -> (120, 'bash x.sh'); otherwise a sensible default wait."""
    parts = cmd.split(None, 1)
    if len(parts) == 2 and parts[0].isdigit():
        return int(parts[0]), parts[1]
    if "php -l" in cmd:
        return 4, cmd
    if "tests/run.php" in cmd:
        return 30, cmd
    if "test.sh" in cmd:
        return 240, cmd
    return 10, cmd


def is_excluded(rel: Path, patterns):
    s = rel.as_posix()
    return any(fnmatch.fnmatch(s, p) or fnmatch.fnmatch(rel.name, p) for p in patterns)


def read_source(path: Path) -> str:
    text = path.read_bytes().decode("utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------
# Human timing model
# --------------------------------------------------------------------------

NEIGHBORS = {
    "a": "qwsz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr", "f": "drtgvc",
    "g": "ftyhbv", "h": "gyujnb", "i": "ujko", "j": "huikmn", "k": "jiolm", "l": "kop",
    "m": "njk", "n": "bhjm", "o": "iklp", "p": "ol", "q": "wa", "r": "edft",
    "s": "awedxz", "t": "rfgy", "u": "yhji", "v": "cfgb", "w": "qase", "x": "zsdc",
    "y": "tghu", "z": "asx",
}
SYMBOLS = set("{}[]()<>;:\"'`~!@#$%^&*-_=+|\\/?,.")
BLOCK_RE = re.compile(
    r"^(def|class|async|function|func|fn|public|private|protected|internal|static|"
    r"interface|struct|enum|impl|module|namespace|export|@\w+|#include|package|"
    r"import|from|using|CREATE|SELECT|router\.|app\.)\b"
)
COMMENT_RE = re.compile(r"^(#|//|/\*|\*|--|<!--|\"\"\"|''')")


class Timing:
    def __init__(self, wpm, think, typo_rate, rng, scale=1.0):
        self.base = 60.0 / (wpm * 5.0)     # seconds per character
        self.think = think
        self.typo_rate = typo_rate
        self.rng = rng
        self.scale = scale

    def char(self, c, prev):
        r = self.rng
        d = self.base * r.lognormvariate(0, 0.4)
        if c in SYMBOLS:
            d *= 1.6
        elif c.isdigit():
            d *= 1.35
        elif c.isupper():
            d *= 1.2
        elif c == " ":
            d *= 0.9
        if prev == " " and r.random() < 0.08:       # hesitate between words
            d += r.uniform(0.2, 0.9) * self.think
        return d * self.scale

    def indent(self):
        return self.rng.uniform(0.02, 0.06) * self.scale

    def newline(self):
        return self.base * self.rng.uniform(0.8, 1.6) * self.scale

    def notice_typo(self):
        return self.rng.uniform(0.15, 0.6) * self.scale

    def backspace(self):
        return self.rng.uniform(0.05, 0.14) * self.scale

    def line_pause(self, line, prev_line):
        """Thinking time before starting to type `line`."""
        r = self.rng
        s = line.strip()
        if not s:
            return r.uniform(0.2, 0.8) * self.think * self.scale
        p = r.lognormvariate(math.log(0.45), 0.6)
        if BLOCK_RE.match(s):
            p += r.uniform(1.5, 6.0)
        if COMMENT_RE.match(s):
            p += r.uniform(0.5, 2.0)
        if prev_line is not None and not prev_line.strip():
            p += r.uniform(0.5, 2.5)
        if r.random() < 0.02:                      # occasional deep thought
            p += r.uniform(6.0, 25.0)
        return p * self.think * self.scale


def file_events(text, t: Timing, start_line=0):
    """Yield (kind, arg, delay) for typing `text`, starting at line `start_line`
    (everything before it is assumed to be in the file already).

    kinds: 'line' (thinking pause / behaviour hook, arg = line index),
           'char' (type arg), 'bs' (backspace), 'fixindent' (after Enter).
    """
    lines = text.split("\n")
    prev = lines[start_line - 1] if start_line > 0 else None
    for idx, line in enumerate(lines):
        if idx < start_line:
            continue
        last = idx == len(lines) - 1
        if last and line == "":
            break
        yield ("line", idx, t.line_pause(line, prev))
        content = line.lstrip(" \t")
        for ch in line[: len(line) - len(content)]:
            yield ("char", ch, t.indent())
        yield from _content_events(content, t)
        if not last:
            yield ("char", "\n", t.newline())
            yield ("fixindent", None, 0.0)
        prev = line


def _content_events(s, t: Timing):
    prev = ""
    for i, c in enumerate(s):
        low = c.lower()
        if c.isalpha() and low in NEIGHBORS and t.rng.random() < t.typo_rate:
            wrong = t.rng.choice(NEIGHBORS[low])
            if c.isupper():
                wrong = wrong.upper()
            extra = t.rng.choices([0, 1, 2], weights=[50, 35, 15])[0]
            seq = [wrong] + list(s[i + 1: i + 1 + extra])
            p = prev
            for ch in seq:
                yield ("char", ch, t.char(ch, p))
                p = ch
            yield ("pause", None, t.notice_typo())
            for _ in seq:
                yield ("bs", None, t.backspace())
        yield ("char", c, t.char(c, prev))
        prev = c


def estimate_seconds(src, files, wpm, think, typo):
    t = Timing(wpm, think, typo, random.Random(1))
    total = 0.0
    for rel in files:
        total += 2.5      # opening the file
        for _, _, d in file_events(read_source(src / rel), t):
            total += d
    return total


# --------------------------------------------------------------------------
# Windows helpers
# --------------------------------------------------------------------------

if os.name == "nt":
    import ctypes.wintypes as wt

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.BringWindowToTop.argtypes = [wt.HWND]
    user32.IsIconic.argtypes = [wt.HWND]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def hwnd_eq(a, b):
    return bool(a) and bool(b) and int(a) == int(b)


def window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def find_windows(pred):
    found = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            title = window_title(hwnd)
            if title and pred(title):
                found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def window_rect(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def focus_window(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        time.sleep(0.3)
    fg = user32.GetForegroundWindow()
    if hwnd_eq(fg, hwnd):
        return True
    cur = kernel32.GetCurrentThreadId()
    fgt = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tgt = user32.GetWindowThreadProcessId(hwnd, None)
    for th in (fgt, tgt):
        if th:
            user32.AttachThreadInput(th, cur, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    for th in (fgt, tgt):
        if th:
            user32.AttachThreadInput(th, cur, False)
    time.sleep(0.25)
    return hwnd_eq(user32.GetForegroundWindow(), hwnd)


# --------------------------------------------------------------------------
# VS Code workspace settings: stop the editor "helping" while we type
# --------------------------------------------------------------------------

VSCODE_SETTINGS = {
    "editor.autoClosingBrackets": "never",
    "editor.autoClosingQuotes": "never",
    "editor.autoClosingComments": "never",
    "editor.autoClosingDelete": "never",
    "editor.autoClosingOvertype": "never",
    "editor.autoSurround": "never",
    "editor.autoIndent": "none",
    "editor.formatOnType": False,
    "editor.formatOnSave": False,
    "editor.formatOnPaste": False,
    "editor.quickSuggestions": {"other": False, "comments": False, "strings": False},
    "editor.suggestOnTriggerCharacters": False,
    "editor.acceptSuggestionOnEnter": "off",
    "editor.acceptSuggestionOnCommitCharacter": False,
    "editor.wordBasedSuggestions": "off",
    "editor.snippetSuggestions": "none",
    "editor.tabCompletion": "off",
    "editor.parameterHints.enabled": False,
    "editor.inlineSuggest.enabled": False,
    "editor.linkedEditing": False,
    "editor.detectIndentation": False,
    "editor.insertSpaces": False,
    "editor.useTabStops": False,
    "editor.codeActionsOnSave": {},
    "editor.stickyScroll.enabled": False,
    "github.copilot.enable": {"*": False},
    "github.copilot.editor.enableAutoCompletions": False,
    "html.autoClosingTags": False,
    "html.autoCreateQuotes": False,
    "javascript.autoClosingTags": False,
    "typescript.autoClosingTags": False,
    "emmet.showExpandedAbbreviation": "never",
    "emmet.triggerExpansionOnTab": False,
    "python.analysis.autoIndent": False,
    "files.autoSave": "off",
    "files.insertFinalNewline": False,
    "files.trimTrailingWhitespace": False,
    "files.trimFinalNewlines": False,
    "files.exclude": {".vscode": True},
    "terminal.integrated.defaultProfile.windows": "Git Bash",
    # Git Bash would rewrite paths passed to docker (-v /c/...:/app) and break them.
    "terminal.integrated.env.windows": {"MSYS_NO_PATHCONV": "1"},
    "window.title": "${dirty}${activeEditorShort}${separator}${rootName}${separator}${appName}",
}


# --------------------------------------------------------------------------
# The replayer
# --------------------------------------------------------------------------

class Stopped(Exception):
    pass


class Replayer:
    def __init__(self, args):
        from pynput import keyboard, mouse
        from pynput.keyboard import Key
        self.Key = Key
        self.kb = keyboard.Controller()
        self.ms = mouse.Controller()
        self.MouseButton = mouse.Button

        self.a = args
        self.src = Path(args.source).resolve()
        self.out = Path(args.out).resolve()
        self.rng = random.Random(args.seed)
        self.t = Timing(args.wpm, args.think, args.typos, self.rng)
        self.state_file = self.out / ".vscode" / "code-replay-state.json"
        self.log_file = self.out / ".vscode" / "code-replay-log.txt"

        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.vscode = None
        self.browser = None
        self.expected = None          # window that must be in front while typing
        self.mode = "editor"          # 'editor' | 'quickopen' | 'browser'
        self.next_lookup = time.time() + self._lookup_interval()
        self.done_queries = set()
        self.lines_to_save = self.rng.randint(12, 40)
        self.chars_done = 0
        self.chars_total = 0
        self.started = time.time()
        self.cur_label = ""
        self.done = set()
        self.cur_step_key = None      # file being typed (for mid-file resume)
        self.cur_line = 0

        self.listener = keyboard.Listener(on_press=self._on_hotkey)
        self.listener.daemon = True
        self.listener.start()

    # ---- control -------------------------------------------------------

    def _on_hotkey(self, key):
        if key == self.Key.f9:
            if self.paused.is_set():
                self.paused.clear()
                print("\n[resumed]")
            else:
                self.paused.set()
                print("\n[paused] press F9 to resume, F10 to stop")
        elif key == self.Key.f10:
            self.stopped.set()
            self.paused.clear()

    def log(self, msg):
        print("\n" + msg)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except OSError:
            pass

    def check(self):
        if self.stopped.is_set():
            raise Stopped()
        x, y = self.ms.position
        if x <= 2 and y <= 2:
            self.stopped.set()
            raise Stopped()
        if self.paused.is_set():
            while self.paused.is_set():
                time.sleep(0.1)
                if self.stopped.is_set():
                    raise Stopped()
            self._after_pause()

    def _after_pause(self):
        """Put focus back where we were before the pause."""
        time.sleep(0.5)
        if self.expected is not None:
            focus_window(self.expected)
            time.sleep(0.3)
        if self.mode == "editor":
            self._raw_hotkey(self.Key.ctrl, "1")
            self._raw_hotkey(self.Key.ctrl, self.Key.end)

    def sleep(self, seconds):
        end = time.time() + max(0.0, seconds)
        while True:
            self.check()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.05, left))

    def guard(self):
        """Never send keys to the wrong window."""
        self.check()
        if self.expected is None:
            return
        if hwnd_eq(user32.GetForegroundWindow(), self.expected):
            return
        if focus_window(self.expected) and self.mode == "editor":
            self._raw_hotkey(self.Key.ctrl, "1")
            self._raw_hotkey(self.Key.ctrl, self.Key.end)
            return
        if hwnd_eq(user32.GetForegroundWindow(), self.expected):
            return
        self.log("Target window lost focus - paused. Bring it back and press F9.")
        self.paused.set()
        self.check()

    # ---- keyboard ------------------------------------------------------

    def _raw_hotkey(self, *keys):
        for k in keys:
            self.kb.press(k)
            time.sleep(0.03)
        for k in reversed(keys):
            self.kb.release(k)
            time.sleep(0.02)

    def hotkey(self, *keys):
        self.guard()
        self._raw_hotkey(*keys)
        time.sleep(self.rng.uniform(0.08, 0.2))

    def tap(self, key):
        self.guard()
        self.kb.press(key)
        time.sleep(self.rng.uniform(0.02, 0.06))
        self.kb.release(key)

    def char(self, c):
        if c == "\n":
            self.tap(self.Key.enter)
        elif c == "\t":
            self.tap(self.Key.tab)
        else:
            self.guard()
            self.kb.type(c)

    def type_plain(self, text, speed=1.0):
        prev = ""
        for c in text:
            self.char(c)
            self.sleep(self.t.char(c, prev) / speed)
            prev = c

    # ---- mouse ---------------------------------------------------------

    def move_mouse(self, x, y):
        sx, sy = self.ms.position
        x, y = max(10, int(x)), max(10, int(y))
        dist = math.hypot(x - sx, y - sy)
        if dist < 3:
            return
        dur = min(1.3, max(0.2, 0.2 + dist / 1600.0 * self.rng.uniform(0.7, 1.3)))
        bend = dist * 0.2
        c1 = (sx + (x - sx) * self.rng.uniform(0.2, 0.4) + self.rng.uniform(-bend, bend),
              sy + (y - sy) * self.rng.uniform(0.2, 0.4) + self.rng.uniform(-bend, bend))
        c2 = (sx + (x - sx) * self.rng.uniform(0.6, 0.8) + self.rng.uniform(-bend, bend),
              sy + (y - sy) * self.rng.uniform(0.6, 0.8) + self.rng.uniform(-bend, bend))
        steps = max(12, int(dur * 90))
        for i in range(1, steps + 1):
            self.check()
            u = i / steps
            u = u * u * (3 - 2 * u)            # ease in/out
            px = (1-u)**3*sx + 3*(1-u)**2*u*c1[0] + 3*(1-u)*u**2*c2[0] + u**3*x
            py = (1-u)**3*sy + 3*(1-u)**2*u*c1[1] + 3*(1-u)*u**2*c2[1] + u**3*y
            self.ms.position = (max(10, int(px)), max(10, int(py)))
            time.sleep(dur / steps)
        self.ms.position = (x, y)

    def click(self, double=False):
        self.check()
        self.ms.click(self.MouseButton.left, 2 if double else 1)
        time.sleep(self.rng.uniform(0.1, 0.25))

    def scroll(self, notches):
        step = 1 if notches > 0 else -1
        for _ in range(abs(notches)):
            self.check()
            self.ms.scroll(0, step)
            time.sleep(self.rng.uniform(0.06, 0.25))

    def region_point(self, hwnd, x0, x1, y0, y1):
        l, t, r, b = window_rect(hwnd)
        w, h = r - l, b - t
        return (l + w * self.rng.uniform(x0, x1), t + h * self.rng.uniform(y0, y1))

    def editor_point(self):
        return self.region_point(self.vscode, 0.32, 0.78, 0.2, 0.85)

    # ---- setup ---------------------------------------------------------

    def prepare_output(self):
        self.out.mkdir(parents=True, exist_ok=True)
        existing = [p for p in self.out.iterdir() if p.name != ".vscode"]
        if existing and not self.a.resume:
            sys.exit(f"Output folder {self.out} is not empty. Use an empty folder, or --resume.")
        vs = self.out / ".vscode"
        vs.mkdir(exist_ok=True)
        settings_path = vs / "settings.json"
        settings = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except ValueError:
                settings = {}
        settings.update(VSCODE_SETTINGS)
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    def load_state(self):
        if self.a.resume and self.state_file.exists():
            st = json.loads(self.state_file.read_text(encoding="utf-8"))
            return set(st.get("done", [])), st.get("partial")
        return set(), None

    def save_state(self):
        st: dict = {"done": sorted(self.done)}
        if self.cur_step_key:
            st["partial"] = {"key": self.cur_step_key, "line": self.cur_line}
        self.state_file.write_text(json.dumps(st, indent=1), encoding="utf-8")

    def find_vscode(self):
        root = self.out.name.lower()
        wins = find_windows(lambda t: "visual studio code" in t.lower() and root in t.lower())
        return wins[0] if wins else None

    def launch_vscode(self):
        subprocess.Popen(f'code "{self.out}"', shell=True)
        print("Waiting for VS Code to open the output folder...")
        for _ in range(60):
            hwnd = self.find_vscode()
            if hwnd:
                return hwnd
            time.sleep(0.5)
        sys.exit("Could not find the VS Code window. Is the `code` command on PATH?")

    def find_browser(self):
        wins = find_windows(lambda t: t.endswith(self.a.browser_title))
        return wins[0] if wins else None

    # ---- behaviours ----------------------------------------------------

    def focus_editor(self):
        self.mode = "editor"
        self.expected = self.vscode
        if not focus_window(self.vscode):
            x, y = self.region_point(self.vscode, 0.45, 0.55, 0.005, 0.02)
            self.move_mouse(x, y)
            self.click()
        self.sleep(0.3)
        self.hotkey(self.Key.ctrl, "1")
        self.hotkey(self.Key.ctrl, self.Key.end)

    def open_file(self, rel: Path, prefix: str = ""):
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        # start empty, or (when resuming mid-file) with the lines already typed
        path.write_bytes(prefix.encode("utf-8"))
        self.sleep(self.rng.uniform(0.8, 1.6))
        self.expected = self.vscode
        self.mode = "quickopen"
        self.hotkey(self.Key.ctrl, "p")
        self.sleep(self.rng.uniform(0.4, 0.9))
        self.type_plain(rel.as_posix(), speed=1.6)
        self.sleep(self.rng.uniform(0.9, 1.6))
        self.tap(self.Key.enter)
        self.sleep(1.0)
        self.mode = "editor"
        if not window_title(self.vscode).lstrip("● ").startswith(rel.name):
            self.tap(self.Key.esc)
            subprocess.Popen(f'code -r -g "{path}"', shell=True)
            self.sleep(2.0)
            focus_window(self.vscode)
        if not window_title(self.vscode).lstrip("● ").startswith(rel.name):
            self.log(f"Could not open {rel} in VS Code - paused. Open it, click into it, press F9.")
            self.paused.set()
            self.check()

    def save(self):
        self.hotkey(self.Key.ctrl, "s")
        self.lines_to_save = self.rng.randint(12, 40)
        self.save_state()

    def time_is_up(self):
        return bool(self.a.max_hours) and \
            time.time() - self.started >= self.a.max_hours * 3600.0

    def run_command(self, cmd):
        """Type a command into the VS Code terminal, let it run, read the output."""
        wait, cmd = parse_run(cmd)
        missing = command_unavailable(cmd)
        if missing:
            self.log(f"Skipped terminal step ({missing}): {cmd}")
            return
        self.cur_label = f"[terminal] {cmd}"
        self.progress(0, 0)
        self.focus_editor()
        self.sleep(self.rng.uniform(0.8, 2.0) * self.a.think)
        self.hotkey(self.Key.ctrl, "`")              # open / focus the terminal
        self.mode = "terminal"
        self.sleep(self.rng.uniform(1.5, 3.0))
        self.type_plain(cmd, speed=1.3)
        self.sleep(self.rng.uniform(0.3, 0.9))
        self.tap(self.Key.enter)
        self.sleep(wait)
        if not self.a.no_mouse:                       # read through the output
            l, t, r, b = window_rect(self.vscode)
            self.move_mouse(l + (r - l) * self.rng.uniform(0.35, 0.7),
                            t + (b - t) * self.rng.uniform(0.75, 0.9))
        self.sleep(self.rng.uniform(3.0, 9.0) * self.a.think)
        self.focus_editor()

    def review_scroll(self):
        self.move_mouse(*self.editor_point())
        n = self.rng.randint(3, 12)
        self.scroll(n)
        self.sleep(self.rng.uniform(2.0, 7.0) * self.a.think)
        if self.rng.random() < 0.4:
            self.move_mouse(*self.editor_point())
            self.sleep(self.rng.uniform(1.0, 3.0))
        self.scroll(-n)
        self.sleep(self.rng.uniform(0.3, 0.8))
        self.hotkey(self.Key.ctrl, self.Key.end)

    def click_around(self):
        self.move_mouse(*self.editor_point())
        self.sleep(self.rng.uniform(0.2, 0.6))
        self.click(double=self.rng.random() < 0.35)
        self.sleep(self.rng.uniform(0.8, 2.5) * self.a.think)
        self.hotkey(self.Key.ctrl, self.Key.end)

    def idle_mouse(self):
        self.move_mouse(*self.editor_point())

    def _lookup_interval(self):
        return self.rng.uniform(self.a.lookup_min, self.a.lookup_max) * 60.0

    def make_query(self, rel: Path, typed_text: str):
        lang = LANG_BY_EXT.get(rel.suffix.lower(), rel.suffix.lstrip(".") or "code")
        mods = []
        for rx in IMPORT_RES:
            mods += [m for m in rx.findall(typed_text) if m and not m.startswith(".")]
        mods = [m.split("/")[0] if not m.startswith("@") else "/".join(m.split("/")[:2])
                for m in mods]
        mods = [m for m in dict.fromkeys(mods) if f"{lang} {m}" not in self.done_queries]
        if mods:
            m = self.rng.choice(mods)
            key = f"{lang} {m}"
            suffix = self.rng.choice(["documentation", "example", "tutorial", "usage"])
            self.done_queries.add(key)
            return f"{lang} {m} {suffix}"
        names = re.findall(r"\b(?:def|function|class|func|fn)\s+([A-Za-z_]\w{3,})", typed_text)
        if names:
            return f"{lang} {self.rng.choice(names)} best practice"
        return None

    def browser_lookup(self, query):
        hwnd = self.browser or self.find_browser()
        if not hwnd:
            return
        self.browser = hwnd
        self.mode = "browser"
        self.expected = hwnd
        if not focus_window(hwnd):
            self.focus_editor()
            return
        self.sleep(0.6)
        self.move_mouse(*self.region_point(hwnd, 0.3, 0.7, 0.3, 0.7))
        self.hotkey(self.Key.ctrl, "t")
        self.sleep(self.rng.uniform(0.6, 1.2))
        self.type_plain(query, speed=1.3)
        self.sleep(self.rng.uniform(0.3, 0.8))
        self.tap(self.Key.enter)
        self.sleep(self.rng.uniform(2.5, 5.0))
        for _ in range(self.rng.randint(2, 5)):
            self.move_mouse(*self.region_point(hwnd, 0.25, 0.75, 0.3, 0.8))
            self.scroll(-self.rng.randint(2, 6))
            self.sleep(self.rng.uniform(2.0, 7.0) * self.a.think)
        self.sleep(self.rng.uniform(2.0, 8.0) * self.a.think)
        self.hotkey(self.Key.ctrl, "w")
        self.sleep(self.rng.uniform(0.4, 0.9))
        self.move_mouse(*self.editor_point())
        self.focus_editor()

    def between_lines(self, rel, lines, idx):
        if idx == 0:
            return
        self.lines_to_save -= 1
        if self.lines_to_save <= 0:
            self.save()
        if not self.a.no_mouse:
            r = self.rng.random()
            if r < 0.012 and idx > 15:
                self.review_scroll()
            elif r < 0.022 and idx > 5:
                self.click_around()
            elif r < 0.05:
                self.idle_mouse()
        if not self.a.no_browser and time.time() >= self.next_lookup:
            q = self.make_query(rel, "\n".join(lines[:idx]))
            if q:
                self.browser_lookup(q)
            self.next_lookup = time.time() + self._lookup_interval()

    # ---- main loop -----------------------------------------------------

    def type_file(self, rel: Path, text: str, file_no, file_count, start_line=0):
        lines = text.split("\n")
        self.cur_label = f"[{file_no}/{file_count}] {rel.as_posix()}"
        self.cur_step_key = rel.as_posix()
        self.cur_line = start_line
        prefix = "".join(l + "\n" for l in lines[:start_line])
        self.open_file(rel, prefix)
        self.focus_editor()
        for kind, arg, delay in file_events(text, self.t, start_line):
            if kind == "line":
                self.cur_line = arg
                if self.time_is_up():
                    self.log(f"Session time limit of {self.a.max_hours:g} h reached.")
                    self.stopped.set()
                    self.check()
                self.between_lines(rel, lines, arg)
                self.progress(arg + 1, len(lines))
                self.sleep(delay)
            elif kind == "char":
                self.char(arg)
                if arg != "\n":
                    self.chars_done += 1
                self.sleep(delay)
            elif kind == "bs":
                self.tap(self.Key.backspace)
                self.sleep(delay)
            elif kind == "fixindent":
                # If the editor auto-indented the new line anyway, select that
                # whitespace so the next typed character replaces it.
                self.hotkey(self.Key.shift, self.Key.home)
            elif kind == "pause":
                self.sleep(delay)
        self.save()
        self.sleep(self.rng.uniform(1.0, 3.0) * self.a.think)
        self.verify(rel)

    def verify(self, rel):
        self.sleep(0.8)
        src_bytes = (self.src / rel).read_bytes()
        dst = self.out / rel
        try:
            typed = dst.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
        except (OSError, UnicodeDecodeError):
            typed = None
        expected = read_source(self.src / rel)
        if typed != expected:
            self.log(f"MISMATCH in {rel}: typed text differs from the source "
                     f"(focus lost or editor auto-insert?). Restoring the original file.")
        if dst.read_bytes() != src_bytes:
            dst.write_bytes(src_bytes)   # exact original bytes (line endings, BOM)

    def progress(self, line_no, line_count):
        elapsed = time.time() - self.started
        pct = 100.0 * self.chars_done / max(1, self.chars_total)
        eta = ""
        if self.chars_done > 200:
            rem = elapsed / self.chars_done * (self.chars_total - self.chars_done)
            eta = f" ETA {fmt_dur(rem)}"
        sys.stdout.write(f"\r{self.cur_label}  line {line_no}/{line_count}  "
                         f"total {pct:5.1f}%  elapsed {fmt_dur(elapsed)}{eta}    ")
        sys.stdout.flush()

    def run(self, steps, copied):
        self.prepare_output()
        self.done, partial = self.load_state()
        done = self.done
        files = [v for k, _, v in steps if k == "type"]
        todo = [f for f in files if f.as_posix() not in done]
        self.chars_total = sum(len(read_source(self.src / f).replace("\n", ""))
                               for f in todo)
        resume_line = {}
        if partial and partial.get("key") not in done:
            resume_line[partial["key"]] = int(partial.get("line", 0))
        if self.a.target_hours:
            est = estimate_seconds(self.src, todo, self.a.wpm, self.a.think, self.a.typos)
            self.t.scale = max(0.1, self.a.target_hours * 3600.0 / est)
            print(f"Pacing scaled x{self.t.scale:.2f} to fill about {self.a.target_hours} h.")

        self.vscode = self.find_vscode() or self.launch_vscode()
        self.browser = None if self.a.no_browser else self.find_browser()
        if not self.a.no_browser and not self.browser:
            print(f"Note: no window ending in '{self.a.browser_title}' found; "
                  "browser lookups are skipped until one is open.")

        limit = f", stopping after {self.a.max_hours:g} h" if self.a.max_hours else ""
        print(f"\n{len(todo)} files to type ({len(files) - len(todo)} already done{limit}). "
              f"Starting in {self.a.start_delay} s - start your screen recorder now.")
        print("F9 = pause/resume   F10 = stop   mouse to top-left corner = emergency stop")
        for i in range(self.a.start_delay, 0, -1):
            sys.stdout.write(f"\r  {i:3d} ")
            sys.stdout.flush()
            self.sleep(1)
        print()

        self.started = time.time()
        file_no = len(files) - len(todo)
        try:
            for kind, key, val in steps:
                if kind == "milestone":
                    self.log(f"== {val}")
                    continue
                if key in done:
                    continue
                if self.time_is_up():
                    self.log(f"Session time limit of {self.a.max_hours:g} h reached.")
                    raise Stopped()
                if kind == "run":
                    self.run_command(val)
                else:
                    file_no += 1
                    self.type_file(val, read_source(self.src / val), file_no, len(files),
                                   resume_line.pop(key, 0))
                    self.cur_step_key = None
                    if not self.a.no_mouse and self.rng.random() < 0.5:
                        self.idle_mouse()
                    self.sleep(self.rng.uniform(3.0, 10.0) * self.a.think * self.t.scale)
                done.add(key)
                self.save_state()
        except Stopped:
            self.save_state()
            try:
                if hwnd_eq(user32.GetForegroundWindow(), self.vscode):
                    self._raw_hotkey(self.Key.ctrl, "s")
            except Exception:
                pass
            n_done = sum(1 for f in files if f.as_posix() in done)
            where = f", stopped inside {self.cur_step_key} at line {self.cur_line + 1}" \
                if self.cur_step_key else ""
            print(f"\nStopped after {fmt_dur(time.time() - self.started)}. "
                  f"{n_done}/{len(files)} files done{where}. "
                  "Run the same command with --resume to continue from here.")
            return

        for rel, _ in copied:
            dst = self.out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not self.a.no_copy_assets:
                shutil.copy2(self.src / rel, dst)
        print(f"\nFinished: typed {len(files)} files in {fmt_dur(time.time() - self.started)}"
              + ("" if self.a.no_copy_assets else f", copied {len(copied)} other files") + ".")


LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "react", ".ts": "typescript",
    ".tsx": "react typescript", ".java": "java", ".kt": "kotlin", ".cs": "c#",
    ".go": "golang", ".rs": "rust", ".php": "php", ".rb": "ruby", ".c": "c",
    ".cpp": "c++", ".h": "c", ".hpp": "c++", ".swift": "swift", ".dart": "dart",
    ".vue": "vue", ".html": "html", ".css": "css", ".scss": "scss", ".sql": "sql",
}
IMPORT_RES = [
    re.compile(r"^\s*from\s+([\w\.]+)\s+import", re.M),                 # python
    re.compile(r"^\s*import\s+([\w\.]+)\s*$", re.M),                     # python
    re.compile(r"""from\s+['"]([^'"]+)['"]"""),                          # js/ts
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),                # node
    re.compile(r"^\s*import\s+(?:static\s+)?([\w\.]+)\s*;", re.M),       # java
    re.compile(r"^\s*using\s+([\w\.]+)\s*;", re.M),                      # c#
    re.compile(r"""#include\s*[<"]([^>"]+)[>"]"""),                       # c/c++
    re.compile(r"^\s*use\s+([\w\\:]+)", re.M),                           # php/rust
]


def fmt_dur(s):
    s = int(s)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Re-type a codebase into VS Code like a human, for screen recordings.")
    p.add_argument("source", help="folder with the existing source code")
    p.add_argument("--out", required=True, help="EMPTY folder where the system is 'rebuilt'")
    p.add_argument("--wpm", type=float, default=32, help="typing speed in words/min (default 32)")
    p.add_argument("--think", type=float, default=1.0,
                   help="multiplier for thinking/reading pauses (default 1.0; 2 = twice as long)")
    p.add_argument("--target-hours", type=float,
                   help="stretch or compress all pacing so the run lasts about this many hours")
    p.add_argument("--max-hours", type=float,
                   help="stop this session after this many hours (continue later with --resume)")
    p.add_argument("--typos", type=float, default=0.015,
                   help="chance of a typo per letter (default 0.015)")
    p.add_argument("--lookup-min", type=float, default=6, help="min minutes between browser lookups")
    p.add_argument("--lookup-max", type=float, default=15, help="max minutes between browser lookups")
    p.add_argument("--browser-title", default=" - Google Chrome",
                   help="suffix of the browser window title (default ' - Google Chrome')")
    p.add_argument("--no-browser", action="store_true", help="never switch to the browser")
    p.add_argument("--no-mouse", action="store_true", help="no scrolling/clicking around")
    p.add_argument("--exclude", action="append", default=[],
                   help="glob of files/folders to skip, e.g. --exclude 'docs/*' (repeatable)")
    p.add_argument("--max-kb", type=int, default=300, help="files larger than this are copied, not typed")
    p.add_argument("--max-line", type=int, default=MAX_LINE_LEN,
                   help="files with a line longer than this many characters are copied, not typed")
    p.add_argument("--order-file", "--plan", dest="order_file",
                   help="build plan: files in the order to type them, '## milestone' "
                        "headings and 'run <command>' terminal steps")
    p.add_argument("--only-plan", action="store_true",
                   help="type only the files named in the plan (e.g. a short test plan)")
    p.add_argument("--no-copy-assets", action="store_true",
                   help="do not copy images/binaries/lock files at the end")
    p.add_argument("--start-delay", type=int, default=15, help="countdown seconds before starting")
    p.add_argument("--seed", type=int, help="random seed (for repeatable runs)")
    p.add_argument("--resume", action="store_true", help="continue a stopped run")
    p.add_argument("--dry-run", action="store_true",
                   help="only show the file plan and estimated duration; touches nothing")
    a = p.parse_args()

    src = Path(a.source).resolve()
    if not src.is_dir():
        sys.exit(f"Source folder not found: {src}")
    out = Path(a.out).resolve()
    if out == src or src in out.parents:
        sys.exit("--out must be a different folder, outside the source folder.")

    steps, copied = build_plan(src, a.exclude, a.max_kb, a.order_file, a.max_line,
                               a.only_plan)
    if a.only_plan:
        copied = []
    files = [v for k, _, v in steps if k == "type"]
    if not files:
        sys.exit("No text source files found to type.")

    if a.dry_run:
        est = estimate_seconds(src, files, a.wpm, a.think, a.typos)
        runs = [v for k, _, v in steps if k == "run"]
        est += sum(parse_run(r)[0] + 12 for r in runs)
        chars = sum(len(read_source(src / f)) for f in files)
        if not a.order_file:
            plan = Path("replay_plan.txt").resolve()
            plan.write_text("# Typing order - edit and pass back with --plan\n" +
                            "\n".join(f.as_posix() for f in files) + "\n", encoding="utf-8")
        print("Build steps, in order:")
        i = 0
        for kind, _, v in steps:
            if kind == "milestone":
                print(f"\n  == {v}")
            elif kind == "run":
                why = command_unavailable(parse_run(v)[1])
                note = f"   (would be skipped: {why})" if why else ""
                print(f"        $ {parse_run(v)[1]}{note}")
            else:
                i += 1
                print(f"  {i:4d}. {v.as_posix()}")
        if copied:
            print("\nFiles to COPY at the end (not typed):")
            for f, why in copied:
                print(f"        {f.as_posix()}  ({why})")
        print(f"\n{len(files)} files, {chars:,} characters, {len(runs)} terminal steps.")
        print(f"Estimated duration at {a.wpm:g} wpm, think x{a.think:g}: {fmt_dur(est)} "
              "(plus browser lookups).")
        if a.max_hours:
            print(f"With --max-hours {a.max_hours:g}: about "
                  f"{math.ceil(est / (a.max_hours * 3600))} sessions.")
        if not a.order_file:
            print(f"Plan written to {plan}")
        return

    if os.name != "nt":
        sys.exit("The live replay only runs on Windows.")
    try:
        import pynput  # noqa: F401
    except ImportError:
        sys.exit("Missing dependency: run  pip install pynput")
    Replayer(a).run(steps, copied)


if __name__ == "__main__":
    main()
