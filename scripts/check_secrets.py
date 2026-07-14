from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    ("OpenAI-compatible API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "secret environment value",
        re.compile(
            r"(?im)^\s*(?:VOLC_ACCESS_TOKEN|AI_CUTTING_LLM_API_KEY|DEEPSEEK_API_KEY)\s*=\s*[^\s#]{12,}\s*$"
        ),
    ),
)


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    findings: list[tuple[str, int, str]] = []
    for raw_name in result.stdout.split(b"\0"):
        if not raw_name:
            continue
        relative = raw_name.decode("utf-8", errors="surrogateescape")
        path = ROOT / relative
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pattern in PATTERNS:
            for match in pattern.finditer(content):
                line = content.count("\n", 0, match.start()) + 1
                findings.append((relative, line, label))

    if findings:
        print("Potential secrets found in tracked files:")
        for relative, line, label in findings:
            print(f"- {relative}:{line} ({label})")
        return 1
    print("Tracked-file secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
