# 中文竖屏读稿视频自动剪辑工具 Spec

## 1. 项目目标

构建一个本地脚本工具，用于自动处理中文竖屏读稿视频。

用户提供一个竖屏口播视频、一份对应原稿和一个可选封面标题后，工具自动完成：

- 删除明显停顿、气口和静音片段
- 生成字幕时间轴
- 优先使用原稿作为字幕文本，减少 AI 识别错别字
- 套用固定字幕样式
- 从视频第一帧生成带大标题的封面
- 输出最终视频、封面图、字幕文件和剪辑报告

第一版目标是跑通稳定的本地自动化流程，不做网页上传界面。

## 2. 用户场景

用户的视频主要是中文竖屏读稿口播，内容结构简单，不需要复杂音效、转场、B-roll 或花字动画。

理想使用方式：

```text
把 video.mp4、script.txt、title.txt 放进 input 文件夹
运行一次脚本
在 output 文件夹得到成片、封面和字幕
```

## 3. 输入

```text
input/
  video.mp4
  script.txt
  title.txt 可选
```

### 3.1 video.mp4

- 竖屏视频
- 默认按 9:16 处理
- 第一版优先支持单人读稿口播

### 3.2 script.txt

- 视频对应原稿
- 字幕文本优先来自原稿，而不是完全使用 Whisper 识别结果
- 原稿与实际口播越接近，自动处理效果越稳定

### 3.3 title.txt

- 可选
- 用作封面大标题
- 如果不存在，则默认使用 `script.txt` 第一行作为标题

## 4. 输出

```text
output/
  final.mp4
  cover.jpg
  subtitle.ass
  subtitle.srt
  edit_report.json
```

### 4.1 final.mp4

最终成片，包含：

- 自动剪掉明显停顿后的画面和声音
- 烧录后的固定样式字幕
- 保持竖屏输出

### 4.2 cover.jpg

封面图：

- 默认截取视频第一帧
- 叠加大标题
- 标题可读，不超出画面

### 4.3 subtitle.ass

带样式字幕文件，用于最终烧录。

### 4.4 subtitle.srt

通用字幕文件，方便检查和后续复用。

### 4.5 edit_report.json

剪辑报告，记录：

- 原视频时长
- 输出视频时长
- 删除的时间段
- 保留的时间段
- 使用的关键参数

## 5. MVP 技术路线

第一版采用成熟开源项目和通用工具拼装，不从零开发视频剪辑底层。

| 能力 | 推荐方案 | 说明 |
|---|---|---|
| 自动删停顿/气口 | auto-editor / FFmpeg silence detect | 优先直接调用命令行 |
| ???? | ???? ASR | ??????????? |
| 字幕文本 | 原稿对齐 | 字幕文本优先来自 `script.txt` |
| 字幕格式 | SRT + ASS | SRT 便于检查，ASS 负责样式 |
| 字幕烧录 | FFmpeg + libass | 输出最终视频 |
| 封面生成 | FFmpeg + Pillow | 截第一帧并加标题 |
| 剪映兼容 | pyJianYingDraft | 后续可选，不纳入第一版 |

## 6. 借鉴项目

### 6.1 auto-editor

地址：https://github.com/WyattBlue/auto-editor

用途：

- 自动识别静音或低音量片段
- 自动剪掉长停顿
- 控制剪辑阈值和前后保留 padding

第一版建议直接调用 `auto-editor`，不修改其源码。

### 6.2 ???? ASR

???????????? ASR????????????

### 6.3 auto-subtitle

地址：https://github.com/m1guelpf/auto-subtitle

用途：

- 参考 Whisper + FFmpeg 自动字幕流程
- 不必直接作为主框架

### 6.4 pyJianYingDraft

地址：https://github.com/GuanYixuan/pyJianYingDraft

用途：

- 后续如果需要生成剪映草稿，可作为扩展
- 第一版不依赖剪映

## 7. 建议目录结构

```text
project/
  input/
    video.mp4
    script.txt
    title.txt

  output/
    final.mp4
    cover.jpg
    subtitle.ass
    subtitle.srt
    edit_report.json

  templates/
    subtitle_style.ass
    cover_style.json

  src/
    main.py
    cut_silence.py
    transcribe.py
    align_script.py
    make_subtitle.py
    render_video.py
    make_cover.py
```

## 8. 命令行接口

