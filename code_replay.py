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

    # Plan file lines:
    #   ## title                      milestone
    #   run [seconds] <command>       type a command into the VS Code terminal
    #   serve <command>               start a long-running server in its own terminal
    #   browse <url|path|clipboard> [| action ...]   use the app in Chrome
    #   restore <step>                re-do <step> at the start of every later session
    #   newfile <path> ... endfile    type a file that is not in the source (e.g. .env)
    #   anything else                 a source file to type
    index = {p.as_posix(): p for p in typed_paths}
    used, steps = set(), []
    newfile = None
    for n, raw in enumerate(Path(order_file).read_text(encoding="utf-8").splitlines(), 1):
        if newfile is not None:
            if raw.strip() == "endfile":
                steps.append(("newfile", f"newfile#{newfile[2]}", (newfile[0], "\n".join(newfile[1]) + "\n")))
                newfile = None
            else:
                newfile[1].append(raw)
            continue
        line = raw.strip()
        if line.startswith("## "):
            steps.append(("milestone", None, line[3:].strip()))
        elif not line or line.startswith("#"):
            continue
        elif line.startswith("newfile "):
            newfile = (line[8:].strip(), [], n)
        elif line.startswith(("run ", "serve ", "browse ", "restore ")):
            kind, rest = line.split(None, 1)
            steps.append((kind, f"{kind}#{n}", rest.strip()))
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
    first = cmd.split()[0]
    if first in ("until", "while", "for", "if", "do", "{", "(") or "=" in first:
        # a shell construct: check the programs it uses instead
        progs = [p for p in ("php", "docker", "git", "node", "npm", "python")
                 if re.search(rf"(^|[\s;($|]){p}\b", cmd)]
    else:
        progs = [first]
    for prog in progs:
        if prog == "bash":
            if not GIT_BASH.exists():
                return "Git Bash is not installed"
        elif not shutil.which(prog):
            return f"{prog} is not installed"
    if "docker" in progs or ("test.sh" in cmd and "unit" not in cmd.split()):
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

