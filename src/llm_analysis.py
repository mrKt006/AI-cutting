from __future__ import annotations

import json
import http.client
import hashlib
import math
import difflib
import re
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from safe_json import loads_json
from subtitle_layout import build_layout_context, measure_text


PROMPT_VERSION = "2026-07-17-deletion-survival-v25"
SEMANTIC_SCHEMA_VERSION = "semantic-index-ranges-v1"
SCHEMA_VERSION = "ai-cutting-decisions-v1"

DEFAULT_PROTECTED_TERMS = (
    "产品信息", "获客体系", "获客系统", "企业微信", "视频号", "左上角", "左下角",
    "人工智能", "数字分身", "知识库", "短视频", "精准客户", "精准客资",
    "美甲美睫店", "美甲美睫", "纹绣院长", "纹绣老师", "人事专员",
)


LEGACY_HOLISTIC_PROMPT = """你是中文短视频口播的自动剪辑与字幕导演。一次性阅读输入中的完整逐字稿，统一完成纠错、删词候选和最终字幕断句。
只返回单行严格 JSON：
{"corrections":[{"start_i":0,"end_i":0,"replacement":"正确文字","confidence":0.97,"reason":"同音词纠正"}],"deletions":[{"start_i":10,"end_i":11,"type":"stutter|false_start|exact_repeat|semantic_repeat|filler|redundant","confidence":0.98,"reason":"删除理由"}],"segmented_text":"第一条完整字幕｜第二条完整字幕｜第三条完整字幕"}。
规则：
1. i 是 tokens 的全片整数索引，范围端点均包含；所有范围必须存在、连续、有序。不得输出 token_ids。
2. 禁止补全 ASR 没识别的内容，禁止添加原文没有的词。corrections 只纠正明确错别字或同音词，confidence 必须至少 0.92；replacement 不得改写语气或意思。
3. deletions 只标记真实口播中的结巴、假起始、精确重复、语义重复、填充词或赘词。不得删除数字、品牌、产品、业务专名和行动指令；删除后必须语法完整、语义连续。
   半句后重新起句属于 false_start，必须删除未完成的前半句，绝不能用 correction 补齐它。例如“我们在广……我们在广西做业务”应删除前一个“我们在广”，不能把它补成“我们在广西”。
4. segmented_text 必须是应用 corrections 后的完整逐字稿，只能插入全角竖线“｜”作为字幕边界。删除所有“｜”后必须与应用纠错后的全文逐字一致；即使某段列为 deletion，也不得从 segmented_text 中省略，后端会在剪辑后移除。禁止添加标点、空格或原文没有的词。
5. 每个 token 已提供程序按真实字体计算的 width_units，以及是否位于 ASR 原始话语段末尾的 utterance_break_after。每个“｜”之间的字幕尽量不超过 layout.recommended_max_units，绝对不能超过 layout.absolute_max_units；utterance_break_after 是强语义提示但不是强制断点。结合完整语义和 pause_after_ms 断句，停顿只是参考。
7. 不能拆开词语、专名、数字单位、英文词、修饰语与中心词；不能让上一条停在未完成的“修饰语/领属/介词/连词/否定/数量/动词必需成分”，避免孤字和极短残句。
8. 在输出前逐个检查相邻字幕的主谓宾归属：如果上一条是名词短语，而下一条以属于它的谓语开头，必须把谓语移回上一条或重新选择更早的断点；不能出现“主语｜谓语”式断裂。相邻重复词可能分别属于前句宾语和后句主语，不要机械合并到同一条。
   并列领属项的结构示例：原文“我们的门店我们的渠道交给系统”，应按“我们的门店｜我们的渠道交给系统”理解；不能按“我们的门店我们的｜渠道交给系统”或“我们的门店我们的渠道｜交给系统”断开。
9. 硬宽度下若长定语句不存在完全独立的断点，按以下次优顺序补 semantic_breaks：完整分句后、完整时间/地点状语后、完整并列项后、存在句或数量引导短语后。允许保留轻微跨条依赖，但绝不能拆词、留下单字修饰语或超宽。
   对“完整引导语或情态短语＋把/将字结构”，超宽时可以在“把/将”之前断，例如“你只需要｜把资料发给系统”；不能断成“你只需要把你自己的｜资料”。
10. 如果长句无法在推荐值内自然断开，可使用绝对上限；不要为了凑长度制造坏断句。"""


HOLISTIC_PROMPT = """你是中文短视频口播的自动剪辑与字幕导演。阅读完整 ASR 原文，统一完成纠错、删词和语义断点标注。最终字幕由后端按真实字体像素全局求解，你不要估算或决定最终排版。

只返回一份完整的标注文案，不要 JSON、Markdown 代码块、解释、标题或前后说明。固定语法：
/完整语义段/下一语义段/  半角斜杠是强断点；全文开头和结尾各一个
/长句中的短语|另一个短语/  半角竖线是备用断点：两侧都可以自然朗读，但不要求最终一定采用
[-删除原文-]           方括号内以减号包住要从音视频删除的原文
[错误原文=>正确文字]   纠正 ASR 错字；左侧保留原文，右侧是字幕显示文字

示例：
原文：这是一个一个示范文案货客系统
返回：/这是[-一个-]|一个示范文案[货客=>获客]/系统/
原文：你的服务告诉aiai能直接工作
返回：/你的服务|告诉[ai=>AI]/[ai=>AI]能直接工作/
注意：两个相邻 ai 具有不同语法角色，必须各自保留，绝不能直接改成一个 AI。
删除块必须先用 -] 完整闭合，再写字幕边界。正确：/前文/[-删除原文-]/后文/；错误：/前文/[-删除原文/]-/后文/。

硬规则：
1. 原文中的每一个字符必须按原顺序恰好出现一次：普通保留文字直接写；删除文字完整放进 [-...-]；纠错文字完整放在 => 左侧。禁止漏字、重复抄写、换序、补写 ASR 没识别的内容或添加标点空格。
2. 输出必须以 / 开始并以 / 结束。/ 是完整分句、话题转换或非常自然的强断点；| 是长句内部语义尚可的备用断点。请尽量标出长句中的全部自然备用断点，特别是完整状语、并列项、宾语、把/将字结构之前；不要在每个字之间滥加 |。删除块和纠错块可以位于任一语义区间内。
3. 纠错只处理明确错别字、同音词和英文大小写，不改写语气或意思。原文“货客”纠为“获客”必须写成 [货客=>获客]，不能直接把原文改成“获客”；连续两个相同英文 token 必须分别纠错，不能合并。
4. 删除只处理真实结巴、假起始、精确重复、无意义填充词和高置信赘词。相邻重复必须只删其中一遍，保留语法角色不同的重复词。例如“告诉 AI AI 能工作”中的两个 AI 分别是宾语和主语，不能删除。
5. 不得删除数字、英文、品牌、产品、业务专名、行动指令以及承载主谓宾的必要成分。删除后必须语法完整、语义连续。
6. 断点必须位于完整语义成分之间，不能拆开词语、专名、数字单位、英文词、修饰语与中心词，不能留下孤字、助词开头或明显的主谓断裂。应写 /你的工厂/你的服务|告诉 AI/，不能写 /你的工厂你的/服务告诉 AI/。
7. 不需要遵守像素宽度，也不要为了凑字数制造坏断句。后端已经知道字体、字号和安全区，会从你标注的强断点与备用断点中选择，并在必要时使用经过保护词校验的本地边界。
8. 输入同时提供“必须逐字映射的原文”和“带时间信息的只读口播视图”。只读视图中的时间、停顿、话语段编号和说明不是原文，绝对不能复制到输出。停顿和 ASR 话语段边界只用于还原说话过程，不能单独证明重复，也不能机械地作为字幕边界。

【表达意图链规则】
9. 不要逐字寻找重复。先在内部把相邻口播按表达意图归组：如果连续片段在表达同一件事，并且后一个版本重新开始、纠正、补充或说得更完整，它们属于同一个“重说链”。
10. 对“残句 A → 重说 B → 最终完整版本 C”，如果 C 已完整承载原意，必须把 A+B 作为完整连续范围删除，只保留 C；绝不能只删除中间的 B 而留下 A。短残句即使与 C 不是逐字重复，也必须随被替代的旧尝试一起删除。
11. 判断重说应基于纠错后的语义，不要求逐字相同。ASR 同音错字、英文大小写，以及最终版本增加少量必要修饰内容，不影响它们属于同一表达意图。旧尝试中包含业务词时，只有最终版本仍完整保留相同业务概念才允许删除。
12. 相同文字如果承担不同语法角色就不是重说，例如“告诉 AI，AI 能工作”的两个 AI 分别是宾语和主语；并列项如“你的工厂、你的服务”也必须保留。
13. 输出前在内部应用全部删除和纠错，并从每个删除位置前后连续朗读：不得留下“在广”“我们在”“我想要把”等未完成残句；不得仍保留两个表达同一意图的版本；不得造成主谓宾或中心词缺失。任一检查不通过，必须扩大、缩小或取消删除范围。

多次重说示例：
原文：在广我们在广西做获客系统我们在广西做ai货客系统
正确：/[-在广我们在广西做获客系统-]/我们在广西做[ai=>AI][货客=>获客]系统/
错误：/在广/[-我们在广西做获客系统-]/我们在广西做[ai=>AI]货客系统/
错误原因：只删除中间一次表达会留下“在广”残句，而且没有纠正最终版本中的同音错字。

14. 输出前先在心里移除所有 /、|、[-、-]、[原文=>纠正] 标记，并把纠错取左侧原文；结果必须与“必须逐字映射的原文”逐字完全相同。"""

HOLISTIC_PROMPT += """
【删除后的语义存活检查】
15. 对一条包含多次假起始、重说和自我纠正的表达链，只删除失败版本，必须保留至少一版信息最完整、语法成立、能够与后文连续朗读的版本。禁止为了省事把整条表达链全部放进同一个删除块。
16. 在输出前模拟执行全部删除。如果删除后下一段以“那、那么、所以、因此、但是、可是、因为、才、就”等承接词开头，必须确认它依赖的条件、原因、转折对象或主语仍保留在前文；否则必须缩小或拆分删除范围。
17. 如果一条重说链中只有某个版本保留了数字、店铺、职业、产品或其他唯一信息，必须保留这个版本。不要把包含唯一信息的最后完整版本与前面的失败尝试一起删除。
示例：原文“如果没有当初……如果当初没有放弃我自己开的美甲店，那我将不会进入这个行业”，只能删除失败尝试并保留一版完整的“如果……美甲店”；禁止删除整个“如果……”表达簇后只留下“那我将不会……”。
"""


SYSTEM_PROMPT = """你是一名专业中文短视频口播剪辑导演。你的目标是直接生成紧凑、自然、信息密度高的自动剪辑决策，不要把决定交回用户确认。
只使用输入中的 token ID，不编造文本、音频或时间。区分：ASR 识别错字只改字幕；说话者卡壳、结巴、半句重说、重复表达和无意义填充词应删除对应视频。
优先保留表达完整、自然、信息更多的一遍。不得删除关键数字、品牌名、产品名、行动指令。删除后必须保持语法和语义连续。
断句以完整语义成分为单位：完整句、主谓结构、宾语或完整短语结束后可以成为断点，但不要机械地在每个宾语后断开。断点前后都必须能自然朗读，不能留下孤立的否定词、代词、助词或一两个字残句。
返回严格 JSON 对象：
corrections: [{token_ids:[...],replacement:string,confidence:0-1,reason:string}]
break_hints: [{after_token_id:string,confidence:0-1,reason:string}]
allowed_breaks: [{after_token_id:string,confidence:0-1,reason:string}]。标注长句内部所有自然短语边界，供字幕宽度不足时选择。
forbidden_breaks: [{token_ids:[...],text:string,confidence:0-1,reason:string}]。每个不能拆开的词组、否定短语、代词、数字单位、品牌名和专有名词必须返回完整且连续的 token IDs；禁止只返回一个边界 ID；text 必须与 token 拼接结果一致。
delete_ranges: [{token_ids:[...],type:"stutter|false_start|exact_repeat|semantic_repeat|filler|redundant",confidence:0-1,reason:string}]
repeat_candidates: 与 delete_ranges 中重复相关项目兼容的候选列表。
final_sentences: [{token_ids:[...],text:string}]
示例：原文“我们在广……我们在广西做获客系统”，删除前一段，type=false_start，保留后一段。
示例：原文“每天每天几十个精准进线”，删除第一个“每天”，type=stutter。"""

LAYOUT_PROMPT = """你是中文短视频字幕排版导演。系统已经生成全部合法候选句，你只负责选择最终组合，不要自己创建句子，不要解释。
返回严格 JSON：{"option_ids":["o000-006","o006-012"]}。
规则：
1. 只能从 constraints.line_options 选择 option_id，禁止返回自创文本或 token_ids。
2. 第一项必须从第一个 token 开始；后一项 start_token_id 必须紧接前一项 end_token_id；最后一项必须覆盖最后一个 token。
3. 不得漏选、重复、交叉或换序。所有候选已经通过最大宽度、完整语义句和禁断词组校验。
4. 优先选择 natural=true 且宽度接近 comfortable_width_px 的候选，同时避免只有一两个字的孤行。
5. 断句应口语自然、语义完整，不能把否定词、代词、数字单位、英文单词和固定搭配拆散。"""


COMPACT_LAYOUT_PROMPT = """
你是中文短视频字幕断句导演。请先理解整段口语，再在像素限制内规划字幕，不能按字数机械切割。
返回严格 JSON：{"line_ends":[6,12]}。line_ends 是每行末尾的左闭右开 token 索引，必须严格递增，最后一个值必须等于 token_count。
规则：
1. 每行必须满足 constraints.line_end_limits 中对应 start 的 hard_end；尽量不超过 comfortable_end。这里是后端按真实字体和预设测得的像素上限，不是估算字数。
2. 不得遗漏、重复、交叉、换序或改写 token。
3. 先保证语义和句法完整，再考虑行宽均衡；停顿只是软信号，短暂停顿不能强迫断句。
4. 不得让上一行停在未完成的定语、领属结构、介词结构、连词、否定词、数量结构或动词必需成分上；下一行不能只是上一行所依赖的中心词或补语。
5. 并列成分应在完整项目之间断开，不能把“修饰语+中心词”拆到两行。避免孤立一两个字、孤立助词和极短尾行。
6. constraints.required_ends 必须保留；constraints.forbidden_ends 禁止使用。
7. 如果没有语义自然且物理合法的方案，也必须返回最接近的方案；后端会把实测错误反馈给你重新规划。
"""

LAYOUT_REVIEW_PROMPT = """你是独立的中文字幕断句复核员，只检查语义与句法，不重新排版。
返回严格 JSON：{"approved":true,"issues":[]}。
逐行检查：单独出现是否自然；行尾是否留下未完成的定语、领属、介词、连词、否定、数量或动词结构；下一行是否只是上一行依赖的中心词或补语；并列项目是否被拆坏；是否存在孤字或极短残句。
停顿和宽度不能为坏断句辩护。只要一处不自然就 approved=false，并在 issues 中说明第几行与结构问题。不得要求改写、增字或删除原文。"""

