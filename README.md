# AI-cutting MVP

AI-cutting 是一个本地 Web 口播视频剪辑工具。它支持上传一个或多个视频，调用火山引擎 ASR 识别口播内容，生成字幕、封面和成片，并提供一个精修台用于继续调整字幕、标题和时间线片段。

## 功能

- 单视频或多视频批量处理
- 火山引擎录音文件识别 ASR
- 自动生成字幕、封面和成片
- 自定义字幕和封面标题样式预设
- 默认导出成片和封面
- 可选额外导出字幕、ASR JSON、剪辑报告和 ZIP 汇总包
- 精修台支持字幕修改、片段隐藏/删除、轨道移动、切割、标题轨和导出精修版

## 环境要求

- Python 3.10+
- FFmpeg / FFprobe，并确保 FFmpeg 支持 libass
- 火山引擎录音文件识别服务的 APP ID 和 Access Token

安装 Python 依赖：

```bash
pip install -r requirements.txt
```

Mac 可以通过 Homebrew 安装 FFmpeg：

```bash
brew install ffmpeg
```

Windows 需要自行安装 FFmpeg，并确保 `ffmpeg`、`ffprobe` 可以在命令行直接运行。

## 启动 Web

Windows 可以双击：

```text
start_web.bat
```

也可以手动启动：

```bash
python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

然后打开：

```text
http://127.0.0.1:8000/
```

## 使用流程

1. 进入设置页，填写火山引擎 APP ID 和 Access Token。
2. 回到首页，上传视频，填写封面标题。
3. 选择字幕样式预设，或进入样式预设页新建、编辑预设。
4. 按需勾选额外导出项，例如字幕文件、ASR JSON、剪辑报告。
5. 点击开始处理，等待任务完成。
6. 下载生成的视频、封面或 ZIP 汇总包。
7. 如果需要手动微调，进入任务详情页的精修台，修改字幕、片段、标题轨后导出精修版。

## CLI 用法

最简命令：

```bash
python src/main.py --video input/video.mp4 --title input/title.txt --subtitle-source volcengine
```

指定输出名称和剪辑参数：

```bash
python src/main.py --video input/video.mp4 --title input/title.txt --output-basename "示例-20260707" --noise=-26dB --min-silence 0.30 --padding 0.08 --subtitle-source volcengine
```

额外导出字幕和报告：

```bash
python src/main.py --video input/video.mp4 --title input/title.txt --subtitle-source volcengine --export-subtitles --export-asr-json --export-report
```

可用环境变量提供火山配置：

```bash
export VOLC_APP_ID="你的 APP ID"
export VOLC_ACCESS_TOKEN="你的 Access Token"
```

PowerShell：

```powershell
$env:VOLC_APP_ID="你的 APP ID"
$env:VOLC_ACCESS_TOKEN="你的 Access Token"
```

## 输出文件

默认输出：

```text
标题-日期.mp4
标题-日期-封面.jpg
```

可选输出：

```text
subtitle.ass
subtitle.srt
volcengine_segments.json
edit_report.json
```

Web 批量任务可额外生成：

```text
全部结果.zip
```

精修台导出会生成：

```text
精修视频
精修封面
精修字幕
精修导出计划
```

## 回归检查

一键运行当前主要检查：

```bash
python scripts/check_all.py
```

它会依次检查：

- Python 编译
- 文本编码
- 精修时间线计划
- Web 错误边界
- 精修页 JS 语法
- Impeccable UI 检测
- Git 空白字符检查

也可以单独运行：

```bash
python scripts/check_encoding.py
python scripts/check_editor_timeline.py
python scripts/check_web_error_boundaries.py
python scripts/check_edit_page_js.py
```

## 安全说明

- `web/settings.local.json` 保存本地火山配置，已被 `.gitignore` 排除，不应该提交到公开仓库。
- `jobs/`、`input/`、`output/`、视频文件和模型文件默认不提交。
- 当前版本仍是本地单人 MVP。如果部署到公网，需要额外做账号隔离、密钥加密、上传限制和任务队列。
