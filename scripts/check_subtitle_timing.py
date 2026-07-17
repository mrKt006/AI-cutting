from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from make_subtitle import TimingSegment, make_cues_from_segmented_timings, make_cues_from_timings  # noqa: E402


def main() -> int:
    timings = [
        TimingSegment(0.05, 0.80, "第一句"),
        TimingSegment(1.20, 2.10, "第二句"),
    ]

    segmented = make_cues_from_segmented_timings(timings, duration=3.0, delay=-0.12)
    assert abs(segmented[0].start - 0.0) < 1e-9
    assert abs(segmented[0].end - 0.68) < 1e-9
    assert abs(segmented[1].start - 1.08) < 1e-9
    assert abs(segmented[1].end - 1.98) < 1e-9

    mapped = make_cues_from_timings("第一句\n第二句", timings, duration=3.0, delay=-0.12)
    assert abs(mapped[0].start - 0.0) < 1e-9
    assert abs(mapped[0].end - 0.68) < 1e-9
    assert abs(mapped[1].start - 1.08) < 1e-9
    assert abs(mapped[1].end - 1.98) < 1e-9

    print("Subtitle timing offset checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
