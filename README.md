# AI-cutting MVP

AI-cutting 是一个本地 Web 视频口播剪辑工具：上传视频后，通过火山引擎 ASR 识别口播内容，自动生成字幕、封面和最终成片。

## 功能

- 上传一个或多个视频批量处理
- 使用火山引擎识别口播字幕
- 自动剪掉静音、气口和部分卡顿重复
- 生成 ASS/SRT 字幕文件
- 自定义字幕和封面标题样式预设
- 默认导出「标题-日期.mp4」和「标题-日期-封面.jpg」
- 可选额外导出字幕、ASR JSON、剪辑报告和 ZIP 汇总包

## 环境要求

- Python 3.10+
- FFmpeg / FFprobe，且 FFmpeg 需要支持 libass
- 火山引擎录音文件识别服务的 APP ID 和 Access Token

安装 Python 依赖：

```bash
pip install -r requirements.txt
```

Mac 可通过 Homebrew 安装 FFmpeg：

```bash
brew install ffmpeg
```

Windows 需要自行安装 FFmpeg，并确保 `ffmpeg`、`ffprobe` 可以在命令行里直接运行。

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
2. 回到首页，上传视频，填写标题。
3. 选择字幕样式预设，或进入样式预设页新建/编辑预设。
4. 按需勾选额外导出项，例如字幕文件、ASR JSON、剪辑报告。
5. 点击开始处理，等待任务完成。
6. 下载生成的视频、封面或 ZIP 汇总包。

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

Web 批量任务会额外生成：

```text
全部结果.zip
```

## 安全说明

- `web/settings.local.json` 保存本地火山配置，已被 `.gitignore` 排除，不应该提交到公开仓库。
- `jobs/`、`input/`、`output/`、视频文件和模型文件也默认不提交。
- 当前版本是本地单人 MVP；如果部署到公网，需要额外做账号隔离、密钥加密、上传限制和任务队列。