第一版命令：

```bash
python src/main.py --video input/video.mp4 --script input/script.txt --title input/title.txt
```

`--title` 可选。

如果没有传入 `--title`，程序尝试读取 `input/title.txt`。

如果 `input/title.txt` 也不存在，则使用 `script.txt` 第一行。

## 9. 自动剪辑规则

第一版只处理明显停顿、气口和静音，不强行做复杂语义纠错。

建议默认规则：

- 检测超过一定时长的低音量片段
- 删除明显长停顿
- 每个剪辑点前后保留少量缓冲，避免剪得太硬
- 输出剪辑报告，便于回看问题

初始参数建议：

```text
静音阈值：-30dB 左右起步
最短静音检测：0.35s 到 0.5s
剪辑点前后保留：0.08s 到 0.18s
```

实际参数需要用 3 条样片微调。

## 10. 字幕规则

### 10.1 基础样式

- 位置：底部居中
- 主字幕：白字 + 黑描边
- 关键词：黄色 + 黑描边
- 字号：按 1080x1920 约 48px 起步
- 一条字幕尽量单行显示
- 不在同一条字幕里强制换行

### 10.2 长句处理

当一句话过长时，优先拆成多条字幕，而不是在同一条字幕中换行。

目标：

```text
每条字幕约 8 到 12 个中文字
尽量保持语义完整
避免遮挡画面主体
```

### 10.3 关键词处理

第一版可以用简单标记语法指定关键词。

示例：

```text
今天讲 **自动剪辑** 怎么帮你省时间。
```

渲染时：

- 普通文字：白字黑描边
- `**自动剪辑**`：黄字黑描边

## 11. 封面规则

默认封面：

- 截取视频第一帧
- 叠加标题
- 输出 `cover.jpg`

标题来源优先级：

1. 命令行传入的 `--title`
2. `input/title.txt`
3. `script.txt` 第一行

封面样式第一版要求：

- 大字体
- 字体清晰
- 不超出画面边界
- 优先放在画面中上部或中部，避免遮挡底部字幕区域

## 12. 第一版不做

- 网页上传界面
- 自动挑最好看的封面帧
- 自动复杂语义纠错
- 自动添加 B-roll
- 自动添加音效
- 自动添加转场
- 多机位剪辑
- 多人对话识别
- 剪映草稿导出
- 复杂字幕动画

## 13. 验收标准

运行：

```bash
python src/main.py --video input/video.mp4 --script input/script.txt --title input/title.txt
```

必须生成：

```text
output/final.mp4
output/cover.jpg
output/subtitle.ass
output/subtitle.srt
output/edit_report.json
```

质量要求：

- 输出视频保持竖屏 9:16
- 明显长停顿删除率达到 80% 以上
- 音画同步，无明显漂移
- 字幕底部居中，白字黑描边
- 支持黄色关键词
- 字幕尽量单行显示
- 封面成功生成且标题可读
- 剪辑报告能说明主要剪辑区间
- 同一输入重复运行，不需要人工点按钮

## 14. 推荐测试样本

### 14.1 短样本

- 30 秒左右
- 有 2 到 3 个明显停顿
- 用来快速验证流程是否跑通

### 14.2 正常样本

- 2 到 5 分钟
- 按稿读
- 有少量重读和停顿
- 用来验证真实工作流

### 14.3 压力样本

- 8 到 15 分钟
- 有几处读错、重复、长停顿
- 用来判断剪辑报告和字幕同步是否稳定

## 15. 风险与盲区

- 自动剪停顿如果参数太激进，会让视频节奏过快、不自然
- auto-editor 主要基于音频响度，不能真正理解读错内容
- Whisper 中文断句和标点可能不符合原稿
- 原稿和实际口播差异越大，字幕对齐越难
- 第一帧可能闭眼或模糊，但第一版仍按指定规则使用第一帧
- 字幕字号需要根据实际视频分辨率微调
- 关键词高亮需要先定义简单、稳定的标记规则

## 16. 后续迭代方向

第一版跑通后，可以考虑：

- 做一个本地网页上传界面
- 自动批量处理多个视频
- 自动挑选前 3 秒中更清晰的封面帧
- 加入更强的原稿对齐和错读删除
- 提供参数预设，例如自然、紧凑、极紧凑
- 支持剪映草稿导出
- 支持字幕样式可视化配置
