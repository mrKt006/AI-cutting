from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINE_ENDING_WARNING = "LF will be replaced by CRLF"


def _run(label: str, command: list[str]) -> bool:
    print(f"\n== {label} ==", flush=True)
    print(" ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        print(f"{label} failed with exit code {result.returncode}.")
        return False
    return True


def _run_git_whitespace_check() -> bool:
    label = "Git whitespace check"
    command = ["git", "diff", "--check"]
    print(f"\n== {label} ==", flush=True)
    print(" ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    output = "\n".join(
        line
        for line in (result.stdout.splitlines() + result.stderr.splitlines())
        if LINE_ENDING_WARNING not in line
    ).strip()
    if output:
        print(output)
    if result.returncode:
        print(f"{label} failed with exit code {result.returncode}.")
        return False
    if not output:
        print("No whitespace errors.")
    return True


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    py = sys.executable
    checks = [
        ("Python compile", [py, "-m", "compileall", "web", "src", "scripts"]),
        ("Encoding scan", [py, "scripts/check_encoding.py"]),
        ("Editor timeline plan", [py, "scripts/check_editor_timeline.py"]),
        ("Web error boundaries", [py, "scripts/check_web_error_boundaries.py"]),
        ("Edit page JS", [py, "scripts/check_edit_page_js.py"]),
        (
            "Impeccable detector",
            ["node", ".agents/skills/impeccable/scripts/detect.mjs", "--json", "web/templates/edit.html", "web/static/style.css"],
        ),
    ]

    for label, command in checks:
        if not _run(label, command):
            return 1
    if not _run_git_whitespace_check():
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
