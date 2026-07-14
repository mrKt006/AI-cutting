from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from transcript_alignment import align_transcript_tokens  # noqa: E402
from subtitle_layout import analysis_break_sets  # noqa: E402


def _tokens(texts: list[str]) -> list[dict]:
    result = []
    cursor = 0.0
    for index, text in enumerate(texts, start=1):
        result.append(
            {
                "id": f"t{index:03d}",
                "text": text,
                "original_text": text,
                "start": cursor,
                "end": cursor + 0.1,
                "timing_source": "asr",
                "edited": False,
            }
        )
        cursor += 0.1
    return result


def main() -> int:
    asr = _tokens(list("我们在广西做") + ["ai"] + list("货客系统"))
    aligned, report = align_transcript_tokens(asr, "我们在广西做AI获客系统。")
    assert report["status"] == "aligned"
    assert "".join(token["text"] for token in aligned) == "我们在广西做AI获客系统"
    assert any(item["original"] == "货" and item["replacement"] == "获" for item in report["corrections"])
    assert aligned[-1]["script_sentence_break_after"] is True

    with_break, break_report = align_transcript_tokens(asr, "我们在广西做|AI获客系统。")
    assert break_report["manual_break_token_ids"]
    break_id = break_report["manual_break_token_ids"][0]
    assert next(token for token in with_break if token["id"] == break_id)["manual_break_after"] is True
    preferred, required, forbidden = analysis_break_sets(with_break, {})
    assert break_id in required and break_id not in forbidden
    assert with_break[-1]["id"] not in required

    spoken_extra = _tokens(list("我们真的在广西做AI获客系统"))
    preserved, extra_report = align_transcript_tokens(spoken_extra, "我们在广西做AI获客系统")
    assert "真的" in "".join(token["text"] for token in preserved)
    assert extra_report["asr_only"]

    rejected, rejected_report = align_transcript_tokens(asr, "这是一份完全不对应当前视频的逐字稿")
    assert rejected_report["status"] == "rejected"
    assert [token["text"] for token in rejected] == [token["text"] for token in asr]

    unequal_asr = _tokens(list("我们开始"))
    unequal, unequal_report = align_transcript_tokens(unequal_asr, "我们现在开始")
    assert "".join(token["text"] for token in unequal) == "我们开始"
    assert unequal_report["script_only"]

    print("Transcript alignment check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