# VS Code's built-in language defaults (e.g. "[json]" turns suggestions back on inside
# strings) beat the general settings above, so repeat the important ones per language.
_PER_LANGUAGE = {
    "editor.quickSuggestions": {"other": "off", "comments": "off", "strings": "off"},
    "editor.suggestOnTriggerCharacters": False,
    "editor.acceptSuggestionOnEnter": "off",
    "editor.wordBasedSuggestions": "off",
    "editor.autoClosingBrackets": "never",
    "editor.autoClosingQuotes": "never",
    "editor.autoSurround": "never",
    "editor.autoIndent": "none",
    "editor.formatOnType": False,
    "editor.insertSpaces": False,
    "editor.detectIndentation": False,
    "editor.tabCompletion": "off",
    "editor.inlineSuggest.enabled": False,
    "editor.suggest.insertMode": "insert",
}
for _lang in ("json", "jsonc", "markdown", "ignore", "properties", "dotenv", "php", "html",
              "javascript", "typescript", "css", "scss", "shellscript", "dockerfile", "sql",
              "yaml", "python", "plaintext", "xml", "ini", "powershell", "bat", "apacheconf"):
    VSCODE_SETTINGS[f"[{_lang}]"] = dict(_PER_LANGUAGE)


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
        self.cur_lines = None
        self.cur_path = Path(".")
        self.mismatches = 0
        self.preview_tab = False      # Chrome tab used to preview the app
        self.app_key = None
        self.project_funcs = set()    # names the project defines (not worth searching)
        self.project_classes = set()
        self.php_builtins = php_builtin_functions()

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
            self.app_key = st.get("app_key")
            return set(st.get("done", [])), st.get("partial")
        return set(), None

    def save_state(self):
        st: dict = {"done": sorted(self.done)}
        if self.app_key:
            st["app_key"] = self.app_key
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
        want = self.a.browser_title.strip(" -").lower()
        wins = find_windows(lambda t: t.endswith(self.a.browser_title) or
                            f" - {want}" in t.lower())
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
        self.checkpoint()

    def checkpoint(self):
        """After a save, compare the file on disk with what should be there by now.
        On a difference, log exactly where and put the correct text back."""
        if not self.cur_step_key or self.cur_lines is None:
            return
        self.sleep(0.6)
        path = self.out / self.cur_path
        expected = "".join(l + "\n" for l in self.cur_lines[:self.cur_line])
        if self.cur_line >= len(self.cur_lines) - 1:
            expected = "\n".join(self.cur_lines)          # whole file typed
        try:
            typed = path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
        except (OSError, UnicodeDecodeError):
            return
        if typed == expected:
            return
        self.log_difference(self.cur_path.as_posix(), expected, typed)
        self.mismatches += 1
        path.write_bytes(expected.encode("utf-8"))
        self.sleep(1.5)                                    # VS Code reloads the file
        if self.mode == "editor":
            self.hotkey(self.Key.ctrl, self.Key.end)

    def log_difference(self, key, expected, typed):
        el, tl = expected.split("\n"), typed.split("\n")
        for i in range(max(len(el), len(tl))):
            e = el[i] if i < len(el) else "<missing>"
            t = tl[i] if i < len(tl) else "<missing>"
            if e != t:
                self.log(f"MISMATCH in {key} line {i + 1} - corrected.\n"
                         f"      expected: {e[:100]!r}\n      typed:    {t[:100]!r}")
                return

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

    def serve_command(self, cmd):
        """Start a long-running server (e.g. php -S) in a terminal of its own, then
        open a fresh terminal so later commands do not land in the server's."""
        missing = command_unavailable(cmd)
        if missing:
            self.log(f"Skipped server step ({missing}): {cmd}")
            return
        m = re.search(r"(?:-S\s+|--port[= ]|:)(?:[\w.]+:)?(\d{2,5})\b", cmd)
        if m:
            import socket
            try:
                socket.create_connection(("127.0.0.1", int(m.group(1))), 0.5).close()
                self.log(f"Server already running on port {m.group(1)}; not starting it again")
                return
            except OSError:
                pass
        self.log(f"Starting server: {cmd}")
        self.cur_label = f"[server] {cmd}"
        self.progress(0, 0)
        self.focus_editor()
        self.sleep(self.rng.uniform(0.8, 2.0) * self.a.think)
        self.hotkey(self.Key.ctrl, self.Key.shift, "`")      # new terminal
        self.mode = "terminal"
        self.sleep(self.rng.uniform(2.0, 3.5))
        self.type_plain(cmd, speed=1.3)
        self.sleep(self.rng.uniform(0.3, 0.9))
        self.tap(self.Key.enter)
        self.sleep(self.rng.uniform(3.0, 5.0))
        self.hotkey(self.Key.ctrl, self.Key.shift, "`")      # a terminal for commands
        self.sleep(self.rng.uniform(1.5, 2.5))
        self.focus_editor()

    def browse(self, spec):
        """Use the app in Chrome:  <url | /path | clipboard> [| action ...]
        actions: type <text>, tab, enter, esc, wait <s>, scroll, look, nolook"""
        parts = [p.strip() for p in spec.split("|")]
        target, actions = parts[0], parts[1:]
        hwnd = self.find_browser() or self.browser
        if not hwnd:
            self.log(f"Preview skipped: no Chrome window is open ({target})")
            return
        self.browser = hwnd
        self.mode = "browser"
        self.expected = hwnd
        if not focus_window(hwnd):
            self.log("Preview skipped: could not bring Chrome to the front")
            self.focus_editor()
            return
        url = target if "://" in target or target == "clipboard" else \
            self.a.app_url.rstrip("/") + "/" + target.lstrip("/")
        self.log(f"Preview in Chrome: {url} " + (" | ".join(actions) if actions else ""))
        self.sleep(0.6)
        self.move_mouse(*self.region_point(hwnd, 0.3, 0.7, 0.3, 0.7))
        if not self.preview_tab:
            self.hotkey(self.Key.ctrl, "t")
            self.preview_tab = True
        else:
            self.hotkey(self.Key.ctrl, "l")
        self.sleep(self.rng.uniform(0.5, 1.0))
        if url == "clipboard":
            self.hotkey(self.Key.ctrl, "v")
        else:
            self.type_plain(url, speed=1.5)
        self.sleep(self.rng.uniform(0.3, 0.8))
        self.tap(self.Key.enter)
        self.sleep(self.rng.uniform(2.5, 4.0))
        look = True
        for act in actions:
            word, _, arg = act.partition(" ")
            if word == "type":
                self.sleep(self.rng.uniform(0.5, 1.5))
                self.hotkey(self.Key.ctrl, "a")       # replace anything Chrome auto-filled
                self.type_plain(arg, speed=1.2)
            elif word in ("tab", "enter", "esc"):
                self.sleep(self.rng.uniform(0.3, 0.9))
                self.tap({"tab": self.Key.tab, "enter": self.Key.enter,
                          "esc": self.Key.esc}[word])
                if word == "enter":
                    self.sleep(self.rng.uniform(2.0, 3.5))
            elif word == "wait":
                self.sleep(float(arg or 2))
            elif word in ("scroll", "look"):
                self.look_at_page(hwnd)
            elif word == "nolook":
                look = False
        if look:
            self.look_at_page(hwnd)
        self.move_mouse(*self.editor_point())
        self.focus_editor()

    def look_at_page(self, hwnd):
        """Read the page: move around it, scroll down and back up."""
        if self.a.no_mouse:
            self.sleep(self.rng.uniform(3.0, 6.0) * self.a.think)
            return
        notches = 0
        for _ in range(self.rng.randint(2, 4)):
            self.move_mouse(*self.region_point(hwnd, 0.2, 0.8, 0.25, 0.85))
            self.sleep(self.rng.uniform(1.0, 3.0) * self.a.think)
            n = self.rng.randint(2, 5)
            self.scroll(-n)
            notches += n
            self.sleep(self.rng.uniform(1.5, 4.0) * self.a.think)
        self.scroll(notches)
        self.sleep(self.rng.uniform(1.0, 2.5) * self.a.think)

    def do_step(self, kind, val, key, file_no, n_files, resume_line):
        """Run one non-milestone plan step; returns the updated file counter."""
        if kind == "run":
            self.run_command(val)
        elif kind == "serve":
            self.serve_command(val)
        elif kind == "browse":
            if not self.a.no_browser:
                self.browse(val)
        elif kind == "newfile":
            path, content = val
            content = content.replace("{{APP_KEY}}", self.get_app_key())
            file_no += 1
            self.type_file(Path(path), content, file_no, n_files,
                           resume_line.pop(key, 0), key=key, content=content)
            self.cur_step_key = None
        else:
            file_no += 1
            self.type_file(val, read_source(self.src / val), file_no, n_files,
                           resume_line.pop(key, 0))
            self.cur_step_key = None
            if not self.a.no_mouse and self.rng.random() < 0.5:
                self.idle_mouse()
            self.sleep(self.rng.uniform(3.0, 10.0) * self.a.think * self.t.scale)
        return file_no

    def get_app_key(self):
        if not self.app_key:
            import base64
            self.app_key = "base64:" + base64.b64encode(os.urandom(32)).decode()
            self.save_state()
        return self.app_key

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
        """A search a developer would plausibly make while writing this file."""
        r = self.rng
        name = rel.name.lower()
        lang = LANG_BY_EXT.get(rel.suffix.lower()) or SPECIAL_LANG.get(name) or \
            (rel.suffix.lstrip(".") or name.lstrip(".") or "code")
        cands = []

        # imported packages / modules (Python, JS, Java, C#, C, PHP `use`)
        mods = []
        for rx in IMPORT_RES:
            mods += [m for m in rx.findall(typed_text) if m and not m.startswith(".")]
        for m in dict.fromkeys(mods):
            m = "/".join(m.split("/")[:2]) if m.startswith("@") else m.split("/")[0]
            cands.append(f"{lang} {m} " + r.choice(["documentation", "example", "usage"]))

        # built-in functions and classes being used (not ones the project defines)
        if rel.suffix.lower() in CALL_LANGS:
            for c in dict.fromkeys(re.findall(r"(?<![\w>$:.])([a-z_][a-z0-9_]{3,})\s*\(",
                                              typed_text)):
                if c in self.project_funcs or c in CALL_KEYWORDS:
                    continue
                if lang == "php" and self.php_builtins and c not in self.php_builtins:
                    continue
                if True:
                    site = "php.net" if lang == "php" else "docs"
                    cands.append(f"{lang} {c} " + r.choice(["manual", "example", site]))
            for c in dict.fromkeys(re.findall(r"\bnew\s+\\?([A-Z]\w+)", typed_text)):
                if c not in self.project_classes:
                    cands.append(f"{lang} {c} class example")

        if re.search(r"\bCREATE\s+TABLE\b", typed_text, re.I):
            cands += ["mysql 8 create table foreign key on delete restrict",
                      "mysql utf8mb4_unicode_ci vs utf8mb4_0900_ai_ci",
                      "mysql composite index column order"]
        if name in FILE_QUERIES:
            cands += FILE_QUERIES[name]
        if rel.suffix.lower() == ".md":
            for h in re.findall(r"^#{1,3}\s+(.+)$", typed_text, re.M):
                h = re.sub(r"[^\w\s-]", " ", h).strip()
                if len(h) > 3:
                    cands.append(f"{h[:50]} best practices")

        fresh = [c for c in dict.fromkeys(cands) if c not in self.done_queries]
        if not fresh:                         # always have something relevant
            words = re.sub(r"[_\-.]+", " ", rel.stem).strip()
            fresh = [q for q in (f"{lang} {words} best practice",
                                 f"{lang} {words} example",
                                 f"{lang} {rel.parent.name} {words}".strip())
                     if q not in self.done_queries]
        if not fresh:
            return None
        q = r.choice(fresh[:12])
        self.done_queries.add(q)
        return re.sub(r"\s+", " ", q)

    def browser_lookup(self, query):
        hwnd = self.find_browser() or self.browser
        if not hwnd:
            self.log(f"Browser lookup skipped: no Chrome window is open ({query})")
            return
        self.browser = hwnd
        self.mode = "browser"
        self.expected = hwnd
        if not focus_window(hwnd):
            self.log("Browser lookup skipped: could not bring Chrome to the front")
            self.focus_editor()
            return
        self.log(f"Browser lookup: {query}")
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

    def type_file(self, rel: Path, text: str, file_no, file_count, start_line=0,
                  key=None, content=None):
        lines = text.split("\n")
        self.cur_label = f"[{file_no}/{file_count}] {rel.as_posix()}"
        self.cur_step_key = key or rel.as_posix()
        self.cur_path = rel
        self.cur_line = start_line
        self.cur_lines = lines
        file_started = time.time()
        self.log(f"Typing {rel.as_posix()} ({len(lines)} lines"
                 + (f", resuming at line {start_line + 1}" if start_line else "") + ")")
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
        self.cur_line = len(lines)            # whole file typed
        self.save()
        self.sleep(self.rng.uniform(1.0, 3.0) * self.a.think)
        self.verify(rel, content)
        self.cur_lines = None
        self.log(f"Finished {rel.as_posix()} in {fmt_dur(time.time() - file_started)}")

    def verify(self, rel, content=None):
        self.sleep(0.8)
        src_bytes = (self.src / rel).read_bytes() if content is None else content.encode("utf-8")
        dst = self.out / rel
        try:
            typed = dst.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
        except (OSError, UnicodeDecodeError):
            typed = None
        expected = read_source(self.src / rel) if content is None else content
        if typed is not None and typed != expected:
            self.log_difference(rel.as_posix(), expected, typed)
            self.mismatches += 1
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
        for f in files:
            if f.suffix.lower() in CALL_LANGS:
                t = read_source(self.src / f)
                self.project_funcs.update(re.findall(r"\bfunction\s+&?(\w+)\s*\(", t))
                self.project_funcs.update(re.findall(r"\bdef\s+(\w+)\s*\(", t))
                self.project_classes.update(re.findall(r"\b(?:class|interface|trait)\s+(\w+)", t))
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
            # A resumed session first restores what an earlier one set up
            # (database container, dev server, signed-in browser).
            for kind, key, val in steps:
                if kind == "restore" and key in done:
                    rk, _, rv = val.partition(" ")
                    self.log(f"Restoring from the last session: {val}")
                    self.do_step(rk, rv.strip(), key + ":r", file_no, len(files), {})
            for kind, key, val in steps:
                if kind == "milestone":
                    self.log(f"== {val}")
                    continue
                if key in done:
                    continue
                if self.time_is_up():
                    self.log(f"Session time limit of {self.a.max_hours:g} h reached.")
                    raise Stopped()
                if kind != "restore":       # restore lines only act in later sessions
                    file_no = self.do_step(kind, val, key, file_no, len(files), resume_line)
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
            where = f", stopped inside {self.cur_path.as_posix()} at line {self.cur_line + 1}" \
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


