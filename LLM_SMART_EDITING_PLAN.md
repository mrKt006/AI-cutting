# LLM Smart Editing Plan

## 1. 背景

当前 AI-cutting 已经具备：

- 静音/气口自动剪辑
- 火山引擎 ASR 字幕
- 本地 Whisper 备用字幕
- 外部 ASR JSON 接入
- 字幕烧录
- 封面生成
- 初步重复候选检测

但当前剪辑主要依赖机械规则：

```text
音量低于阈值
静音超过一定时长
剪辑点前后保留 padding
```

这种方式能处理明显停顿和气口，但对以下问题处理能力有限：

- 读错词
- 口误后重说
- 结巴
- 重复短句
- 废话和口头禅
- 上下文语义纠错
- 重点词句提取
- 字幕错别字纠正

后续目标是引入 LLM，但不是让 LLM 直接剪视频，而是让 LLM 基于逐词稿做结构化标注，再由程序按时间轴执行。

## 2. 总体原则

### 2.1 LLM 不直接剪视频

不建议让 LLM 输出：

```text
直接删除 12.31s 到 13.92s
```

原因：

- LLM 对毫秒级时间不稳定
- 直接剪辑风险高
- 难以回溯原因
- 难以调试误删

推荐让 LLM 输出结构化建议：

```json
{
  "type": "remove",
  "reason": "重复表达",
  "text": "然后然后",
  "evidence": ["连续重复两次"],
  "confidence": 0.86
}
```

再由程序结合 ASR 词级时间戳定位剪辑区间。

### 2.2 ASR 提供时间，LLM 提供语义

推荐职责划分：

```text
ASR:
  识别文本
  词级时间戳
  句级时间戳
  置信度

音频分析:
  静音
  停顿
  能量变化
  VAD

LLM:
  语义判断
  错词纠正建议
  重复/废话判断
  重点词句提取
  标注理由

剪辑程序:
  执行时间轴剪辑
  生成字幕
  生成报告
```

### 2.3 先建议，后执行

第一阶段只生成建议，不自动剪。

原因：

```text
自动剪语义内容误删风险高
```

推荐路径：

```text
阶段 1：生成 edit_suggestions.json
阶段 2：生成可读 review.html
阶段 3：用户确认后 apply
阶段 4：高置信规则可自动应用
```

## 3. 理想数据流

```text
原视频 video.mp4
  -> 提取音频 audio.wav
  -> ASR 获取逐词稿 transcript_words.json
  -> 音频分析获取 silence/vad/pause.json
  -> 聚合成 analysis_input.json
  -> LLM 生成 edit_suggestions.json
  -> 可选人工审核
  -> apply_suggestions 生成剪辑计划 edit_plan.json
  -> 执行剪辑 cut.mp4
  -> 对 cut.mp4 重新 ASR 或映射字幕
  -> 生成 final.mp4
```

## 4. 关键中间文件

### 4.1 transcript_words.json

逐词稿是后续智能剪辑的核心。

示例：

```json
{
  "segments": [
    {
      "start": 0.48,
      "end": 3.56,
      "text": "从去年的下半年到今天",
      "words": [
        { "text": "从", "start": 0.48, "end": 0.62, "confidence": 0.98 },
        { "text": "去年", "start": 0.62, "end": 0.94, "confidence": 0.97 }
      ]
    }
  ]
}
```

如果火山返回 `words` 字段，应优先保留。

### 4.2 pause_analysis.json

音频停顿分析。

示例：

```json
{
  "silences": [
    { "start": 12.40, "end": 13.05, "duration": 0.65 }
  ],
  "long_pauses": [
    { "start": 28.10, "end": 29.30, "duration": 1.20 }
  ]
}
```

### 4.3 edit_suggestions.json

LLM 输出建议。

示例：

```json
{
  "suggestions": [
    {
      "id": "s001",
      "type": "remove",
      "category": "repeat",
      "target_text": "然后然后",
      "start": 12.34,
      "end": 13.01,
      "confidence": 0.88,
      "reason": "连续重复，无新增语义",
      "risk": "low"
    },
    {
      "id": "s002",
      "type": "correct_subtitle",
      "from": "三内",
      "to": "三类",
      "confidence": 0.92,
      "reason": "上下文在讲分类，三类更符合语义",
      "risk": "low"
    },
    {
      "id": "s003",
      "type": "highlight",
      "target_text": "自己研究 Claude，研究 Codex",
      "confidence": 0.85,
      "reason": "工具名和方法论关键词",
      "risk": "none"
    }
  ]
}
```

### 4.4 edit_plan.json

程序把建议转换为真正剪辑计划。

示例：

```json
{
  "remove_segments": [
    {
      "start": 12.30,
      "end": 13.05,
      "source_suggestion": "s001"
    }
  ],
  "subtitle_corrections": [
    {
      "from": "三内",
      "to": "三类",
      "source_suggestion": "s002"
    }
  ],
  "highlights": [
    {
      "text": "Claude",
      "style": "yellow"
    },
    {
      "text": "Codex",
      "style": "yellow"
    }
  ]
}
```

## 5. LLM 可做的任务

### 5.1 错别字和错词纠正

输入：

- ASR 文本
- 上下文前后句
- 可选原稿

输出：

```text
疑似错词
建议替换
理由
置信度
```

例：

```text
三内 -> 三类
```

注意：

