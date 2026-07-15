from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from safe_json import write_json_file  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="atomic-json-check-") as temporary_dir:
        target = Path(temporary_dir) / "job.json"
        target.write_text('{"status":"queued"}', encoding="utf-8")

        real_replace = os.replace
        attempts = 0

        def flaky_replace(source: str | bytes | Path, destination: str | bytes | Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts <= 3:
                raise PermissionError(5, "simulated transient Windows file lock")
            real_replace(source, destination)

        with patch("safe_json.os.replace", side_effect=flaky_replace):
            write_json_file(
                target,
                {"status": "running", "attempt": 4},
                initial_delay=0.001,
            )

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload == {"status": "running", "attempt": 4}
        assert attempts == 4

        errors: list[Exception] = []

        def writer(index: int) -> None:
            try:
                write_json_file(target, {"writer": index})
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, errors
        final_payload = json.loads(target.read_text(encoding="utf-8"))
        assert final_payload["writer"] in range(12)
        assert not list(target.parent.glob(f".{target.name}.*.tmp"))

    print("Atomic JSON write check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