STAGE_PROMPTS = {
    "correction": """你只负责 ASR 错字和同音词纠正。不得改写语气或删词。只返回严格 JSON：
{"corrections":[{"token_ids":["..."],"replacement":"...","confidence":0.0,"reason":"..."}]}。
token_ids 必须存在、连续、有序；replacement 必须是原片段的正确写法。没有错误时返回空数组。""",
    "deletion_candidates": """你只负责找口播中可删除的结巴、假起始、精确重复、语义重复、填充词和赘词候选，不做最终批准。
返回严格 JSON：{"delete_ranges":[{"token_ids":["..."],"type":"stutter|false_start|exact_repeat|semantic_repeat|filler|redundant","confidence":0.0,"reason":"..."}],"repeat_candidates":[]}。
不得选择关键数字、产品名、品牌、行动指令；token 必须连续有序。宁缺毋滥。""",
    "deletion_verification": """你是独立删词复核员。逐项检查 candidates，不能新增候选。删除后必须语法完整、语义连续，并保留数字、专名、产品与行动指令。
返回严格 JSON：{"verified_deletions":[{"candidate_index":0,"approved":true,"confidence":0.0,"reason":"..."}]}。
每个 candidate_index 必须且只能出现一次；confidence 是你独立判断的置信度。""",
    "semantic_spans": """你只负责完整语义句与真正不可拆的专名，不纠错也不删词。只返回单行严格 JSON：
{"forbidden_ranges":[{"start_i":0,"end_i":1,"confidence":0.0,"reason":"真正不可拆词组"}],"protected_ranges":[],"sentence_ends":[5,12]}。
i 是输入 tokens 的块内整数索引，范围端点均包含。sentence_ends 必须严格递增、最后一个值必须等于最后一个 token 的 i，由后端据此完整覆盖所有 token；不得让下一句以“的、了、系、品、角”等承接字开头。每个 range 必须满足 0<=start_i<=end_i<=最后索引且长度最多 8 token。只标注产品名、品牌、英文词、数字单位和固定业务词；绝不能把完整句子标成不可拆词组；不要返回 token_ids、text、break_hints 或 allowed_breaks，不缩进。""",
}


