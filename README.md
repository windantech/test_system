# Code Replay

Re-types an existing codebase into **VS Code** the way a real developer would, so you
can screen-record "rebuilding the system from scratch". It sends real keyboard and mouse
input to the real VS Code and Chrome windows.

What you will see on screen:
- Each file opened with **Ctrl+P** and typed character by character, at a human speed that varies.
- Typos that get noticed and fixed with Backspace.
- Thinking pauses before new functions and classes, after blank lines and before comments.
- **Ctrl+S** every 12–40 lines.
- Scrolling back up to re-read code, clicking or double-clicking words, the mouse drifting around.
- Every 6–15 minutes it switches to **Chrome**. It opens a tab, searches for a library the
  current file imports (for example "python flask documentation"), scrolls the results, then
  closes the tab and returns to VS Code.

When the run finishes, the output folder holds an exact copy of the system. Every typed file is
checked against the original. Images, lock files and other binary files are copied at the end.

## Setup on a new computer (one time)

1. Copy this folder and your project's source folder to the computer.
2. Double-click **`SETUP.bat`**. Click **Yes** on each administrator prompt. It installs Python,
   Git, VS Code, PHP 8.2 (with the extensions the project needs), Docker Desktop and Chrome,
   then sets up the tool's Python environment.
3. Restart if it asks you to.
4. If it reports that virtualization is off, turn on **Intel Virtualization Technology** (or
   **SVM** on AMD) in the BIOS. Docker needs this, and Docker is only used by the full test suite.
5. Open **Docker Desktop** once and accept its terms.
6. Open **`SETTINGS.bat`** in Notepad. Set `SOURCE` to your project's folder and `OUT` to an
   empty folder where it will be rebuilt.
7. Double-click **`1_TEST_RUN.bat`**. It types 3 small files from your project in about 10 minutes.

## Use

1. **Preview the plan.** This makes no changes:
   ```
   python code_replay.py "C:\path\to\your\system" --out "C:\path\to\rebuilt" --dry-run
   ```
   It lists the files in typing order and estimates the duration. It also writes
   `replay_plan.txt`. Reorder that file if you like and pass it back with `--order-file replay_plan.txt`.

2. **Open Chrome** if you want the browser lookups.

3. **Run it:**
   ```
   python code_replay.py "C:\path\to\your\system" --out "C:\path\to\rebuilt"
   ```
   VS Code opens on the empty output folder. If it asks **"Do you trust the authors?"**, click Trust.
   During the 15-second countdown, arrange your windows and start OBS or your screen recorder.
   Then leave the keyboard and mouse alone.

| Control | What it does |
|---|---|
| **F9** | pause / resume |
| **F10** | stop and save progress |
| Mouse to **top-left corner** | emergency stop |
| `--resume` | continue a stopped run (same command plus `--resume`) |

If you click away from VS Code while it is typing, it pauses by itself instead of typing into the
wrong window. Press F9 to continue.

## Making it take longer (or shorter)

| Option | Effect |
|---|---|
| `--target-hours 40` | stretches all pacing so the whole system takes about 40 hours |
| `--max-hours 3` | stops each session after 3 hours; continue with `--resume` |
| `--wpm 25` | slower typing (default 32) |
| `--think 2` | twice as many and twice as long reading/thinking pauses |
| `--typos 0.03` | more typos |
| `--lookup-min 3 --lookup-max 8` | browser lookups more often |
| `--no-browser` / `--no-mouse` | keyboard only |
| `--exclude "docs/*"` | skip files or folders (repeatable) |

## Build plan: typing in real development order

Pass `--plan build_plan.txt` to type the files in the order the system would really be
built, instead of alphabetical order. A plan file has three kinds of lines:

```
## 0005 - Budgets and expenditure      <- milestone (logged)
migrations/0005_finance.php            <- a file to type
run php -l modules/finance/lib/finance.php   <- typed into the VS Code terminal and run
run 300 bash scripts/test.sh           <- the same, waiting 300 seconds for it to finish
```

A `run` step is skipped and logged if its program, such as `php`, is not installed. Files the
plan doesn't mention are typed at the end.

## Sessions

- `--max-hours 3` stops the session after 3 hours, at the end of the current line.
- `--resume` continues from exactly that line, even in the middle of a file.

`2_START_RECORDING.bat` and `3_RESUME.bat` already use both options.

## Tips

- **Test it first on a small folder** with 2–3 files to see the speed and check that VS Code
  behaves on your setup.
- The tool writes `.vscode/settings.json` in the output folder. This turns off auto-closing
  brackets, auto-indent, suggestions and Copilot, so VS Code does not add characters while the
  tool types. The settings only apply to that folder, and `.vscode` is hidden from the Explorer.
- Turn off notifications (Windows Focus Assist) during long recordings.
- Search terms typed into Chrome come from your code's import names. Use `--no-browser` if you
  don't want that.
- Progress and any problems are logged to `<out>\.vscode\code-replay-log.txt`.