SPECIAL_LANG = {
    ".gitignore": "gitignore", ".htaccess": "apache htaccess", "dockerfile": "dockerfile",
    ".env.example": "dotenv", "makefile": "makefile", "composer.json": "composer",
    "package.json": "npm",
}
CALL_LANGS = {".php", ".py", ".js", ".ts", ".jsx", ".tsx", ".rb", ".go"}
CALL_KEYWORDS = {
    "array", "isset", "empty", "unset", "list", "echo", "print", "exit", "die", "eval",
    "require", "require_once", "include", "include_once", "function", "return", "elseif",
    "foreach", "while", "switch", "match", "catch", "self", "static", "parent", "fn",
    "range", "print", "len", "super", "this", "then", "else",
}
FILE_QUERIES = {
    ".gitignore": ["gitignore template php project", "gitignore keep empty folder gitkeep"],
    "composer.json": ["composer.json require php extensions ext-pdo_mysql",
                      "composer autoload files without dependencies"],
    ".htaccess": ["htaccess rewrite all requests to public index.php",
                  "apache deny access to directory htaccess"],
    ".env.example": ["php read .env file without composer", "env file APP_KEY base64 32 bytes"],
    "dockerfile": ["dockerfile php 8.2 cli install pdo_mysql"],
    "test.sh": ["bash set -euo pipefail explained", "docker run mysql tmpfs for tests"],
    "readme.md": ["readme template for php project"],
}


def php_builtin_functions():
    """PHP's own function names (asked from the installed php), or an empty set."""
    php = shutil.which("php")
    if not php:
        return set()
    try:
        out = subprocess.run(
            [php, "-r", 'echo implode("\\n", get_defined_functions()["internal"]);'],
            capture_output=True, text=True, timeout=20).stdout
        return set(out.split())
    except (OSError, subprocess.TimeoutExpired):
        return set()


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
    p.add_argument("--app-url", default="http://127.0.0.1:8080",
                   help="where the rebuilt app is served, for 'browse /path' steps")
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
            elif kind in ("run", "serve"):
                cmd = parse_run(v)[1] if kind == "run" else v
                why = command_unavailable(cmd)
                note = f"   (would be skipped: {why})" if why else ""
                print(f"        $ {cmd}{'   [server]' if kind == 'serve' else ''}{note}")
            elif kind == "browse":
                print(f"        [Chrome] {v}")
            elif kind == "restore":
                print(f"        [next sessions start with] {v}")
            elif kind == "newfile":
                i += 1
                print(f"  {i:4d}. {v[0]}   (new file, not from the source)")
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