def build_holistic_request(
    tokens: list[dict[str, Any]],
    *,
    layout: dict[str, Any] | None = None,
    style: dict[str, Any] | None = None,
    width: int | None = None,
    height: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the exact token payload shared by production and the prompt lab."""
    compact_tokens = []
    for index, token in enumerate(tokens):
        next_start = float(tokens[index + 1].get("start", token.get("end", 0))) if index + 1 < len(tokens) else float(token.get("end", 0))
        compact_tokens.append(
            {
                "id": token.get("id"),
                "text": token.get("text"),
                "start": round(float(token.get("start", 0)), 3),
                "end": round(float(token.get("end", 0)), 3),
                "pause_after": round(max(0.0, next_start - float(token.get("end", 0))), 3),
                "timing_source": token.get("timing_source", "unknown"),
            }
        )
    layout_payload = dict(layout or {})
    layout_payload.setdefault("recommended_max_units", 8)
    layout_payload.setdefault("absolute_max_units", 9)
    context = build_layout_context(style, int(width), int(height)) if style is not None and width and height else None
    reference_unit = max(1.0, float(layout_payload.get("reference_unit_px") or 1.0))
    fixed_effects = (
        context.outline * 2 + context.effect_extent + context.background_padding * 2
        if context is not None else 0.0
    )

    def source_group(token_id: Any) -> str:
        value = str(token_id or "")
        match = re.match(r"^(u\d+|s\d+)", value)
        return match.group(1) if match else re.split(r"-(?:w|edit)-?", value, maxsplit=1)[0]

    request_tokens = []
    for index, token in enumerate(compact_tokens):
        text_value = str(token.get("text") or "")
        width_units = (
            max(0.1, (measure_text(text_value, context) - fixed_effects) / reference_unit)
            if context is not None else max(0.1, float(len(text_value)))
        )
        request_tokens.append({
            "i": index,
            "text": text_value,
            "pause_after_ms": round(float(token.get("pause_after", 0)) * 1000),
            "width_units": round(width_units, 2),
            "utterance_break_after": bool(
                index + 1 == len(compact_tokens)
                or source_group(token.get("id")) != source_group(compact_tokens[index + 1].get("id"))
            ),
        })
    recommended_limit = float(layout_payload["recommended_max_units"])
    hard_limit = float(layout_payload["absolute_max_units"])
    for start in range(len(request_tokens)):
        total_units = 0.0
        recommended_end = start
        hard_end = start
        candidate_text = ""
        for end in range(start, len(request_tokens)):
            total_units += float(request_tokens[end]["width_units"])
            candidate_text += str(request_tokens[end].get("text") or "")
            if context is not None:
                measured = measure_text(candidate_text, context)
                if measured <= context.comfort_width + 0.5:
                    recommended_end = end
                if measured <= context.hard_width + 0.5:
                    hard_end = end
                else:
                    break
            else:
                if total_units <= recommended_limit + 1e-6:
                    recommended_end = end
                if total_units <= hard_limit + 1e-6:
                    hard_end = end
                else:
                    break
        request_tokens[start]["recommended_end_i"] = recommended_end
        request_tokens[start]["hard_end_i"] = hard_end
    request_input = {"tokens": request_tokens, "layout": layout_payload}
    validation_tokens = [
        {
            **token,
            "hard_end_i": request_tokens[index]["hard_end_i"],
            "recommended_end_i": request_tokens[index]["recommended_end_i"],
            "utterance_break_after": request_tokens[index]["utterance_break_after"],
        }
        for index, token in enumerate(compact_tokens)
    ]
    return request_input, validation_tokens


def build_markup_request(
    tokens: list[dict[str, Any]],
    *,
    layout: dict[str, Any] | None = None,
    style: dict[str, Any] | None = None,
    width: int | None = None,
    height: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    physical_request, validation_tokens = build_holistic_request(
        tokens, layout=layout, style=style, width=width, height=height
    )
    request_tokens = physical_request["tokens"]
    speech_timeline: list[dict[str, Any]] = []
    segment_start = 0
    for index, token in enumerate(validation_tokens):
        if not token.get("utterance_break_after"):
            continue
        segment_tokens = validation_tokens[segment_start : index + 1]
        segment_request_tokens = request_tokens[segment_start : index + 1]
        segment_text = "".join(str(item.get("text") or "") for item in segment_tokens)
        internal_pauses: list[dict[str, Any]] = []
        char_offset = 0
        for local_index, item in enumerate(segment_request_tokens[:-1]):
            char_offset += len(str(item.get("text") or ""))
            pause_ms = int(item.get("pause_after_ms") or 0)
            if pause_ms >= 80:
                internal_pauses.append({"after_char": char_offset, "pause_ms": pause_ms})
        speech_timeline.append({
            "segment": len(speech_timeline) + 1,
            "start_ms": round(float(segment_tokens[0].get("start", 0)) * 1000),
            "end_ms": round(float(segment_tokens[-1].get("end", 0)) * 1000),
            "text": segment_text,
            "pause_after_ms": int(request_tokens[index].get("pause_after_ms") or 0),
            "boundary_after": "end_of_transcript" if index == len(validation_tokens) - 1 else "asr_utterance",
            "internal_pauses": internal_pauses,
        })
        segment_start = index + 1
    return {
        "transcript": "".join(str(token.get("text") or "") for token in validation_tokens),
        "speech_timeline": speech_timeline,
        "layout": physical_request["layout"],
    }, validation_tokens


def analyze_transcript(
    tokens: list[dict[str, Any]],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float = 60.0,
    cache_dir: str | Path | None = None,
    layout: dict[str, Any] | None = None,
    style: dict[str, Any] | None = None,
    width: int | None = None,
    height: int | None = None,
    prompt_override: str | None = None,
    max_attempts: int = 3,
    include_debug: bool = False,
) -> dict[str, Any]:
    if not base_url or not model or not api_key or not tokens:
        return _empty_analysis("not_configured")
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    request_input, validation_tokens = build_markup_request(
        tokens, layout=layout, style=style, width=width, height=height
    )
    effective_prompt = (prompt_override or HOLISTIC_PROMPT).strip()
    prompt_fingerprint = hashlib.sha256(effective_prompt.encode("utf-8")).hexdigest()[:16]
    cache_key = _cache_key(f"{PROMPT_VERSION}:{prompt_fingerprint}", model, request_input)
    cached = None if prompt_override else _read_cache(cache_dir, cache_key)
    if cached is not None:
        return {**cached, "cached": True, "usage": _empty_usage(), "api_calls": 0, "cache_hits": 1}
    result = _request_holistic_analysis(
        request_input,
        endpoint=endpoint,
        model=model,
        api_key=api_key,
        timeout=timeout,
        style=style,
        width=width,
        height=height,
        source_tokens=validation_tokens,
        system_prompt=effective_prompt,
        max_attempts=max(1, min(3, int(max_attempts))),
        include_debug=include_debug,
    )
    if result.get("status") == "ok":
        _inject_lexical_protection(validation_tokens, result)
        if not prompt_override:
            _write_cache(cache_dir, cache_key, result)
    return result


def _request_holistic_analysis(
    request_input: dict[str, Any],
    *,
    endpoint: str,
    model: str,
    api_key: str,
    timeout: float,
    style: dict[str, Any] | None,
    width: int | None,
    height: int | None,
    source_tokens: list[dict[str, Any]],
    system_prompt: str,
    max_attempts: int,
    include_debug: bool,
) -> dict[str, Any]:
    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _markup_user_content(request_input)},
    ]
    messages = list(base_messages)
    retry_errors: list[str] = []
    total_usage = _empty_usage()
    traces: list[dict[str, Any]] = []
    last_error = ""
    last_raw_response = ""
    for attempt in range(1, max_attempts + 1):
        model_key = str(model).strip().casefold()
        is_v4_pro = model_key == "deepseek-v4-pro"
        completion_limit = 32768 if is_v4_pro else 8192
        payload = {
            "model": model,
            "max_tokens": completion_limit,
            "messages": messages,
        }
        if model_key == "deepseek-v4-flash":
            # V4 models default to thinking mode. This task must spend its output
            # budget on reproducing the complete annotated transcript, not CoT.
            payload["thinking"] = {"type": "disabled"}
            payload["temperature"] = 0
        elif is_v4_pro:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = "high"
        else:
            payload["temperature"] = 0
        started = time.perf_counter()
        raw_response = ""
        try:
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            effective_timeout = max(float(timeout), 240.0) if is_v4_pro else float(timeout)
            with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            choice = body["choices"][0]
            raw_response = str(choice.get("message", {}).get("content") or "")
            usage = _normalize_usage(body.get("usage"))
            total_usage = {key: total_usage[key] + usage[key] for key in total_usage}
            last_raw_response = raw_response
            if not raw_response.strip():
                if choice.get("finish_reason") == "length" or usage["completion_tokens"] >= completion_limit - 1:
                    raise ValueError(
                        f"DeepSeek 输出达到 {completion_limit} token 上限，最终标注文案尚未生成完整"
                    )
                raise ValueError("DeepSeek 返回了空标注文案")
            if raw_response.lstrip().startswith("{"):
                # Compatibility for old cached tests and manually retained prompts.
                parsed = _parse_json_content(raw_response)
                result = _materialize_holistic_index_result(
                    parsed,
                    source_tokens,
                    request_input["layout"],
                    style=style,
                    width=width,
                    height=height,
                )
                pipeline_version = "holistic-transcript-v1"
                semantic_schema_version = "holistic-caption-ends-v3"
            else:
                result = _materialize_holistic_markup_result(
                    raw_response,
                    source_tokens,
                    request_input["layout"],
                    style=style,
                    width=width,
                    height=height,
                )
                pipeline_version = "holistic-markup-v1"
                semantic_schema_version = "holistic-markup-v1"
            _validate_deletion_survival(result, source_tokens)
            trace_result = {**result, "raw_response": raw_response, "latency_ms": round((time.perf_counter() - started) * 1000), "attempt_count": attempt, "retry_errors": list(retry_errors), "usage": usage}
            traces.append(_decision_trace("holistic_transcript", source_tokens, trace_result, endpoint, model, request_input["layout"]))
            result.update({
                "status": "ok",
                "pipeline_version": pipeline_version,
                "prompt_version": PROMPT_VERSION,
                "semantic_schema_version": semantic_schema_version,
                "usage": total_usage,
                "api_calls": attempt,
                "cache_hits": 0,
                "attempt_count": attempt,
                "retry_errors": retry_errors,
                "decision_traces": traces,
            })
            if include_debug:
                result.update({
                    "debug_request": request_input,
                    "debug_system_prompt": system_prompt,
                    "raw_response": raw_response,
                    "latency_ms": trace_result["latency_ms"],
                    "model": model,
                })
            return result
        except (
            KeyError, IndexError, TypeError, ValueError, RuntimeError, json.JSONDecodeError,
            urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
        ) as exc:
            last_error = f"{exc.__class__.__name__}: {str(exc)[:800]}"
            retry_errors.append(last_error)
            traces.append(_decision_trace(
                "holistic_transcript",
                source_tokens,
                {"status": "failed", "reason": last_error, "raw_response": raw_response, "latency_ms": round((time.perf_counter() - started) * 1000), "attempt_count": attempt, "retry_errors": list(retry_errors)},
                endpoint,
                model,
                request_input["layout"],
            ))
            if attempt < max_attempts:
                messages = [
                    *base_messages,
                    {"role": "assistant", "content": raw_response or "{}"},
                    {"role": "user", "content": "上次完整标注文案未通过本地严格校验：" + last_error + "。请从头重新返回覆盖全部原文的完整标注文案，不要解释，也不要只返回局部。"},
                ]
    failed = _empty_analysis(last_error or "invalid_response")
    failed.update({"pipeline_version": "holistic-markup-v1", "prompt_version": PROMPT_VERSION, "usage": total_usage, "api_calls": max_attempts, "attempt_count": max_attempts, "retry_errors": retry_errors, "decision_traces": traces})
    if include_debug:
        failed.update({
            "debug_request": request_input,
            "debug_system_prompt": system_prompt,
            "raw_response": last_raw_response,
            "latency_ms": traces[-1].get("latency_ms", 0) if traces else 0,
            "model": model,
        })
    return failed


def _validate_deletion_survival(result: dict[str, Any], tokens: list[dict[str, Any]]) -> None:
    """Reject AI delete plans that remove the antecedent of a retained consequence."""
    operations = [row for row in result.get("delete_ranges", []) if isinstance(row, dict)]
    if not operations or not tokens:
        return
    token_index = {str(token.get("id") or ""): index for index, token in enumerate(tokens)}
    deleted_indices: set[int] = set()
    operation_ranges: list[tuple[int, int, dict[str, Any]]] = []
    for operation in operations:
        source_indices = operation.get("source_indices") or []
        if isinstance(source_indices, list) and len(source_indices) == 2:
            start_i, end_i = int(source_indices[0]), int(source_indices[1])
        else:
            indices = sorted(
                token_index[token_id]
                for token_id in (str(value) for value in operation.get("token_ids", []))
                if token_id in token_index
            )
            if not indices:
                continue
            start_i, end_i = indices[0], indices[-1]
        if start_i < 0 or end_i >= len(tokens) or end_i < start_i:
            continue
        operation_ranges.append((start_i, end_i, operation))
        deleted_indices.update(range(start_i, end_i + 1))

    if not deleted_indices:
        return
    ordered = sorted(deleted_indices)
    runs: list[tuple[int, int]] = []
    run_start = run_end = ordered[0]
    for index in ordered[1:]:
        if index == run_end + 1:
            run_end = index
            continue
        runs.append((run_start, run_end))
        run_start = run_end = index
    runs.append((run_start, run_end))

    condition_markers = ("如果", "假如", "要是", "只要", "倘若")
    consequence_markers = ("那", "那么", "所以", "因此")
    for start_i, end_i in runs:
        removed_text = "".join(str(tokens[index].get("text") or "") for index in range(start_i, end_i + 1))
        left_indices = [index for index in range(max(0, start_i - 36), start_i) if index not in deleted_indices]
        right_indices = [index for index in range(end_i + 1, min(len(tokens), end_i + 37)) if index not in deleted_indices]
        left_text = "".join(str(tokens[index].get("text") or "") for index in left_indices)
        right_text = "".join(str(tokens[index].get("text") or "") for index in right_indices)
        removed_condition = any(marker in removed_text for marker in condition_markers)
        retained_condition = any(marker in left_text for marker in condition_markers)
        orphaned_consequence = right_text.startswith(consequence_markers) and removed_condition and not retained_condition
        if orphaned_consequence:
            raise ValueError(
                f"删除 token {start_i}-{end_i} 会移除完整条件句，导致后文“{right_text[:10]}”失去前置条件；"
                "请只删除失败的假起始和重复部分，保留一版完整的‘如果……’表达"
            )

    for start_i, end_i, operation in operation_ranges:
        if str(operation.get("type") or "") == "redundant" and end_i - start_i + 1 > 24:
            raise ValueError(
                f"赘词删除 token {start_i}-{end_i} 范围过大；请按重说链拆成较小删除段，"
                "并保留信息最完整、能与后文连续朗读的一版"
            )


def _markup_user_content(request_input: dict[str, Any]) -> str:
    layout = request_input.get("layout") or {}
    recommended = layout.get("recommended_max_units", 8)
    absolute = layout.get("absolute_max_units", 9)
    timeline_lines: list[str] = []
    for row in request_input.get("speech_timeline") or []:
        start_seconds = float(row.get("start_ms", 0)) / 1000
        end_seconds = float(row.get("end_ms", 0)) / 1000
        text_value = str(row.get("text") or "")
        internal = row.get("internal_pauses") or []
        internal_note = ""
        if internal:
            internal_note = "；内部气口 " + "、".join(
                f"字符{int(item.get('after_char', 0))}后{int(item.get('pause_ms', 0))}ms"
                for item in internal
            )
        if row.get("boundary_after") == "end_of_transcript":
            boundary_note = "全文结束"
        else:
            boundary_note = f"段后停顿 {int(row.get('pause_after_ms', 0))}ms，ASR 话语段结束"
        timeline_lines.append(
            f"[{int(row.get('segment', 0)):03d}｜{start_seconds:.3f}s–{end_seconds:.3f}s] "
            f"{text_value} 〈{boundary_note}{internal_note}〉"
        )
    timeline_text = "\n".join(timeline_lines) or "（没有可用时间信息）"
    return (
        f"排版参考容量：推荐约 {recommended} 单位，硬上限约 {absolute} 单位，"
        f"真实像素硬宽度 {layout.get('hard_width_px', 0)}px。\n"
        "你只标注语义：用 / 标强断点、用 | 尽量标全长句内可自然朗读的备用断点。"
        "不需要估算最终字幕宽度，后端会用真实字体全局选择。\n"
        "【必须逐字映射的原文】\n"
        "原文开始\n"
        f"{request_input.get('transcript', '')}\n"
        "原文结束\n\n"
        "【带时间信息的只读口播视图】\n"
        "每一行对应一个 ASR 话语段。以下方括号、时间、停顿和尖括号说明均不是原文，"
        "禁止复制到输出；只用于判断气口、重说和表达尝试：\n"
        f"{timeline_text}"
    )


def _materialize_holistic_markup_result(
    raw_markup: str,
    tokens: list[dict[str, Any]],
    layout: dict[str, Any],
    *,
    style: dict[str, Any] | None,
    width: int | None,
    height: int | None,
) -> dict[str, Any]:
    """Parse a human-readable full transcript while conserving every ASR character."""
    if not tokens:
        raise ValueError("完整标注文案没有输入 token")
    markup = str(raw_markup or "").strip()
    if markup.startswith("```"):
        lines = markup.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        markup = "\n".join(lines).strip()
    if not markup:
        raise ValueError("DeepSeek 返回了空标注文案")
    normalized_markup = re.sub(r"\[-([^/\]\n]+?)/\]-", r"[-\1-]", markup)
    syntax_repaired = normalized_markup != markup
    markup = normalized_markup

    source = "".join(str(token.get("text") or "") for token in tokens)
    token_char_ranges: list[tuple[int, int]] = []
    char_cursor = 0
    for token in tokens:
        text = str(token.get("text") or "")
        token_char_ranges.append((char_cursor, char_cursor + len(text)))
        char_cursor += len(text)
    token_boundaries = {0, *[end for _, end in token_char_ranges]}

    def consume(fragment: str, label: str, source_cursor: int) -> tuple[int, int]:
        if not fragment:
            raise ValueError(f"{label}不能为空")
        expected = source[source_cursor : source_cursor + len(fragment)]
        if expected != fragment:
            mismatch = next((i for i, (left, right) in enumerate(zip(expected, fragment)) if left != right), min(len(expected), len(fragment)))
            absolute = source_cursor + mismatch
            raise ValueError(
                f"标注文案在原文字符 {absolute} 处不一致："
                f"期望“{source[absolute:absolute + 12]}”，实际“{fragment[mismatch:mismatch + 12]}”"
            )
        return source_cursor, source_cursor + len(fragment)

    def token_span(start_char: int, end_char: int, label: str) -> tuple[int, int]:
        if start_char not in token_boundaries or end_char not in token_boundaries:
            raise ValueError(f"{label}落在 ASR token 内部，无法映射精确时间戳")
        indices = [
            index for index, (start, end) in enumerate(token_char_ranges)
            if start >= start_char and end <= end_char and end > start
        ]
        if not indices:
            raise ValueError(f"{label}没有对应的 ASR token")
        return indices[0], indices[-1]

    source_cursor = 0
    caption_specs: list[dict[str, Any]] = []
    deletion_specs: list[dict[str, Any]] = []
    correction_specs: list[dict[str, Any]] = []
    markup_repairs: list[dict[str, Any]] = (
        [{"at_char": None, "restored_text": "", "reason": "自动修复了删除标记 [-内容/]- 的闭合顺序"}]
        if syntax_repaired else []
    )
    if not markup.startswith("/") or not markup.endswith("/"):
        raise ValueError("标注文案必须以 / 开始并以 / 结束")

    # `/` is a strong semantic boundary and `|` is an acceptable fallback
    # boundary.  They are model hints, not final captions.  Split only at the
    # top level so correction/deletion syntax remains opaque to the scanner.
    raw_segments: list[tuple[str, str]] = []
    segment_start_index = 1
    in_brackets = False
    for marker_index in range(1, len(markup)):
        character = markup[marker_index]
        if character == "[":
            in_brackets = True
        elif character == "]":
            in_brackets = False
        elif not in_brackets and character in {"/", "|"}:
            raw_segments.append((markup[segment_start_index:marker_index], character))
            segment_start_index = marker_index + 1
    if not raw_segments:
        raise ValueError("标注文案没有任何语义区间")
    ai_boundary_chars: list[dict[str, Any]] = []
    for segment_index, (raw_segment, closing_marker) in enumerate(raw_segments, start=1):
        if raw_segment == "":
            # Tolerate the old paired-slash habit without creating an empty span.
            continue
        segment_start = source_cursor
        display_parts: list[str] = []
        segment_cursor = 0
        while segment_cursor < len(raw_segment):
            if raw_segment.startswith("[-", segment_cursor):
                close = raw_segment.find("-]", segment_cursor + 2)
                if close < 0:
                    raise ValueError(f"区间[{segment_index}]删除标记缺少结尾 -]")
                deleted_text = raw_segment[segment_cursor + 2 : close]
                start_char, end_char = consume(deleted_text, "删除原文", source_cursor)
                deletion_specs.append({"source_text": deleted_text, "start_char": start_char, "end_char": end_char})
                source_cursor = end_char
                segment_cursor = close + 2
                continue
            if raw_segment[segment_cursor] == "[":
                close = raw_segment.find("]", segment_cursor + 1)
                if close < 0:
                    raise ValueError(f"区间[{segment_index}]纠错标记缺少结尾 ]")
                body = raw_segment[segment_cursor + 1 : close]
                if "=>" not in body:
                    raise ValueError("普通方括号不受支持；纠错必须写成 [原文=>正确文字]")
                original, replacement = body.split("=>", 1)
                if not replacement:
                    raise ValueError("纠错后的文字不能为空")
                start_char, end_char = consume(original, "纠错原文", source_cursor)
                correction_specs.append({
                    "source_text": original,
                    "replacement": replacement,
                    "start_char": start_char,
                    "end_char": end_char,
                })
                display_parts.append(replacement)
                source_cursor = end_char
                segment_cursor = close + 1
                continue
            next_markers = [
                position for position in (
                    raw_segment.find("[-", segment_cursor),
                    raw_segment.find("[", segment_cursor),
                ) if position >= 0
            ]
            next_marker = min(next_markers) if next_markers else len(raw_segment)
            literal = raw_segment[segment_cursor:next_marker]
            if not source.startswith(literal, source_cursor):
                nearby = source.find(literal, source_cursor + 1, min(len(source), source_cursor + 25))
                if nearby > source_cursor:
                    restored = source[source_cursor:nearby]
                    display_parts.append(restored)
                    markup_repairs.append({
                        "at_char": source_cursor,
                        "restored_text": restored,
                        "reason": "模型漏抄原文，按安全策略恢复为保留内容",
                    })
                    source_cursor = nearby
            start_char, end_char = consume(literal, "字幕原文", source_cursor)
            display_parts.append(literal)
            source_cursor = end_char
            segment_cursor = next_marker
        display_text = "".join(display_parts).strip()
        if display_text:
            caption_specs.append({
                "source_start": segment_start,
                "source_end": source_cursor,
                "display_text": display_text,
            })
        elif not any(
            spec["start_char"] >= segment_start and spec["end_char"] <= source_cursor
            for spec in deletion_specs
        ):
            raise ValueError(f"区间[{segment_index}]为空且不是纯删除区间")
        if source_cursor > 0 and source_cursor < len(source):
            ai_boundary_chars.append({
                "after_char": source_cursor,
                "strength": "strong" if closing_marker == "/" else "allowed",
            })
    if source_cursor != len(source):
        raise ValueError(
            f"标注文案没有覆盖完整原文：从字符 {source_cursor} 开始缺少“{source[source_cursor:source_cursor + 20]}”"
        )
    if not caption_specs:
        raise ValueError("标注文案没有任何 /字幕/ 块")

    deletions: list[dict[str, Any]] = []
    deleted_indices: set[int] = set()
    for spec in deletion_specs:
        start_i, end_i = token_span(spec["start_char"], spec["end_char"], "删除标记")
        indices = set(range(start_i, end_i + 1))
        if deleted_indices & indices:
            raise ValueError("删除标记范围重叠")
        deleted_indices.update(indices)
        deleted_text = spec["source_text"]
        left = source[max(0, spec["start_char"] - max(12, len(deleted_text) * 2 + 4)) : spec["start_char"]]
        right = source[spec["end_char"] : spec["end_char"] + max(12, len(deleted_text) * 2 + 4)]
        if right.startswith(deleted_text):
            kind, reason = "exact_repeat", "相邻精确重复，保留后一遍"
        elif left.endswith(deleted_text):
            kind, reason = "exact_repeat", "相邻精确重复，保留前一遍"
        elif deleted_text in {"嗯", "啊", "呃", "额", "那个", "这个", "就是"}:
            kind, reason = "filler", "无意义填充词"
        elif len(deleted_text) >= 2 and (
            right.startswith(deleted_text[:2]) or deleted_text in right[: max(8, len(deleted_text) + 4)]
        ):
            kind, reason = "false_start", "未完成表达后重新起句"
        elif deleted_text and right.startswith(deleted_text[-1]):
            kind, reason = "stutter", "重复起音"
        else:
            kind, reason = "redundant", "AI 完整文案标记的赘词候选"
        span = tokens[start_i : end_i + 1]
        deletions.append({
            "token_ids": [str(token.get("id") or "") for token in span],
            "type": kind,
            "confidence": 0.99 if kind != "redundant" else 0.95,
            "reason": reason,
            "source_indices": [start_i, end_i],
        })

    corrections: list[dict[str, Any]] = []
    correction_indices: set[int] = set()
    for spec in correction_specs:
        start_i, end_i = token_span(spec["start_char"], spec["end_char"], "纠错标记")
        indices = set(range(start_i, end_i + 1))
        if correction_indices & indices or deleted_indices & indices:
            raise ValueError("纠错标记与删除或其他纠错范围重叠")
        correction_indices.update(indices)
        span = tokens[start_i : end_i + 1]
        corrections.append({
            "token_ids": [str(token.get("id") or "") for token in span],
            "replacement": spec["replacement"],
            "confidence": 0.99,
            "reason": "AI 完整文案显式纠错",
            "source_indices": [start_i, end_i],
        })

    context = build_layout_context(style, int(width), int(height)) if style is not None and width and height else None
    hard_width = float(layout.get("hard_width_px") or (context.hard_width if context is not None else 0.0))
    comfort_width = float(context.comfort_width if context is not None else hard_width)

    # Reconstruct display text locally.  A correction is attached to its first
    # source token and its remaining source tokens become zero-width pieces;
    # boundaries inside that correction are forbidden.  Deletion proposals are
    # deliberately kept in the layout projection until the deterministic safety
    # gate approves them.  Applied deletions disappear later when retained tokens
    # are mapped to the cut timeline; rejected proposals therefore remain visible
    # without requiring a second layout pass.
    corrected_pieces = [str(token.get("text") or "") for token in tokens]
    correction_internal_breaks: set[int] = set()
    for row in corrections:
        start_i, end_i = row["source_indices"]
        corrected_pieces[start_i] = str(row["replacement"])
        for index in range(start_i + 1, end_i + 1):
            corrected_pieces[index] = ""
        correction_internal_breaks.update(range(start_i, end_i))
    ai_boundary_scores: dict[int, float] = {}
    ai_boundary_strengths: dict[int, str] = {}
    ai_boundary_sources: dict[int, str] = {}
    ignored_ai_boundaries: list[dict[str, Any]] = []
    token_end_to_index = {end: index for index, (_, end) in enumerate(token_char_ranges)}
    for boundary in ai_boundary_chars:
        after_char = int(boundary["after_char"])
        after_i = token_end_to_index.get(after_char)
        if after_i is None:
            ignored_ai_boundaries.append({**boundary, "reason": "落在 ASR token 内部"})
            continue
        strength = str(boundary["strength"])
        score = 1.0 if strength == "strong" else 0.72
        if score > ai_boundary_scores.get(after_i, -1.0):
            ai_boundary_scores[after_i] = score
            ai_boundary_strengths[after_i] = strength
            ai_boundary_sources[after_i] = "deepseek"

    protected_term_breaks: set[int] = set()
    corrected_full_text = "".join(corrected_pieces)
    corrected_char_to_token = [
        token_index
        for token_index, piece in enumerate(corrected_pieces)
        for _ in piece
    ]
    for term in DEFAULT_PROTECTED_TERMS:
        search_from = 0
        while True:
            found = corrected_full_text.casefold().find(term.casefold(), search_from)
            if found < 0:
                break
            char_end = found + len(term) - 1
            if char_end < len(corrected_char_to_token):
                first_token = corrected_char_to_token[found]
                last_token = corrected_char_to_token[char_end]
                protected_term_breaks.update(range(first_token, last_token))
            search_from = found + max(1, len(term))

    # Repeated possessive phrases are common parallel structures in spoken
    # Chinese: “我们的门店我们的渠道”, “你的工厂你的服务”.  They provide a
    # broadly useful local candidate before the repeated item, especially when
    # the model accidentally proposes a subject/predicate split later.
    possessive_groups: dict[str, list[int]] = {}
    for match in re.finditer(r"(我们|你们|他们|她们|我|你|他|她)(?:自己)?的", corrected_full_text):
        possessive_groups.setdefault(match.group(1), []).append(match.start())
    for occurrences in possessive_groups.values():
        for previous_char, current_char in zip(occurrences, occurrences[1:]):
            if current_char - previous_char > 30 or current_char <= 0:
                continue
            boundary_token = corrected_char_to_token[current_char - 1]
            if 0 <= boundary_token < len(tokens) - 1 and ai_boundary_scores.get(boundary_token, 0.0) < 0.94:
                ai_boundary_scores[boundary_token] = 0.94
                ai_boundary_strengths[boundary_token] = "strong"
                ai_boundary_sources[boundary_token] = "parallel_possessive_heuristic"

    # Adjacent identical ASCII words often represent a role hand-off rather
    # than a stutter (“告诉 AI / AI 能…”).  If the deletion safety gate later
    # proves one copy redundant it disappears naturally; otherwise this is the
    # least destructive and most readable boundary.
    for index in range(len(corrected_pieces) - 1):
        left = corrected_pieces[index].strip()
        right = corrected_pieces[index + 1].strip()
        if (
            left and right and left.casefold() == right.casefold()
            and all(character.isascii() for character in left + right)
            and ai_boundary_scores.get(index, 0.0) < 0.95
        ):
            ai_boundary_scores[index] = 0.95
            ai_boundary_strengths[index] = "strong"
            ai_boundary_sources[index] = "adjacent_ascii_role_boundary"

    def source_group(index: int) -> str:
        value = str(tokens[index].get("id") or "")
        match = re.match(r"^(u\d+|s\d+)", value)
        return match.group(1) if match else re.split(r"-(?:w|edit)-?", value, maxsplit=1)[0]

    chinese_number_chars = set("零〇一二三四五六七八九十百千万亿两点年月日号")
    bad_right_starts = {"的", "了", "系", "品", "角", "告"}

    def safe_boundary(after_i: int, run_end: int) -> bool:
        if after_i == run_end:
            return True
        if after_i < 0 or after_i >= len(tokens) - 1:
            return False
        if after_i in correction_internal_breaks or after_i in protected_term_breaks:
            return False
        left_id = str(tokens[after_i].get("id") or "")
        right_id = str(tokens[after_i + 1].get("id") or "")
        if re.sub(r"-c\d+$", "", left_id) == re.sub(r"-c\d+$", "", right_id) and "-c" in left_id:
            return False
        left_text = corrected_pieces[after_i].strip()
        right_text = corrected_pieces[after_i + 1].strip()
        if not left_text or not right_text:
            return False
        if (
            left_text[-1:].isascii() and right_text[:1].isascii()
            and left_text[-1:].isalnum() and right_text[:1].isalnum()
            and left_text.casefold() != right_text.casefold()
        ):
            return False
        if left_text[-1:] in chinese_number_chars and right_text[:1] in chinese_number_chars:
            return False
        if right_text[:1] in bad_right_starts:
            return False
        return True

    dependency_tails = set("的在往从向把将给跟和与或而被让叫使为对比因如若但并且就才又还更最很太也都能会要需可")
    dependency_heads = set("的了着过吗呢吧啊呀嘛么")

    def solve_run(start_i: int, end_i: int) -> list[tuple[int, int]]:
        best: dict[int, tuple[float, int | None]] = {start_i: (0.0, None)}
        for cursor in range(start_i, end_i + 1):
            if cursor not in best:
                continue
            for candidate_end in range(cursor, end_i + 1):
                if not safe_boundary(candidate_end, end_i):
                    continue
                display_text = "".join(corrected_pieces[cursor : candidate_end + 1]).strip()
                if not display_text:
                    continue
                measured = measure_text(display_text, context) if context is not None else 0.0
                if hard_width and measured > hard_width + 0.5:
                    break
                ratio = measured / max(1.0, comfort_width)
                next_cursor = candidate_end + 1
                duration = max(
                    0.0,
                    float(tokens[candidate_end].get("end", 0)) - float(tokens[cursor].get("start", 0)),
                )
                pause = (
                    max(0.0, float(tokens[next_cursor].get("start", 0)) - float(tokens[candidate_end].get("end", 0)))
                    if next_cursor <= end_i else 0.0
                )
                utterance_boundary = next_cursor <= end_i and source_group(candidate_end) != source_group(next_cursor)
                ai_score = ai_boundary_scores.get(candidate_end, 0.0) if candidate_end < end_i else 0.0
                local_boundary_penalty = 1.45 if candidate_end < end_i and ai_score == 0.0 else 0.0
                crossed_strong = sum(
                    1 for index in range(cursor, candidate_end)
                    if ai_boundary_strengths.get(index) == "strong" and safe_boundary(index, end_i)
                )
                crossed_allowed = sum(
                    1 for index in range(cursor, candidate_end)
                    if ai_boundary_strengths.get(index) == "allowed" and safe_boundary(index, end_i)
                )
                semantic_crossing_penalty = crossed_strong * 4.4 + crossed_allowed * 0.55
                left_tail = display_text[-1:]
                next_head = corrected_pieces[next_cursor].strip()[:1] if next_cursor <= end_i else ""
                dependency_penalty = (1.5 if left_tail in dependency_tails else 0.0) + (1.0 if next_head in dependency_heads else 0.0)
                short_penalty = 5.0 if len(display_text) <= 2 and candidate_end < end_i else (1.5 if len(display_text) <= 3 and candidate_end < end_i else 0.0)
                duration_penalty = max(0.0, duration - 3.8) ** 2 * 0.35
                signal_bonus = ai_score * 2.2 + min(1.4, pause * 3.5) + (0.8 if utterance_boundary else 0.0)
                cost = (
                    best[cursor][0]
                    + 3.2
                    + (ratio - 0.84) ** 2 * 1.6
                    + local_boundary_penalty
                    + semantic_crossing_penalty
                    + dependency_penalty
                    + short_penalty
                    + duration_penalty
                    - signal_bonus
                )
                previous = best.get(next_cursor)
                if previous is None or cost < previous[0]:
                    best[next_cursor] = (cost, cursor)
        target = end_i + 1
        if target not in best:
            text = "".join(corrected_pieces[start_i : end_i + 1]).strip()
            measured = measure_text(text, context) if context is not None else 0.0
            raise ValueError(
                f"真实像素全局断句器找不到合法路径：token {start_i}-{end_i}，"
                f"整段宽度 {measured:.1f}px，硬上限 {hard_width:.1f}px；可能存在超宽不可拆词组"
            )
        ranges: list[tuple[int, int]] = []
        cursor = target
        while cursor > start_i:
            previous_cursor = best[cursor][1]
            if previous_cursor is None:
                raise ValueError("真实像素全局断句器回溯失败")
            ranges.append((previous_cursor, cursor - 1))
            cursor = previous_cursor
        ranges.reverse()
        return ranges

    optimized_ranges = solve_run(0, len(tokens) - 1)
    captions: list[dict[str, Any]] = []
    caption_indices: set[int] = set()
    semantic_breaks: list[dict[str, Any]] = []
    used_ai_boundaries: list[dict[str, Any]] = []
    used_local_boundaries: list[int] = []
    for item_index, (start_i, end_i) in enumerate(optimized_ranges, start=1):
        retained = list(range(start_i, end_i + 1))
        caption_indices.update(retained)
        display_text = "".join(corrected_pieces[start_i : end_i + 1]).strip()
        measured = measure_text(display_text, context) if context is not None else 0.0
        if hard_width and measured > hard_width + 0.5:
            raise ValueError(f"全局断句器内部错误：字幕[{item_index}]真实宽度 {measured:.1f}px 超过 {hard_width:.1f}px")
        captions.append({
            "token_ids": [str(tokens[index].get("id") or "") for index in retained],
            "text": display_text,
            "display_text": display_text,
            "model_display_text": None,
            "source_indices": [start_i, end_i],
            "width_px": round(measured, 1) if context is not None else None,
        })
        if item_index < len(optimized_ranges):
            strength = ai_boundary_strengths.get(end_i)
            if strength:
                boundary_source = ai_boundary_sources.get(end_i, "deepseek")
                used_ai_boundaries.append({"after_i": end_i, "strength": strength, "source": boundary_source})
                if boundary_source == "parallel_possessive_heuristic":
                    reason = "并列领属结构候选断点"
                elif boundary_source == "adjacent_ascii_role_boundary":
                    reason = "相邻英文词语法角色切换断点"
                else:
                    reason = "DeepSeek 强语义断点" if strength == "strong" else "DeepSeek 备用语义断点"
                confidence = ai_boundary_scores[end_i]
            else:
                used_local_boundaries.append(end_i)
                reason = "真实像素约束下的本地安全边界"
                confidence = 0.7
            semantic_breaks.append({"after_i": end_i, "confidence": confidence, "reason": reason})

    expected_source_indices = set(range(len(tokens)))
    if caption_indices != expected_source_indices:
        missing = sorted(expected_source_indices - caption_indices)
        unexpected = sorted(caption_indices - expected_source_indices)
        raise ValueError(f"全局断句字幕覆盖异常：缺失={missing[:12]}，额外={unexpected[:12]}")
    validation = {
        "valid": True,
        "errors": [],
        "checked_tokens": len(tokens),
        "caption_coverage": 1.0,
        "caption_token_count": len(caption_indices),
        "deleted_token_count": len(deleted_indices),
        "caption_coverage_basis": "all_source_tokens_before_verified_deletion",
        "pixel_overflows": 0,
        "caption_text_source": "ai_full_transcript_markup",
        "source_projection_exact": True,
        "markup_repairs": markup_repairs,
        "semantic_boundary_optimizer": {
            "status": "validated",
            "ai_candidate_count": len(ai_boundary_scores),
            "used_ai_boundaries": used_ai_boundaries,
            "used_local_boundaries": used_local_boundaries,
            "ignored_ai_boundaries": ignored_ai_boundaries,
            "caption_count": len(captions),
        },
        "layout_capacity": layout,
    }
    return {
        "corrections": corrections,
        "deletion_candidates": deletions,
        "delete_ranges": deletions,
        "repeat_candidates": [row for row in deletions if row["type"] in {"stutter", "false_start", "exact_repeat"}],
        "captions": captions,
        "semantic_breaks": semantic_breaks,
        "final_sentences": captions,
        "break_hints": [],
        "allowed_breaks": [
            {
                "after_token_id": str(tokens[index].get("id") or ""),
                "after_i": index,
                "confidence": ai_boundary_scores[index],
                "reason": (
                    "并列领属结构候选断点"
                    if ai_boundary_sources.get(index) == "parallel_possessive_heuristic"
                    else (
                        "相邻英文词语法角色切换断点"
                        if ai_boundary_sources.get(index) == "adjacent_ascii_role_boundary"
                        else ("DeepSeek 强语义断点" if ai_boundary_strengths[index] == "strong" else "DeepSeek 备用语义断点")
                    )
                ),
            }
            for index in sorted(ai_boundary_scores)
        ],
        "forbidden_breaks": [],
        "protected_spans": [],
        "layout_capacity": layout,
        "layout_decision": {
            "status": "validated_local",
            "source": "deepseek_semantics_deterministic_pixel_optimizer",
            "chunks": [{
                "status": "validated_local",
                "source": "deepseek_semantics_deterministic_pixel_optimizer",
                "sentences": captions,
                "used_ai_boundaries": used_ai_boundaries,
                "used_local_boundaries": used_local_boundaries,
            }],
        },
        "validation": validation,
        "warnings": [row["reason"] + f"：{row['restored_text']}" for row in markup_repairs],
        "annotated_transcript": markup,
    }


def _materialize_holistic_index_result(
    parsed: dict[str, Any],
    tokens: list[dict[str, Any]],
    layout: dict[str, Any],
    *,
    style: dict[str, Any] | None,
    width: int | None,
    height: int | None,
) -> dict[str, Any]:
    """Validate index-only AI decisions and rebuild every caption locally."""
    if not tokens:
        raise ValueError("完整分析没有输入 token")
    last_index = len(tokens) - 1

    def indexed_ranges(key: str, *, min_confidence: float = 0.0) -> list[dict[str, Any]]:
        values = parsed.get(key)
        if not isinstance(values, list):
            raise ValueError(f"{key} 必须是数组")
        rows: list[dict[str, Any]] = []
        for item_index, item in enumerate(values, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"{key}[{item_index}] 必须是对象")
            if key == "captions" and any(field in item for field in ("text", "display_text")):
                raise ValueError(f"captions[{item_index}] 只允许返回索引，不得重新抄写字幕正文")
            try:
                start_i = int(item.get("start_i"))
                end_i = int(item.get("end_i"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key}[{item_index}] start_i/end_i 必须是整数") from exc
            if start_i < 0 or end_i < start_i or end_i > last_index:
                raise ValueError(f"{key}[{item_index}] 索引越界或顺序错误")
            default_confidence = 1.0 if key == "captions" else 0.0
            try:
                confidence = float(item.get("confidence", default_confidence))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key}[{item_index}] confidence 必须是数字") from exc
            if not 0 <= confidence <= 1 or confidence < min_confidence:
                raise ValueError(f"{key}[{item_index}] 置信度低于 {min_confidence:.2f}")
            rows.append({**item, "start_i": start_i, "end_i": end_i, "confidence": confidence})
        return rows

    corrections_raw = indexed_ranges("corrections", min_confidence=0.92)
    deletions_raw = indexed_ranges("deletions")
    captions_raw: list[dict[str, Any]] = []
    if "caption_ends" in parsed:
        if parsed.get("captions") not in (None, []):
            raise ValueError("使用 caption_ends 时禁止同时返回 captions")
        raw_caption_ends = parsed.get("caption_ends")
        if not isinstance(raw_caption_ends, list):
            raise ValueError("caption_ends 必须是整数索引数组")
        deleted_for_caption_build = {
            index
            for row in deletions_raw
            for index in range(row["start_i"], row["end_i"] + 1)
        }
        caption_ends: list[int] = []
        previous_end = -1
        for item_index, value in enumerate(raw_caption_ends, start=1):
            try:
                end_i = int(value.get("end_i")) if isinstance(value, dict) else int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"caption_ends[{item_index}] 必须是整数索引") from exc
            if end_i < 0 or end_i > last_index:
                raise ValueError(f"caption_ends[{item_index}] 索引越界")
            if end_i <= previous_end:
                raise ValueError("caption_ends 必须严格递增")
            if end_i in deleted_for_caption_build:
                raise ValueError(f"caption_ends[{item_index}] 不能落在 deletion 范围内")
            caption_ends.append(end_i)
            previous_end = end_i
        requested_ends = set(caption_ends)
        current_start: int | None = None
        for index in range(last_index + 1):
            if index in deleted_for_caption_build:
                if current_start is not None:
                    captions_raw.append({"start_i": current_start, "end_i": index - 1, "confidence": 1.0})
                    current_start = None
                continue
            if current_start is None:
                current_start = index
            next_is_deleted = index < last_index and index + 1 in deleted_for_caption_build
            if index in requested_ends or next_is_deleted or index == last_index:
                captions_raw.append({"start_i": current_start, "end_i": index, "confidence": 1.0})
                current_start = None
    else:
        # Read-only compatibility for historical AI artifacts and cached tests.
        captions_raw = indexed_ranges("captions")
    raw_allowed_breaks = parsed.get("allowed_breaks")
    if not isinstance(raw_allowed_breaks, list):
        raise ValueError("allowed_breaks 必须是整数索引数组")
    allowed_break_indices: set[int] = set()
    for item_index, value in enumerate(raw_allowed_breaks, start=1):
        try:
            after_i = int(value.get("after_i")) if isinstance(value, dict) else int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"allowed_breaks[{item_index}] 必须是整数索引") from exc
        if after_i == last_index:
            # The final token is the natural transcript terminator, not an
            # internal break candidate. Models commonly include it.
            continue
        if after_i < 0 or after_i > last_index:
            raise ValueError(f"allowed_breaks[{item_index}] 索引越界")
        allowed_break_indices.add(after_i)
    if not captions_raw and len(deletions_raw) != 1:
        raise ValueError("captions 不能为空，除非唯一 deletion 覆盖全文")

    occupied_by_kind: dict[str, set[int]] = {}
    for key, rows in (("corrections", corrections_raw), ("deletions", deletions_raw), ("captions", captions_raw)):
        occupied: set[int] = set()
        previous_end = -1
        for item_index, row in enumerate(rows, start=1):
            indexes = set(range(row["start_i"], row["end_i"] + 1))
            if occupied & indexes:
                raise ValueError(f"{key}[{item_index}] 与同类范围重叠")
            if row["start_i"] <= previous_end:
                raise ValueError(f"{key} 必须按索引严格递增")
            occupied.update(indexes)
            previous_end = row["end_i"]
        occupied_by_kind[key] = occupied

    correction_indices = occupied_by_kind["corrections"]
    deleted_indices = occupied_by_kind["deletions"]
    caption_indices = occupied_by_kind["captions"]
    if correction_indices & deleted_indices:
        raise ValueError("corrections 与 deletions 不得重叠")
    if caption_indices & deleted_indices:
        raise ValueError("captions 不得包含或跨越 deletion token")
    expected_retained = set(range(len(tokens))) - deleted_indices
    if caption_indices != expected_retained:
        missing = sorted(expected_retained - caption_indices)
        unexpected = sorted(caption_indices - expected_retained)
        raise ValueError(
            "captions 必须恰好覆盖全部未删除 token："
            f"缺失={missing[:12]}，额外={unexpected[:12]}"
        )
    if deleted_indices and not captions_raw and deleted_indices != set(range(len(tokens))):
        raise ValueError("captions 未覆盖删除范围之外的 token")

    corrected_pieces = [str(token.get("text") or "") for token in tokens]
    corrections: list[dict[str, Any]] = []
    for item_index, row in enumerate(corrections_raw, start=1):
        replacement = str(row.get("replacement") or "").strip()
        if not replacement:
            raise ValueError(f"corrections[{item_index}] replacement 不能为空")
        corrected_pieces[row["start_i"]] = replacement
        for index in range(row["start_i"] + 1, row["end_i"] + 1):
            corrected_pieces[index] = ""
        span = tokens[row["start_i"] : row["end_i"] + 1]
        corrections.append({
            "token_ids": [str(token.get("id") or "") for token in span],
            "replacement": replacement,
            "confidence": row["confidence"],
            "reason": str(row.get("reason") or "")[:300],
            "source_indices": [row["start_i"], row["end_i"]],
        })

    for item_index, row in enumerate(corrections_raw, start=1):
        if not any(
            row["start_i"] >= caption["start_i"] and row["end_i"] <= caption["end_i"]
            for caption in captions_raw
        ):
            raise ValueError(f"corrections[{item_index}] 不得跨越字幕边界")

    allowed_delete_types = {"stutter", "false_start", "exact_repeat", "semantic_repeat", "filler", "redundant"}
    deletions: list[dict[str, Any]] = []
    for item_index, row in enumerate(deletions_raw, start=1):
        kind = str(row.get("type") or "redundant")
        if kind not in allowed_delete_types:
            raise ValueError(f"deletions[{item_index}] 不支持的类型：{kind}")
        span = tokens[row["start_i"] : row["end_i"] + 1]
        deletions.append({
            "token_ids": [str(token.get("id") or "") for token in span],
            "type": kind,
            "confidence": row["confidence"],
            "reason": str(row.get("reason") or "")[:300],
            "source_indices": [row["start_i"], row["end_i"]],
        })

    def hard_boundary_allowed(after_i: int) -> bool:
        if after_i >= last_index:
            return True
        next_i = after_i + 1
        while next_i <= last_index and next_i in deleted_indices:
            next_i += 1
        if next_i > last_index or next_i != after_i + 1:
            return True
        left = tokens[after_i]
        right = tokens[next_i]
        right_text = corrected_pieces[next_i].strip()
        right_prefix = "".join(corrected_pieces[next_i : min(len(corrected_pieces), next_i + 3)]).strip()
        left_text = corrected_pieces[after_i].strip()
        if right_text[:1] == "的":
            return False
        if right_text[:1] == "了" and not right_prefix.startswith("了解"):
            return False
        if right_text[:1] == "系" and left_text.endswith("体"):
            return False
        if right_text[:1] == "品" and left_text.endswith("产"):
            return False
        if right_text[:1] == "角" and left_text.endswith(("上", "下")):
            return False
        left_id = str(left.get("id") or "")
        right_id = str(right.get("id") or "")
        if re.sub(r"-c\d+$", "", left_id) == re.sub(r"-c\d+$", "", right_id) and "-c" in left_id:
            return False
        if left_text[-1:].isascii() and right_text[:1].isascii() and left_text[-1:].isalnum() and right_text[:1].isalnum():
            return False
        return True

    context = build_layout_context(style, int(width), int(height)) if style is not None and width and height else None
    hard_width = float(layout.get("hard_width_px") or (context.hard_width if context is not None else 0.0))
    comfort_width = float(context.comfort_width if context is not None else hard_width)
    correction_internal_breaks = {
        after_i
        for row in corrections_raw
        for after_i in range(row["start_i"], row["end_i"])
    }
    allowed_break_indices -= deleted_indices
    allowed_break_indices -= correction_internal_breaks

    protected_term_breaks: set[int] = set()
    normalized_full_text = "".join(corrected_pieces)
    char_to_token = [
        token_index
        for token_index, piece in enumerate(corrected_pieces)
        for _ in piece
    ]
    for term in DEFAULT_PROTECTED_TERMS:
        search_from = 0
        while True:
            found = normalized_full_text.casefold().find(term.casefold(), search_from)
            if found < 0:
                break
            char_end = found + len(term) - 1
            if char_end < len(char_to_token):
                first_token = char_to_token[found]
                last_token = char_to_token[char_end]
                protected_term_breaks.update(range(first_token, last_token))
            search_from = found + max(1, len(term))

    effective_captions_raw: list[dict[str, Any]] = []
    ai_break_repairs: list[dict[str, Any]] = []
    validated_local_repairs: list[dict[str, Any]] = []

    def source_group(index: int) -> str:
        value = str(tokens[index].get("id") or "")
        match = re.match(r"^(u\d+|s\d+)", value)
        return match.group(1) if match else re.split(r"-(?:w|edit)-?", value, maxsplit=1)[0]

    chinese_number_chars = set("零〇一二三四五六七八九十百千万亿两点年月日号")

    def locally_safe_boundary(after_i: int) -> bool:
        if (
            not hard_boundary_allowed(after_i)
            or after_i in correction_internal_breaks
            or after_i in protected_term_breaks
        ):
            return False
        left_text = corrected_pieces[after_i].strip()
        right_text = corrected_pieces[after_i + 1].strip() if after_i + 1 < len(corrected_pieces) else ""
        if left_text[-1:] in chinese_number_chars and right_text[:1] in chinese_number_chars:
            return False
        return True

    def solve_split_path(start_i: int, end_i: int, candidate_ends: list[int]) -> list[dict[str, Any]]:
        ordered_ends = sorted(set(candidate_ends))
        if end_i not in ordered_ends:
            ordered_ends.append(end_i)
        best: dict[int, tuple[float, int | None]] = {start_i: (0.0, None)}
        for cursor in range(start_i, end_i + 1):
            if cursor not in best:
                continue
            for candidate_end in ordered_ends:
                if candidate_end < cursor:
                    continue
                text = "".join(corrected_pieces[cursor : candidate_end + 1]).strip()
                measured = measure_text(text, context) if context is not None else 0.0
                if hard_width and measured > hard_width + 0.5:
                    break
                next_cursor = candidate_end + 1
                ratio = measured / max(1.0, comfort_width)
                pause = (
                    max(0.0, float(tokens[candidate_end + 1].get("start", 0)) - float(tokens[candidate_end].get("end", 0)))
                    if candidate_end + 1 < len(tokens) else 0.0
                )
                utterance_boundary = (
                    candidate_end + 1 < len(tokens)
                    and source_group(candidate_end) != source_group(candidate_end + 1)
                )
                left_tail = corrected_pieces[candidate_end].strip()[-1:]
                dependency_penalty = 0.9 if left_tail in {"的", "地", "得", "把", "将", "在", "让", "向", "从", "跟", "和", "与", "而", "因", "为", "可"} else 0.0
                short_segment_penalty = 4.0 if candidate_end - cursor + 1 <= 2 and candidate_end < end_i else 0.0
                signal_bonus = min(1.8, pause * 4.0) + (1.15 if utterance_boundary else 0.0)
                # Prefer the fewest physical splits. Pause/utterance signals
                # choose between equally short valid paths; they must not
                # create extra one- or two-word captions by themselves.
                cost = best[cursor][0] + (ratio - 0.82) ** 2 * 1.4 + 3.5 + dependency_penalty + short_segment_penalty - signal_bonus
                previous = best.get(next_cursor)
                if previous is None or cost < previous[0]:
                    best[next_cursor] = (cost, cursor)
        target = end_i + 1
        if target not in best:
            return []
        ranges: list[dict[str, Any]] = []
        cursor = target
        while cursor > start_i:
            previous_cursor = best[cursor][1]
            if previous_cursor is None:
                return []
            ranges.append({"start_i": previous_cursor, "end_i": cursor - 1, "confidence": 1.0})
            cursor = previous_cursor
        ranges.reverse()
        return ranges

    for model_caption_index, row in enumerate(captions_raw, start=1):
        start_i, end_i = row["start_i"], row["end_i"]
        model_text = "".join(corrected_pieces[start_i : end_i + 1]).strip()
        model_width = measure_text(model_text, context) if context is not None else 0.0
        if not hard_width or model_width <= hard_width + 0.5:
            effective_captions_raw.append(row)
            continue
        ai_candidate_ends = sorted(
            after_i
            for after_i in allowed_break_indices
            if start_i <= after_i < end_i and hard_boundary_allowed(after_i)
        )
        ranges = solve_split_path(start_i, end_i, [*ai_candidate_ends, end_i])
        repair_source = "deepseek_allowed_breaks"
        if not ranges:
            local_candidate_ends = [
                after_i
                for after_i in range(start_i, end_i)
                if locally_safe_boundary(after_i)
            ]
            ranges = solve_split_path(start_i, end_i, [*local_candidate_ends, end_i])
            repair_source = "validated_local_pixel_wrap"
        if not ranges:
            raise ValueError(
                f"captions[{model_caption_index}]={model_width:.1f}px>{hard_width:.1f}px，"
                "AI 与已验证本地像素边界均无法生成合法拆分路径"
            )
        for split_row in ranges:
            split_row["confidence"] = row["confidence"]
        effective_captions_raw.extend(ranges)
        repair = {
            "model_caption": model_caption_index,
            "source_indices": [start_i, end_i],
            "model_width_px": round(model_width, 1),
            "split_after_i": [item["end_i"] for item in ranges[:-1]],
            "source": repair_source,
        }
        if repair_source == "deepseek_allowed_breaks":
            ai_break_repairs.append(repair)
        else:
            validated_local_repairs.append(repair)

    captions: list[dict[str, Any]] = []
    semantic_breaks: list[dict[str, Any]] = []
    overflows: list[str] = []
    advisory_index_overruns: list[dict[str, int]] = []
    for item_index, row in enumerate(effective_captions_raw, start=1):
        start_i, end_i = row["start_i"], row["end_i"]
        hard_end_i = int(tokens[start_i].get("hard_end_i", last_index))
        if end_i > hard_end_i:
            advisory_index_overruns.append({"caption": item_index, "end_i": end_i, "hard_end_i": hard_end_i})
        if item_index < len(effective_captions_raw) and not hard_boundary_allowed(end_i):
            raise ValueError(f"captions[{item_index}] 使用了非法词中或承接字边界")
        display_text = "".join(corrected_pieces[start_i : end_i + 1]).strip()
        if not display_text:
            raise ValueError(f"captions[{item_index}] 本地重建后为空")
        measured = measure_text(display_text, context) if context is not None else 0.0
        if hard_width and measured > hard_width + 0.5:
            overflows.append(f"captions[{item_index}]={measured:.1f}px>{hard_width:.1f}px")
        span = tokens[start_i : end_i + 1]
        caption = {
            "token_ids": [str(token.get("id") or "") for token in span],
            "text": display_text,
            "display_text": display_text,
            "model_display_text": None,
            "source_indices": [start_i, end_i],
            "width_px": round(measured, 1) if context is not None else None,
        }
        captions.append(caption)
        if item_index < len(effective_captions_raw):
            semantic_breaks.append({"after_i": end_i, "confidence": 1.0, "reason": "DeepSeek 索引断句"})
    if overflows:
        raise ValueError("字幕真实像素超宽（请一次修正全部项目）：" + "；".join(overflows))

    validation = {
        "valid": True,
        "errors": [],
        "checked_tokens": len(tokens),
        "caption_coverage": 1.0,
        "caption_token_count": len(caption_indices),
        "deleted_token_count": len(deleted_indices),
        "pixel_overflows": 0,
        "advisory_index_overruns": advisory_index_overruns,
        "ai_break_repairs": ai_break_repairs,
        "validated_local_repairs": validated_local_repairs,
        "caption_text_source": "local_token_reconstruction",
        "layout_capacity": layout,
    }
    return {
        "corrections": corrections,
        "deletion_candidates": deletions,
        "delete_ranges": deletions,
        "repeat_candidates": [item for item in deletions if item["type"] in {"stutter", "false_start", "exact_repeat", "semantic_repeat"}],
        "captions": captions,
        "semantic_breaks": semantic_breaks,
        "final_sentences": captions,
        "break_hints": [],
        "allowed_breaks": [
            {"after_token_id": str(tokens[index].get("id") or ""), "after_i": index, "confidence": 1.0, "reason": "DeepSeek 允许的备用语义边界"}
            for index in sorted(allowed_break_indices)
        ],
        "forbidden_breaks": [],
        "protected_spans": [],
        "layout_capacity": layout,
        "layout_decision": {
            "status": "validated_local" if validated_local_repairs else "ai",
            "source": "holistic_index_decisions_with_validated_pixel_wrap" if validated_local_repairs else "holistic_index_decisions",
            "chunks": [{
                "status": "validated_local" if validated_local_repairs else "ai",
                "source": "holistic_index_decisions_with_validated_pixel_wrap" if validated_local_repairs else "holistic_index_decisions",
                "sentences": captions,
                "ai_break_repairs": ai_break_repairs,
                "validated_local_repairs": validated_local_repairs,
            }],
        },
        "validation": validation,
        "warnings": [],
    }


def _materialize_holistic_result_legacy(
    parsed: dict[str, Any],
    tokens: list[dict[str, Any]],
    layout: dict[str, Any],
    *,
    style: dict[str, Any] | None,
    width: int | None,
    height: int | None,
) -> dict[str, Any]:
    if not tokens:
        raise ValueError("完整分析没有输入 token")
    last_index = len(tokens) - 1

    def indexed_ranges(key: str, *, min_confidence: float = 0.0) -> list[dict[str, Any]]:
        values = parsed.get(key)
        if not isinstance(values, list):
            raise ValueError(f"{key} 必须是数组")
        rows: list[dict[str, Any]] = []
        for item_index, item in enumerate(values, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"{key}[{item_index}] 必须是对象")
            try:
                start_i, end_i = int(item.get("start_i")), int(item.get("end_i"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key}[{item_index}] 索引必须是整数") from exc
            if start_i < 0 or end_i < start_i or end_i > last_index:
                raise ValueError(f"{key}[{item_index}] 索引越界或顺序错误")
            confidence = float(item.get("confidence", 1.0 if key == "captions" else 0.0))
            if not 0 <= confidence <= 1 or confidence < min_confidence:
                raise ValueError(f"{key}[{item_index}] 置信度低于 {min_confidence:.2f}")
            rows.append({**item, "start_i": start_i, "end_i": end_i, "confidence": confidence})
        return rows

    corrections_raw = indexed_ranges("corrections", min_confidence=0.92)
    deletions_raw = indexed_ranges("deletions")
    def hard_boundary_allowed(after_i: int) -> bool:
        if after_i < 0 or after_i >= last_index:
            return after_i == last_index
        left = tokens[after_i]
        right = tokens[after_i + 1]
        right_text = str(right.get("text") or "").strip()
        if right_text[:1] in {"的", "了", "系", "品", "角"}:
            return False
        left_id = str(left.get("id") or "")
        right_id = str(right.get("id") or "")
        if re.sub(r"-c\d+$", "", left_id) == re.sub(r"-c\d+$", "", right_id) and "-c" in left_id:
            return False
        left_text = str(left.get("text") or "").strip()
        if left_text[-1:].isascii() and right_text[:1].isascii() and left_text[-1:].isalnum() and right_text[:1].isalnum():
            return False
        return True
    for key, rows in (("corrections", corrections_raw), ("deletions", deletions_raw)):
        occupied: set[int] = set()
        for row in rows:
            indexes = set(range(row["start_i"], row["end_i"] + 1))
            if occupied & indexes:
                raise ValueError(f"{key} 范围重叠")
            occupied.update(indexes)
    corrected_pieces = [str(token.get("text") or "") for token in tokens]
    corrections: list[dict[str, Any]] = []
    for row in corrections_raw:
        replacement = str(row.get("replacement") or "").strip()
        if not replacement:
            raise ValueError("corrections replacement 不能为空")
        corrected_pieces[row["start_i"]] = replacement
        for index in range(row["start_i"] + 1, row["end_i"] + 1):
            corrected_pieces[index] = ""
        span = tokens[row["start_i"] : row["end_i"] + 1]
        corrections.append({
            "token_ids": [str(token.get("id") or "") for token in span],
            "replacement": replacement,
            "confidence": row["confidence"],
            "reason": str(row.get("reason") or "")[:300],
            "source_indices": [row["start_i"], row["end_i"]],
        })
    allowed_delete_types = {"stutter", "false_start", "exact_repeat", "semantic_repeat", "filler", "redundant"}
    deletions: list[dict[str, Any]] = []
    for row in deletions_raw:
        span = tokens[row["start_i"] : row["end_i"] + 1]
        kind = str(row.get("type") or "redundant")
        if kind not in allowed_delete_types:
            raise ValueError(f"不支持的删除类型：{kind}")
        deletions.append({
            "token_ids": [str(token.get("id") or "") for token in span],
            "type": kind,
            "confidence": row["confidence"],
            "reason": str(row.get("reason") or "")[:300],
            "source_indices": [row["start_i"], row["end_i"]],
        })
    segmented_text = str(parsed.get("segmented_text") or "").strip()
    if not segmented_text:
        raise ValueError("segmented_text 不能为空")
    parts = re.split(r"[｜|]", segmented_text)
    if not parts or any(not part for part in parts):
        raise ValueError("segmented_text 包含空字幕段")
    def normalize_segment_text(value: Any) -> str:
        return "".join(
            char for char in str(value or "").casefold()
            if not (char.isspace() or unicodedata.category(char).startswith(("P", "Z")))
        )
    normalized_parts = [normalize_segment_text(part) for part in parts]
    full_normalized_pieces = [normalize_segment_text(piece) for piece in corrected_pieces]
    deleted_indices = {
        index
        for row in deletions_raw
        for index in range(row["start_i"], row["end_i"] + 1)
    }
    post_delete_pieces = ["" if index in deleted_indices else piece for index, piece in enumerate(corrected_pieces)]
    post_delete_normalized_pieces = [normalize_segment_text(piece) for piece in post_delete_pieces]
    joined_model_text = "".join(normalized_parts)
    if joined_model_text == "".join(full_normalized_pieces):
        alignment_pieces = list(corrected_pieces)
        normalized_pieces = full_normalized_pieces
        captions_exclude_deletions = False
    elif joined_model_text == "".join(post_delete_normalized_pieces):
        alignment_pieces = post_delete_pieces
        normalized_pieces = post_delete_normalized_pieces
        captions_exclude_deletions = True
    else:
        full_normalized_text = "".join(full_normalized_pieces)
        char_to_token = [
            token_index
            for token_index, piece in enumerate(full_normalized_pieces)
            for _ in piece
        ]
        matcher = difflib.SequenceMatcher(a=full_normalized_text, b=joined_model_text, autojunk=False)
        diff_summary = []
        safe_diff = True
        reconciled_rows: list[dict[str, Any]] = []
        for tag, a0, a1, b0, b1 in matcher.get_opcodes():
            if tag == "equal":
                continue
            diff_summary.append(f"{tag}:原文[{a0}:{a1}]={full_normalized_text[a0:a1]} 模型[{b0}:{b1}]={joined_model_text[b0:b1]}")
            if tag != "delete" or b0 != b1 or a1 <= a0:
                safe_diff = False
                break
            omitted = full_normalized_text[a0:a1]
            chosen_start = a0
            if chosen_start >= len(char_to_token) or chosen_start + len(omitted) - 1 >= len(char_to_token):
                safe_diff = False
                break
            start_i = char_to_token[chosen_start]
            end_i = char_to_token[chosen_start + len(omitted) - 1]
            declared = next(
                (
                    row for row in deletions_raw
                    if start_i >= row["start_i"] and end_i <= row["end_i"]
                ),
                None,
            )
            if declared is not None:
                reconciled_rows.append({
                    **declared,
                    "start_i": start_i,
                    "end_i": end_i,
                    "reason": str(declared.get("reason") or "") + "；按 segmented_text 收缩实际删除范围",
                })
                continue
            repeat_start = chosen_start
            if a0 >= len(omitted) and full_normalized_text[a0 - len(omitted) : a0] == omitted:
                repeat_start = a0 - len(omitted)
            elif full_normalized_text[a1 : a1 + len(omitted)] != omitted:
                safe_diff = False
                break
            repeat_start_i = char_to_token[repeat_start]
            repeat_end_i = char_to_token[repeat_start + len(omitted) - 1]
            reconciled_rows.append({
                "start_i": repeat_start_i,
                "end_i": repeat_end_i,
                "type": "exact_repeat",
                "confidence": 0.99,
                "reason": f"DeepSeek segmented_text 合并了相邻精确重复“{omitted}”",
                "source": "segmented_text_implicit_repeat",
            })
        if safe_diff and reconciled_rows:
            deletions_raw = reconciled_rows
            deletions = []
            for row in reconciled_rows:
                span = tokens[row["start_i"] : row["end_i"] + 1]
                deletions.append({
                    "token_ids": [str(token.get("id") or "") for token in span],
                    "type": str(row.get("type") or "redundant"),
                    "confidence": float(row.get("confidence", 0)),
                    "reason": str(row.get("reason") or "")[:300],
                    "source_indices": [row["start_i"], row["end_i"]],
                    "source": str(row.get("source") or "segmented_text_reconciled"),
                })
            deleted_indices = {
                index
                for row in reconciled_rows
                for index in range(row["start_i"], row["end_i"] + 1)
            }
            post_delete_pieces = ["" if index in deleted_indices else piece for index, piece in enumerate(corrected_pieces)]
            post_delete_normalized_pieces = [normalize_segment_text(piece) for piece in post_delete_pieces]
        if safe_diff and joined_model_text == "".join(post_delete_normalized_pieces):
            alignment_pieces = post_delete_pieces
            normalized_pieces = post_delete_normalized_pieces
            captions_exclude_deletions = True
        else:
            raise ValueError(
                "segmented_text 禁止增删词：规范化后长度 "
                f"{len(joined_model_text)}，完整原文 {len(full_normalized_text)}，"
                f"删除后原文 {len(''.join(post_delete_normalized_pieces))}；差异 {'；'.join(diff_summary[:4])}"
            )
    captions_raw: list[dict[str, Any]] = []
    token_cursor = 0
    for part_index, (part, normalized_part) in enumerate(zip(parts, normalized_parts), start=1):
        start_i = token_cursor
        materialized = ""
        while token_cursor < len(tokens) and len(materialized) < len(normalized_part):
            materialized += normalized_pieces[token_cursor]
            token_cursor += 1
        if materialized != normalized_part:
            raise ValueError(f"segmented_text 第 {part_index} 段断在 token 内部或文字不一致")
        while token_cursor < len(tokens) and normalized_pieces[token_cursor] == "":
            token_cursor += 1
        end_i = token_cursor - 1
        if end_i < start_i:
            raise ValueError(f"segmented_text 第 {part_index} 段没有 token")
        if part_index < len(parts) and not hard_boundary_allowed(end_i):
            raise ValueError(f"segmented_text 第 {part_index} 段使用了非法词中或承接字边界")
        canonical_part = "".join(alignment_pieces[start_i : end_i + 1])
        captions_raw.append({"start_i": start_i, "end_i": end_i, "display_text": canonical_part, "confidence": 1.0})
    if token_cursor != len(tokens):
        raise ValueError("segmented_text 没有覆盖全部 token")
    semantic_breaks = [
        {"after_i": row["end_i"], "confidence": 1.0, "reason": "DeepSeek 完整文本断句"}
        for row in captions_raw[:-1]
    ]
    expected_cursor = 0
    captions: list[dict[str, Any]] = []
    context = build_layout_context(style, int(width), int(height)) if style is not None and width and height else None
    overflows: list[str] = []
    normalized_display_texts = 0
    for caption_index, row in enumerate(captions_raw, start=1):
        if row["start_i"] != expected_cursor:
            raise ValueError(f"captions[{caption_index}] 没有从索引 {expected_cursor} 无缝开始")
        expected_cursor = row["end_i"] + 1
        expected_text = "".join(alignment_pieces[row["start_i"] : row["end_i"] + 1])
        display_text = str(row.get("display_text") or "").strip()
        normalize = lambda value: re.sub(r"[\s，。！？!?；;：:,]", "", str(value or "")).casefold()
        model_display_text = display_text
        if not display_text:
            display_text = expected_text
        elif normalize(display_text) != normalize(expected_text):
            # Indices are the source of truth. Never allow model-added or
            # model-omitted words into the rendered subtitle text.
            normalized_display_texts += 1
            display_text = expected_text
        measured = measure_text(display_text, context) if context is not None else 0.0
        if context is not None and measured > context.hard_width + 0.5:
            overflows.append(f"captions[{caption_index}] {measured:.1f}px>{context.hard_width:.1f}px 文本={display_text}")
        span = tokens[row["start_i"] : row["end_i"] + 1]
        captions.append({
            "token_ids": [str(token.get("id") or "") for token in span],
            "text": display_text,
            "display_text": display_text,
            "model_display_text": model_display_text if model_display_text != display_text else None,
            "source_indices": [row["start_i"], row["end_i"]],
            "width_px": round(measured, 1) if context is not None else None,
        })
    if expected_cursor != len(tokens):
        raise ValueError(f"captions 必须覆盖到最后一个索引 {last_index}")
    if overflows:
        raise ValueError("字幕真实像素超宽：" + "；".join(overflows[:8]))
    validation = {
        "valid": True,
        "errors": [],
        "checked_tokens": len(tokens),
        "caption_coverage": 1.0,
        "pixel_overflows": 0,
        "normalized_display_texts": normalized_display_texts,
        "captions_exclude_deletions": captions_exclude_deletions,
        "layout_capacity": layout,
    }
    return {
        "corrections": corrections,
        "deletion_candidates": deletions,
        "delete_ranges": deletions,
        "repeat_candidates": [item for item in deletions if item["type"] in {"stutter", "false_start", "exact_repeat", "semantic_repeat"}],
        "captions": captions,
        "semantic_breaks": semantic_breaks,
        "segmented_text": segmented_text,
        "final_sentences": captions,
        "break_hints": [],
        "allowed_breaks": [],
        "forbidden_breaks": [],
        "protected_spans": [],
        "layout_capacity": layout,
        "layout_decision": {"status": "ai", "source": "holistic_transcript", "chunks": [{"status": "ai", "source": "holistic_transcript", "sentences": captions}]},
        "validation": validation,
        "warnings": ([f"本地按 token 重建了 {normalized_display_texts} 条 display_text"] if normalized_display_texts else []),
    }


def _caption_ends_from_semantic_breaks(
    tokens: list[dict[str, Any]],
    corrected_pieces: list[str],
    semantic_breaks: list[dict[str, Any]],
    context: Any,
) -> list[int]:
    """Choose only among boundaries explicitly approved by the holistic model."""
    count = len(tokens)
    confidence_by_end = {
        int(item["after_i"]): float(item.get("confidence", 0))
        for item in semantic_breaks
    }
    for index, token in enumerate(tokens[:-1]):
        if token.get("utterance_break_after"):
            confidence_by_end[index] = max(confidence_by_end.get(index, 0), 0.82)
    confidence_by_end[count - 1] = 1.0
    candidates = sorted(confidence_by_end)
    best = [math.inf] * (count + 1)
    previous = [-1] * (count + 1)
    best[0] = 0.0
    for start in range(count):
        if math.isinf(best[start]):
            continue
        for end in candidates:
            if end < start:
                continue
            text = "".join(corrected_pieces[start : end + 1])
            measured = measure_text(text, context)
            if measured > context.hard_width + 0.5:
                break
            ratio = measured / max(1.0, context.comfort_width)
            length = end - start + 1
            cost = (ratio - 0.82) ** 2 * 8 + (1.0 - confidence_by_end[end]) * 3
            if length <= 2 and end != count - 1:
                cost += 6
            next_position = end + 1
            if best[start] + cost < best[next_position]:
                best[next_position] = best[start] + cost
                previous[next_position] = start
    if previous[count] < 0:
        ordered = [-1, *candidates]
        gaps: list[str] = []
        for left, right in zip(ordered, ordered[1:]):
            start = left + 1
            text = "".join(corrected_pieces[start : right + 1])
            measured = measure_text(text, context)
            if measured > context.hard_width + 0.5:
                gaps.append(f"{start}-{right}:{measured:.1f}px>{context.hard_width:.1f}px")
        detail = "，".join(gaps[:8]) or "无可达路径"
        raise ValueError("DeepSeek semantic_breaks 不足，缺少安全语义边界：" + detail)
    ends: list[int] = []
    cursor = count
    while cursor > 0:
        start = previous[cursor]
        if start < 0:
            raise ValueError("semantic_breaks 回溯失败")
        ends.append(cursor - 1)
        cursor = start
    ends.reverse()
    return ends


def decide_line_layout(
    tokens: list[dict[str, Any]],
    constraints: dict[str, Any],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float = 60.0,
    previous: dict[str, Any] | None = None,
    validation_errors: list[str] | None = None,
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    if not base_url or not model or not api_key or not tokens:
        return {"status": "skipped", "reason": "not_configured", "sentences": []}
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    user_payload: dict[str, Any] = {"tokens": tokens, "constraints": constraints}
    if previous is not None:
        user_payload["previous_output"] = previous
        user_payload["validation_errors"] = validation_errors or []
        user_payload["instruction"] = "上一次结果未通过系统校验，请逐项修正后返回完整 JSON。"
    cache_key = _cache_key("layout-semantic-v2", model, user_payload)
    cached = _read_cache(cache_dir, cache_key)
    if cached is not None:
        result = {**cached, "cached": True, "usage": _empty_usage(), "latency_ms": 0, "attempt_count": 0, "retry_errors": []}
        result["decision_trace"] = _decision_trace("subtitle_layout", tokens, result, endpoint, model, constraints)
        return result
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": COMPACT_LAYOUT_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    last_error = ""
    retry_errors: list[str] = []
    for _ in range(1):
        started = time.perf_counter()
        try:
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            parsed = _parse_json_content(body["choices"][0]["message"]["content"])
            usage = _normalize_usage(body.get("usage"))
            trace_base = {
                "raw_response": body["choices"][0]["message"]["content"],
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "attempt_count": 1,
                "retry_errors": retry_errors,
            }
            option_ids = parsed.get("option_ids")
            if isinstance(option_ids, list):
                result = {"status": "ok", "option_ids": [str(option_id) for option_id in option_ids if str(option_id)], "usage": usage, **trace_base}
                result["decision_trace"] = _decision_trace("subtitle_layout", tokens, result, endpoint, model, constraints)
                _write_cache(cache_dir, cache_key, result)
                return result
            line_ends = parsed.get("line_ends")
            if isinstance(line_ends, list):
                result = {
                    "status": "ok",
                    "line_ends": [int(value) for value in line_ends],
                    "usage": usage,
                    **trace_base,
                }
                result["decision_trace"] = _decision_trace("subtitle_layout", tokens, result, endpoint, model, constraints)
                _write_cache(cache_dir, cache_key, result)
                return result
            sentences = _clean_final_sentences(parsed.get("sentences"))
            result = {"status": "ok", "sentences": sentences, "usage": usage, **trace_base}
            result["decision_trace"] = _decision_trace("subtitle_layout", tokens, result, endpoint, model, constraints)
            _write_cache(cache_dir, cache_key, result)
            return result
        except (
            KeyError, IndexError, TypeError, ValueError, RuntimeError, json.JSONDecodeError,
            urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
        ) as exc:
            last_error = exc.__class__.__name__
            retry_errors.append(last_error)
    failed = {"status": "failed", "reason": last_error or "invalid_response", "sentences": []}
    failed["attempt_count"] = 1
    failed["retry_errors"] = retry_errors
    failed["decision_trace"] = _decision_trace("subtitle_layout", tokens, failed, endpoint, model, constraints)
    return failed


def review_line_layout(
    tokens: list[dict[str, Any]],
    sentences: list[dict[str, Any]],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float = 60.0,
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Independently reject physically valid but linguistically broken line layouts."""
    if not base_url or not model or not api_key or not tokens or not sentences:
        return {"status": "skipped", "approved": False, "issues": ["语义复核未配置"]}
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    review_input = {
        "source_text": "".join(str(token.get("text") or "") for token in tokens),
        "lines": [
            {"line": index, "text": str(item.get("text") or "")}
            for index, item in enumerate(sentences, start=1)
        ],
    }
    cache_key = _cache_key("layout-review-v1", model, review_input)
    cached = _read_cache(cache_dir, cache_key)
    if cached is not None:
        return {**cached, "cached": True, "usage": _empty_usage(), "latency_ms": 0}
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 384,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": LAYOUT_REVIEW_PROMPT},
            {"role": "user", "content": json.dumps(review_input, ensure_ascii=False)},
        ],
    }
    started = time.perf_counter()
    try:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        parsed = _parse_json_content(body["choices"][0]["message"]["content"])
        approved = parsed.get("approved") is True
        issues = [str(value) for value in parsed.get("issues", []) if str(value)] if isinstance(parsed.get("issues"), list) else []
        result = {
            "status": "ok",
            "approved": approved,
            "issues": issues if issues or approved else ["复核未给出拒绝原因"],
            "usage": _normalize_usage(body.get("usage")),
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "raw_response": body["choices"][0]["message"]["content"],
        }
        _write_cache(cache_dir, cache_key, result)
        return result
    except (
        KeyError, IndexError, TypeError, ValueError, RuntimeError, json.JSONDecodeError,
        urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
    ) as exc:
        return {"status": "failed", "approved": False, "issues": [exc.__class__.__name__], "usage": _empty_usage()}


