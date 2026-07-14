from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from transcript_document import TranscriptDocumentError, parse_transcript_document  # noqa: E402


def main() -> int:
    markdown = """# 23. 把成交案例喂给AI，它比你更会找下一个客户

> 系列：AI营销获客

## 原始文案

你公司最值钱的数据，可能躺在聊天记录里。

## 最终文案

[待确认]

## 修改记录

[暂无]
"""
    parsed = parse_transcript_document(markdown, ".md", "2026-07-12_23_案例.md")
    assert parsed.content_title == "把成交案例喂给AI，它比你更会找下一个客户"
    assert parsed.cover_title == ""
    assert parsed.transcript == "你公司最值钱的数据，可能躺在聊天记录里。"
    assert parsed.transcript_source == "原始文案"
    assert any("修改记录" in warning for warning in parsed.warnings)

    explicit = """# 项目名称

## 内容标题
AI为什么读不懂你的业务

## 封面标题
工具再好
也替代不了业务上下文

## 逐字稿
你买再好的工具|AI读不到你们业务的上下文。

## 备注
这一段不参与匹配。
"""
    parsed = parse_transcript_document(explicit, ".md", "demo.md")
    assert parsed.content_title == "AI为什么读不懂你的业务"
    assert parsed.cover_title == "工具再好\n也替代不了业务上下文"
    assert parsed.transcript == "你买再好的工具|AI读不到你们业务的上下文。"
    assert "这一段不参与匹配" not in parsed.transcript

    text = """【内容标题】
AI获客系统

【封面标题】
AI获客
从案例开始

【逐字稿】
我们在广西做AI获客系统。

【拍摄说明】
这里忽略。
"""
    parsed = parse_transcript_document(text, ".txt", "script.txt")
    assert parsed.cover_title == "AI获客\n从案例开始"
    assert parsed.transcript == "我们在广西做AI获客系统。"
    assert any("拍摄说明" in warning for warning in parsed.warnings)

    for invalid, expected in (
        ("# 只有标题", "没有找到有效逐字稿"),
        ("## 逐字稿\n[待确认]", "没有找到有效逐字稿"),
        ("## 逐字稿\n第一份\n## 逐字稿\n第二份", "多个有效"),
    ):
        try:
            parse_transcript_document(invalid, ".md", "invalid.md")
        except TranscriptDocumentError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"Invalid transcript document was accepted: {invalid}")

    print("Transcript document check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