LLM 只能根据语义推断，不能真正“听见”发音。若要更准确，需要结合 ASR 置信度和原音回听。

### 5.2 重复表达检测

例：

```text
找我们的人，找我们的人，总共分为三类
```

可标注：

```text
第一个“找我们的人”可删除
```

### 5.3 卡壳和结巴检测

例：

```text
我我我觉得
这个这个方案
然后呃然后
```

可标注：

```text
删除重复字词
删除口头填充词
保留自然停顿
```

### 5.4 废话和口头禅检测

候选：

```text
呃
嗯
然后
就是
其实
怎么说呢
```

注意：

这些词不能一律删除，有些在句子里有语义功能。

### 5.5 重点词句提取

用于：

- 字幕高亮
- 封面标题候选
- 摘要
- 短视频标题

例：

```json
{
  "type": "highlight",
  "target_text": "Claude",
  "reason": "工具名"
}
```

## 6. LLM 输入设计

不要把完整 10 分钟逐字稿一次塞给模型。

推荐按窗口切分：

```text
每 60-90 秒一个 chunk
chunk 之间保留 10 秒 overlap
```

每个 chunk 输入：

```json
{
  "video_context": {
    "style": "中文竖屏口播",
    "goal": "删除明显卡壳、重复和废话，修正字幕错词，提取重点词"
  },
  "segments": [
    {
      "start": 0.48,
      "end": 3.56,
      "text": "从去年的下半年到今天",
      "words": []
    }
  ],
  "rules": {
    "do_not_remove": "有实质信息的内容",
    "prefer_low_risk": true,
    "output_json_only": true
  }
}
```

## 7. LLM 输出约束

必须强制 JSON schema。

字段建议：

```json
{
  "suggestions": [
    {
      "type": "remove | correct_subtitle | highlight | keep",
      "category": "repeat | filler | mistake | key_point | other",
      "start": 0,
      "end": 0,
      "target_text": "",
      "replacement": "",
      "confidence": 0.0,
      "risk": "low | medium | high",
      "reason": ""
    }
  ]
}
```

过滤规则：

```text
低置信度不自动执行
高风险不自动执行
没有时间戳不执行 remove
只允许 correction 影响字幕，不影响音频
```

## 8. 自动执行策略

### 8.1 第一阶段：只报告

命令示例：

```bash
python src/smart_review.py --video input/video.mp4 --asr input/asr_words.json --output output/edit_suggestions.json
```

输出：

```text
edit_suggestions.json
review.html
```

### 8.2 第二阶段：半自动执行

用户审核后执行：

```bash
python src/apply_suggestions.py --video input/video.mp4 --suggestions output/approved_suggestions.json
```

### 8.3 第三阶段：自动执行低风险项

规则：

```text
confidence >= 0.9
risk == low
category in ["repeat", "filler"]
duration <= 2.0s
```

## 9. 是否让 LLM 识别音波

短期不建议。

原因：

- 成本高
- 接口复杂
- 音频多模态模型对精确时间轴未必稳定
- 当前 ASR + VAD 已能提供足够多音频信息

推荐：

```text
音频分析/VAD 负责“哪里有声音、哪里停顿”
ASR 负责“说了什么、什么时候说”
LLM 负责“这句话该不该保留、字幕该不该修”
```

## 10. 与剪映能力的关系

剪映里的能力大概率来自：

```text
ASR 逐字时间戳
VAD 静音检测
置信度/错词检测
规则和模型结合
人工交互式确认
```

我们的路线也应类似，但先从可控的 JSON 建议开始。

## 11. 技术风险

### 11.1 误删

最大风险。

解决：

```text
默认只建议
保留原始报告
支持回看和人工确认
低风险才自动执行
```

### 11.2 ASR 词级时间戳不足

如果只有句级时间戳，精细删除会不准。

解决：

```text
尽量获取 words 字段
必要时使用二次 ASR 或 forced alignment
```

### 11.3 LLM 幻觉

LLM 可能纠错过度。

解决：

```text
必须引用原文片段
必须输出置信度
不能凭空新增内容
字幕纠错和音频剪辑分开
```

### 11.4 成本

ASR + LLM 都可能按量收费。

解决：

```text
先切 chunk
只把文本和时间戳给 LLM
不传视频
缓存 ASR 结果
缓存 LLM 建议
```

## 12. 迭代路线

### 阶段 A：增强 ASR 数据

- 保留火山 `words` 字段
- 输出 `transcript_words.json`
- 输出 `pause_analysis.json`

### 阶段 B：智能建议

- 新增 `smart_review.py`
- 调 LLM 生成 `edit_suggestions.json`
- 生成 `review.html`

### 阶段 C：人工确认

- Web 端展示建议
- 用户勾选同意/拒绝
- 输出 `approved_suggestions.json`

### 阶段 D：半自动执行

- 根据确认建议剪辑
- 修正字幕
- 高亮关键词

### 阶段 E：低风险自动化

- 自动删除明显重复
- 自动删除孤立口头禅
- 自动高亮关键词

## 13. 推荐下一步

当前还不需要马上进入这个阶段。

建议先完成：

```text
Web MVP
火山先剪后识别流程稳定
字幕时间准确
输出管理清晰
```

之后再做：

```text
保存火山 words 字段
生成 transcript_words.json
开发 smart_review.py
```

第一版智能剪辑不要自动剪，只生成建议报告。