def _analysis_chunks(
    tokens: list[dict[str, Any]], limit: int = 80, overlap: int = 24
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    start = 0
    while start < len(tokens):
        hard_end = min(len(tokens), start + limit)
        end = hard_end
        if hard_end < len(tokens):
            search_start = min(hard_end - 1, start + max(1, limit // 2))
            candidates = [index for index in range(search_start, hard_end) if float(tokens[index].get("pause_after", 0)) >= 0.25]
            if candidates:
                end = candidates[-1] + 1
        chunks.append(tokens[start:end])
        if end >= len(tokens):
            break
        start = max(start + 1, end - max(0, min(overlap, limit // 2)))
    return chunks


def _run_analysis_stage(
    stage: str,
    tokens: list[dict[str, Any]],
    *,
    endpoint: str,
    model: str,
    api_key: str,
    timeout: float,
    cache_dir: str | Path | None,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    indexed_tokens = [{**token, "i": index} for index, token in enumerate(tokens)]
    stage_input: dict[str, Any] = {"tokens": indexed_tokens}
    if candidates is not None:
        stage_input["candidates"] = candidates
    cache_kind = "analysis-semantic-index-ranges-v1" if stage == "semantic_spans" else f"analysis-{stage}"
    cache_key = _cache_key(cache_kind, model, stage_input)
    cached = _read_cache(cache_dir, cache_key)
    if cached is not None:
        return {
            **cached,
            "cached": True,
            "usage": _empty_usage(),
            "latency_ms": 0,
            "attempt_count": 0,
            "retry_errors": [],
        }
    result = _request_analysis_stage(
        stage,
        stage_input,
        endpoint=endpoint,
        model=model,
        api_key=api_key,
        timeout=timeout,
    )
    if result.get("status") == "ok":
        _write_cache(cache_dir, cache_key, result)
    return result


def _request_analysis_stage(
    stage: str,
    stage_input: dict[str, Any],
    *,
    endpoint: str,
    model: str,
    api_key: str,
    timeout: float,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 6144 if stage == "semantic_spans" else 1536,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": STAGE_PROMPTS[stage]},
            {"role": "user", "content": json.dumps(stage_input, ensure_ascii=False)},
        ],
    }
    retry_errors: list[str] = []
    last_error = ""
    started = time.perf_counter()
    base_messages = list(payload["messages"])
    for attempt in range(1, 3):
        started = time.perf_counter()
        raw_response = None
        try:
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            raw_response = body["choices"][0]["message"]["content"]
            parsed = _parse_json_content(raw_response)
            result = _sanitize_stage_result(stage, parsed, stage_input)
            result.update(
                {
                    "status": "ok",
                    "usage": _normalize_usage(body.get("usage")),
                    "raw_response": raw_response,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "attempt_count": attempt,
                    "retry_errors": retry_errors,
                }
            )
            return result
        except (
            KeyError, IndexError, TypeError, ValueError, RuntimeError, json.JSONDecodeError,
            urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
        ) as exc:
            last_error = f"{exc.__class__.__name__}: {str(exc)[:160]}"
            retry_errors.append(last_error)
            if isinstance(exc, ValueError) and raw_response:
                payload["messages"] = [
                    *base_messages,
                    {"role": "assistant", "content": str(raw_response)},
                    {
                        "role": "user",
                        "content": "上次 JSON 未通过严格校验：" + str(exc)[:800]
                        + "。请只修正这些错误并重新返回完整 JSON；span 必须按 i 连续，不能跳 token。",
                    },
                ]
    return {
        "status": "failed",
        "reason": last_error or "invalid_response",
        "usage": _empty_usage(),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "attempt_count": 2,
        "retry_errors": retry_errors,
    }


def _sanitize_stage_result(
    stage: str, parsed: dict[str, Any], stage_input: dict[str, Any]
) -> dict[str, Any]:
    tokens = stage_input["tokens"]
    if stage == "deletion_verification":
        candidates = stage_input.get("candidates") or []
        raw_reviews = parsed.get("verified_deletions")
        if not isinstance(raw_reviews, list) or len(raw_reviews) != len(candidates):
            raise ValueError("independent review must cover every candidate exactly once")
        reviews: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in raw_reviews:
            if not isinstance(item, dict):
                raise ValueError("invalid review item")
            index = int(item.get("candidate_index", -1))
            if index < 0 or index >= len(candidates) or index in seen:
                raise ValueError("invalid or duplicate candidate_index")
            seen.add(index)
            confidence = float(item.get("confidence", 0))
            if not 0 <= confidence <= 1:
                raise ValueError("review confidence out of range")
            reviews.append(
                {
                    "candidate_index": index,
                    "approved": item.get("approved") is True,
                    "confidence": confidence,
                    "reason": str(item.get("reason") or ""),
                }
            )
        return {"verified_deletions": sorted(reviews, key=lambda item: item["candidate_index"])}
    if stage == "semantic_spans" and "sentence_ends" in parsed:
        parsed = _materialize_semantic_ranges(parsed, tokens)
    sanitized = _sanitize_analysis(parsed)
    keys = {
        "correction": ("corrections",),
        "deletion_candidates": ("delete_ranges", "repeat_candidates"),
        "semantic_spans": (
            "break_hints", "allowed_breaks", "forbidden_breaks", "protected_spans", "final_sentences"
        ),
    }[stage]
    result = {key: sanitized.get(key, []) for key in keys}
    if stage == "semantic_spans":
        token_text = {str(token.get("id") or ""): str(token.get("text") or "") for token in tokens}
        for key in ("forbidden_breaks", "protected_spans", "final_sentences"):
            for item in result.get(key, []):
                canonical = "".join(token_text.get(str(token_id), "") for token_id in item.get("token_ids", []))
                if canonical and canonical != str(item.get("text") or ""):
                    item["model_text"] = str(item.get("text") or "")
                    item["text"] = canonical
                    item["text_normalized_from_token_ids"] = True
    validation = _analysis_validation(tokens, result, require_coverage=stage == "semantic_spans")
    if not validation["valid"]:
        raise ValueError("; ".join(validation["errors"][:6]))
    if stage == "semantic_spans" and not result.get("final_sentences"):
        raise ValueError("semantic stage returned no final_sentences")
    result["validation"] = validation
    return result


def _materialize_semantic_ranges(parsed: dict[str, Any], tokens: list[dict[str, Any]]) -> dict[str, Any]:
    if not tokens:
        raise ValueError("semantic stage has no tokens")
    last_index = len(tokens) - 1
    raw_ends = parsed.get("sentence_ends")
    if not isinstance(raw_ends, list) or not raw_ends:
        raise ValueError("sentence_ends 必须是非空整数数组")
    try:
        ends = [int(value) for value in raw_ends]
    except (TypeError, ValueError) as exc:
        raise ValueError("sentence_ends 只能包含整数") from exc
    if ends != sorted(set(ends)) or ends[-1] != last_index or any(value < 0 or value > last_index for value in ends):
        raise ValueError(f"sentence_ends 必须严格递增且最后一个值为 {last_index}")

    def materialize_ranges(key: str) -> list[dict[str, Any]]:
        values = parsed.get(key) or []
        if not isinstance(values, list):
            raise ValueError(f"{key} 必须是数组")
        result: list[dict[str, Any]] = []
        for index, item in enumerate(values, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"{key}[{index}] 必须是对象")
            try:
                start_i = int(item.get("start_i"))
                end_i = int(item.get("end_i"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key}[{index}] start_i/end_i 必须是整数") from exc
            if start_i < 0 or end_i < start_i or end_i > last_index:
                raise ValueError(f"{key}[{index}] 索引越界或顺序错误")
            if end_i - start_i + 1 > 8:
                raise ValueError(f"{key}[{index}] 超过 8 个 token")
            span = tokens[start_i : end_i + 1]
            result.append(
                {
                    "token_ids": [str(token.get("id") or "") for token in span],
                    "text": "".join(str(token.get("text") or "") for token in span),
                    "confidence": float(item.get("confidence", 0)),
                    "reason": str(item.get("reason") or ""),
                    "source_indices": [start_i, end_i],
                }
            )
        return result

    final_sentences: list[dict[str, Any]] = []
    start = 0
    for end in ends:
        span = tokens[start : end + 1]
        final_sentences.append(
            {
                "token_ids": [str(token.get("id") or "") for token in span],
                "text": "".join(str(token.get("text") or "") for token in span),
                "source_indices": [start, end],
            }
        )
        start = end + 1
    return {
        "forbidden_breaks": materialize_ranges("forbidden_ranges"),
        "protected_spans": materialize_ranges("protected_ranges"),
        "final_sentences": final_sentences,
    }


def _combine_stage_results(stage_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    candidates = [dict(item) for item in stage_results["deletion_candidates"].get("delete_ranges", [])]
    reviews = {
        int(item["candidate_index"]): item
        for item in stage_results["deletion_verification"].get("verified_deletions", [])
    }
    verified: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        review = reviews.get(index, {})
        candidate["independent_review"] = review
        if (
            float(candidate.get("confidence", 0)) >= 0.75
            and review.get("approved") is True
            and float(review.get("confidence", 0)) >= 0.90
        ):
            verified.append(candidate)
    semantic = stage_results["semantic_spans"]
    return {
        "status": "ok",
        "corrections": stage_results["correction"].get("corrections", []),
        "deletion_candidates": candidates,
        "delete_ranges": verified,
        "repeat_candidates": stage_results["deletion_candidates"].get("repeat_candidates", []),
        "break_hints": semantic.get("break_hints", []),
        "allowed_breaks": semantic.get("allowed_breaks", []),
        "forbidden_breaks": semantic.get("forbidden_breaks", []),
        "protected_spans": semantic.get("protected_spans", []),
        "final_sentences": semantic.get("final_sentences", []),
        "usage": _sum_usage(stage_results.values()),
    }


def _request_analysis(
    tokens: list[dict[str, Any]], *, endpoint: str, model: str, api_key: str, timeout: float
) -> dict[str, Any]:
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"tokens": tokens}, ensure_ascii=False)},
        ],
    }
    last_error = ""
    retry_errors: list[str] = []
    for attempt in range(1, 3):
        started = time.perf_counter()
        try:
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            result = _sanitize_analysis(_parse_json_content(body["choices"][0]["message"]["content"]))
            validation = _analysis_validation(tokens, result, require_coverage=bool(result.get("final_sentences")))
            if not validation["valid"]:
                raise ValueError("; ".join(validation["errors"][:6]))
            result["validation"] = validation
            result["usage"] = _normalize_usage(body.get("usage"))
            result["raw_response"] = body["choices"][0]["message"]["content"]
            result["latency_ms"] = round((time.perf_counter() - started) * 1000)
            result["attempt_count"] = attempt
            result["retry_errors"] = retry_errors
            return result
        except (
            KeyError, IndexError, TypeError, ValueError, RuntimeError, json.JSONDecodeError,
            urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
        ) as exc:
            last_error = exc.__class__.__name__
            retry_errors.append(last_error)
    failed = _empty_analysis(last_error or "invalid_response")
    failed["latency_ms"] = round((time.perf_counter() - started) * 1000)
    failed["attempt_count"] = 2
    failed["retry_errors"] = retry_errors
    return failed


def _decision_trace(
    task_type: str,
    tokens: list[dict[str, Any]],
    result: dict[str, Any],
    endpoint: str,
    model: str,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token_ids = [str(token.get("id") or "") for token in tokens if token.get("id")]
    input_text = "".join(str(token.get("text") or "") for token in tokens)
    decision = {
        key: result.get(key)
        for key in (*_analysis_list_keys(), "option_ids", "sentences")
        if key in result
    }
    return {
        "task_type": task_type,
        "provider": urlsplit(endpoint).netloc,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "input": {
            "first_token_id": token_ids[0] if token_ids else None,
            "last_token_id": token_ids[-1] if token_ids else None,
            "token_count": len(token_ids),
            "text_sha256": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
        },
        "constraints": constraints or None,
        "raw_response": result.get("raw_response"),
        "decision": decision,
        "status": result.get("status"),
        "error_type": result.get("reason"),
        "attempt_count": int(result.get("attempt_count") or 0),
        "retry_errors": result.get("retry_errors") or [],
        "latency_ms": int(result.get("latency_ms") or 0),
        "usage": result.get("usage") or _empty_usage(),
        "cache_hit": bool(result.get("cached")),
    }


def _cache_key(kind: str, model: str, payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        {"version": PROMPT_VERSION, "kind": kind, "model": model, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _read_cache(cache_dir: str | Path | None, key: str) -> dict[str, Any] | None:
    if not cache_dir:
        return None
    path = Path(cache_dir) / f"{key}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("status") == "ok" else None


def _write_cache(cache_dir: str | Path | None, key: str, value: dict[str, Any]) -> None:
    if not cache_dir:
        return
    path = Path(cache_dir) / f"{key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return


def _empty_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _normalize_usage(value: Any) -> dict[str, int]:
    data = value if isinstance(value, dict) else {}
    return {
        "prompt_tokens": max(0, int(data.get("prompt_tokens") or 0)),
        "completion_tokens": max(0, int(data.get("completion_tokens") or 0)),
        "total_tokens": max(0, int(data.get("total_tokens") or 0)),
    }


def _sum_usage(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        key: sum(int(result.get("usage", {}).get(key, 0)) for result in results)
        for key in _empty_usage()
    }


def _analysis_list_keys() -> tuple[str, ...]:
    return (
        "corrections", "break_hints", "allowed_breaks", "forbidden_breaks", "protected_spans",
        "repeat_candidates", "deletion_candidates", "delete_ranges", "final_sentences",
    )


def _dedupe_analysis_items(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        signature = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if signature in seen:
            continue
        seen.add(signature)
        result.append(item)
    return result


def _merge_final_sentences(
    tokens: list[dict[str, Any]], sentences: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    token_ids = [str(token.get("id") or "") for token in tokens]
    positions = {token_id: index for index, token_id in enumerate(token_ids)}
    token_by_id = {str(token.get("id") or ""): token for token in tokens}
    candidates = []
    for sentence in sentences:
        ids = [str(value) for value in sentence.get("token_ids", []) if str(value) in positions]
        if ids:
            candidates.append((positions[ids[0]], ids))
    candidates.sort(key=lambda item: item[0])
    result: list[dict[str, Any]] = []
    cursor = 0
    for _, ids in candidates:
        remaining = [token_id for token_id in ids if positions[token_id] >= cursor]
        if not remaining or positions[remaining[0]] != cursor:
            continue
        expected = token_ids[cursor : cursor + len(remaining)]
        if remaining != expected:
            continue
        result.append(
            {
                "token_ids": remaining,
                "text": "".join(str(token_by_id[token_id].get("text") or "") for token_id in remaining),
            }
        )
        cursor += len(remaining)
        if cursor >= len(token_ids):
            break
    if cursor < len(token_ids):
        remaining = token_ids[cursor:]
        result.append(
            {
                "token_ids": remaining,
                "text": "".join(str(token_by_id[token_id].get("text") or "") for token_id in remaining),
                "source": "coverage_fallback",
            }
        )
    return result


def _analysis_validation(
    tokens: list[dict[str, Any]], analysis: dict[str, Any], *, require_coverage: bool
) -> dict[str, Any]:
    token_ids = [str(token.get("id") or "") for token in tokens]
    token_by_id = {str(token.get("id") or ""): token for token in tokens}
    positions = {token_id: index for index, token_id in enumerate(token_ids)}
    errors: list[str] = []

    def validate_span(item: dict[str, Any], label: str, compare_text: bool = False) -> list[str]:
        ids = [str(value) for value in item.get("token_ids", []) if str(value)]
        if not ids or any(token_id not in positions for token_id in ids):
            errors.append(f"{label} 包含不存在的 token")
            return []
        indexes = [positions[token_id] for token_id in ids]
        if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
            expected = token_ids[min(indexes) : max(indexes) + 1]
            missing = [token_id for token_id in expected if token_id not in ids]
            errors.append(
                f"{label} token 不连续，缺少中间 token: {','.join(missing[:8])}；"
                f"应使用连续序列 {','.join(expected[:12])} 或拆成多个连续 span"
            )
        if compare_text:
            joined = "".join(str(token_by_id[token_id].get("text") or "") for token_id in ids)
            normalize = lambda value: re.sub(r"[\s，。！？!?；;：:,]", "", str(value or "")).casefold()
            if normalize(joined) != normalize(item.get("text")):
                errors.append(f"{label} text 与 token_ids 不一致")
        return ids

    for key in ("corrections", "deletion_candidates", "delete_ranges", "repeat_candidates"):
        for index, item in enumerate(analysis.get(key, []), start=1):
            validate_span(item, f"{key}[{index}]")
    for key in ("forbidden_breaks", "protected_spans", "final_sentences"):
        for index, item in enumerate(analysis.get(key, []), start=1):
            ids = validate_span(item, f"{key}[{index}]", compare_text=True)
            if key != "final_sentences" and len(ids) > 8:
                errors.append(f"{key}[{index}] 超过 8 个 token，不能把完整句子标为不可拆词组")
    final_ids = [
        token_id
        for sentence in analysis.get("final_sentences", [])
        for token_id in sentence.get("token_ids", [])
    ]
    if require_coverage and final_ids != token_ids:
        errors.append("final_sentences 必须完整且按顺序覆盖输入 token")
    for index, sentence in enumerate(analysis.get("final_sentences", [])[:-1], start=1):
        ids = [str(value) for value in sentence.get("token_ids", []) if str(value) in positions]
        if not ids:
            continue
        next_position = positions[ids[-1]] + 1
        if next_position < len(tokens):
            next_text = str(tokens[next_position].get("text") or "").strip()
            if next_text[:1] in {"的", "了", "系", "品", "角"}:
                errors.append(f"final_sentences[{index}] 断点会让下一句以承接字“{next_text[:1]}”开头")
    return {"valid": not errors, "errors": errors, "checked_tokens": len(token_ids)}


def _inject_lexical_protection(tokens: list[dict[str, Any]], analysis: dict[str, Any]) -> None:
    pieces = [str(token.get("text") or "") for token in tokens]
    text = "".join(pieces)
    char_to_token = [index for index, piece in enumerate(pieces) for _ in piece]
    existing = {
        tuple(str(token_id) for token_id in item.get("token_ids", []))
        for item in analysis.get("forbidden_breaks", [])
    }
    for term in DEFAULT_PROTECTED_TERMS:
        start = 0
        while True:
            found = text.casefold().find(term.casefold(), start)
            if found < 0:
                break
            char_end = found + len(term) - 1
            if char_end >= len(char_to_token):
                break
            first_token = char_to_token[found]
            last_token = char_to_token[char_end]
            span_ids = [str(token.get("id") or "") for token in tokens[first_token : last_token + 1]]
            if span_ids and tuple(span_ids) not in existing:
                protected = {
                    "token_ids": span_ids,
                    "text": term,
                    "confidence": 1.0,
                    "reason": "本地业务词组保护",
                    "source": "lexicon",
                }
                analysis.setdefault("forbidden_breaks", []).append(protected)
                analysis.setdefault("protected_spans", []).append(dict(protected))
                existing.add(tuple(span_ids))
            start = found + len(term)


def apply_high_confidence_corrections(
    tokens: list[dict[str, Any]], analysis: dict[str, Any], threshold: float = 0.92
) -> list[dict[str, Any]]:
    token_by_id = {str(token.get("id")): token for token in tokens}
    for correction in analysis.get("corrections", []):
        if float(correction.get("confidence", 0)) < threshold:
            continue
        ids = [str(item) for item in correction.get("token_ids", []) if str(item) in token_by_id]
        replacement = str(correction.get("replacement") or "")
        if not ids or not replacement:
            continue
        first = token_by_id[ids[0]]
        first["original_text"] = str(first.get("original_text") or first.get("text") or "")
        first["text"] = replacement
        first["edited"] = True
        first["correction_reason"] = str(correction.get("reason") or "")
        first["correction_confidence"] = float(correction.get("confidence", 0))
        for token_id in ids[1:]:
            token_by_id[token_id]["text"] = ""
            token_by_id[token_id]["edited"] = True
    return tokens


def _parse_json_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("missing JSON object")
    parsed = loads_json(match.group(0), source="LLM analysis response")
    if not isinstance(parsed, dict):
        raise ValueError("LLM analysis response must be a JSON object")
    return parsed


def _sanitize_analysis(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "corrections": _clean_operations(data.get("corrections"), require_replacement=True),
        "break_hints": _clean_break_hints(data.get("break_hints")),
        "allowed_breaks": _clean_break_hints(data.get("allowed_breaks")),
        "forbidden_breaks": _clean_forbidden_breaks(data.get("forbidden_breaks")),
        "protected_spans": _clean_protected_spans(data.get("protected_spans")),
        "repeat_candidates": _clean_operations(data.get("repeat_candidates"), require_replacement=False),
        "delete_ranges": _clean_delete_ranges(data.get("delete_ranges")),
        "final_sentences": _clean_final_sentences(data.get("final_sentences")),
    }


def _clean_delete_ranges(value: Any) -> list[dict[str, Any]]:
    allowed = {"stutter", "false_start", "exact_repeat", "semantic_repeat", "filler", "redundant"}
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("token_ids"), list):
            continue
        token_ids = [str(token_id) for token_id in item["token_ids"] if str(token_id)]
        if not token_ids:
            continue
        kind = str(item.get("type") or "redundant")
        result.append(
            {
                "token_ids": token_ids,
                "type": kind if kind in allowed else "redundant",
                "confidence": max(0.0, min(1.0, float(item.get("confidence", 0)))),
                "reason": str(item.get("reason") or "")[:300],
            }
        )
    return result


def _clean_final_sentences(value: Any) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("token_ids"), list):
            continue
        token_ids = [str(token_id) for token_id in item["token_ids"] if str(token_id)]
        text = str(item.get("text") or "")[:1000]
        if token_ids and text:
            result.append({"token_ids": token_ids, "text": text})
    return result


def _clean_protected_spans(value: Any) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("token_ids"), list):
            continue
        token_ids = [str(token_id) for token_id in item["token_ids"] if str(token_id)]
        text = str(item.get("text") or "")[:300]
        if len(token_ids) < 2 or not text:
            continue
        result.append(
            {
                "token_ids": token_ids,
                "text": text,
                "confidence": max(0.0, min(1.0, float(item.get("confidence", 0)))),
                "reason": str(item.get("reason") or "")[:300],
            }
        )
    return result


def _clean_operations(value: Any, require_replacement: bool) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("token_ids"), list):
            continue
        cleaned = {
            "token_ids": [str(token_id) for token_id in item["token_ids"]],
            "confidence": max(0.0, min(1.0, float(item.get("confidence", 0)))),
            "reason": str(item.get("reason") or "")[:300],
        }
        if require_replacement:
            cleaned["replacement"] = str(item.get("replacement") or "")[:300]
            if not cleaned["replacement"]:
                continue
        result.append(cleaned)
    return result


def _clean_break_hints(value: Any) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not item.get("after_token_id"):
            continue
        result.append(
            {
                "after_token_id": str(item["after_token_id"]),
                "confidence": max(0.0, min(1.0, float(item.get("confidence", 0)))),
                "reason": str(item.get("reason") or "")[:300],
            }
        )
    return result


def _clean_forbidden_breaks(value: Any) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        reason = str(item.get("reason") or "")[:300]
        token_ids = [str(token_id) for token_id in item.get("token_ids", []) if str(token_id)]
        text = str(item.get("text") or "")[:300]
        if len(token_ids) >= 2 and text:
            result.append({"token_ids": token_ids, "text": text, "confidence": confidence, "reason": reason})
        elif item.get("after_token_id"):
            result.append(
                {
                    "after_token_id": str(item["after_token_id"]),
                    "confidence": confidence,
                    "reason": reason,
                }
            )
    return result


def _empty_analysis(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason, "corrections": [], "break_hints": [], "allowed_breaks": [], "forbidden_breaks": [], "protected_spans": [], "repeat_candidates": [], "delete_ranges": [], "final_sentences": []}
