from __future__ import annotations

from make_subtitle import TimingSegment


def detect_repeated_utterances(segments: list[TimingSegment], max_gap: float = 1.2) -> list[dict]:
    findings: list[dict] = []
    previous: TimingSegment | None = None
    for segment in segments:
        text = _normalize_text(segment.text)
        if not text:
            continue
        if previous:
            prev_text = _normalize_text(previous.text)
            gap = segment.start - previous.end
            if 0 <= gap <= max_gap and _is_probable_repeat(prev_text, text):
                findings.append(
                    {
                        "type": "repeat_candidate",
                        "previous": {
                            "start": round(previous.start, 3),
                            "end": round(previous.end, 3),
                            "text": previous.text,
                        },
                        "current": {
                            "start": round(segment.start, 3),
                            "end": round(segment.end, 3),
                            "text": segment.text,
                        },
                        "gap": round(gap, 3),
                    }
                )
        previous = segment
    return findings


def _normalize_text(text: str) -> str:
    return "".join(ch for ch in text.strip() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _is_probable_repeat(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 4 and shorter in longer:
        return True
    prefix_len = min(len(left), len(right), 8)
    return prefix_len >= 4 and left[:prefix_len] == right[:prefix_len]
