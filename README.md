# AI-cutting MVP

AI-cutting 是一个本地 Web 口播视频剪辑工具。它支持上传一个或多个视频，调用火山引擎 ASR 识别口播内容，生成字幕、封面和成片，并提供一个精修台用于继续调整字幕、标题和时间线片段。

## 功能

- 单视频或多视频批量处理
- 火山引擎录音文件识别 ASR
- 可选 `.md/.txt` 逐字稿纠正专有名词和识别文字
- 可选 DeepSeek/OpenAI-compatible 语义纠错、断句与自动口误删除
- 自动生成字幕、封面和成片
- 内容标题、封面标题和字幕样式分别配置
- 自定义字幕和封面标题样式预设
- 默认导出成片和封面
- 可选额外导出字幕、ASR JSON、剪辑报告和 ZIP 汇总包
- 精修台支持字幕修改、片段隐藏/删除、轨道移动、切割、标题轨和导出精修版
- 任务支持安全暂停、继续、取消和服务重启恢复

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

脚本会优先使用 `http://127.0.0.1:8000/`，如果端口被占用，会自动改用 `http://127.0.0.1:8001/`。

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
2. 回到首页，上传视频，分别填写内容标题和封面标题。
3. 有逐字稿时，为每条视频上传对应的 UTF-8 `.md` 或 `.txt` 文件。
4. 选择字幕样式预设，或进入样式预设页新建、编辑预设。
5. 按需勾选额外导出项，例如字幕文件、ASR JSON、剪辑报告。
6. 点击开始处理，等待任务完成；任务页可安全暂停、继续或取消。
7. 下载生成的视频、封面或 ZIP 汇总包。
8. 如果需要手动微调，进入任务详情页的精修台，修改字幕、片段和标题后导出精修版。

逐字稿格式参见 [docs/TRANSCRIPT_FORMAT.md](docs/TRANSCRIPT_FORMAT.md)，常见错误参见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

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
- 跟踪文件密钥扫描
- 精修时间线计划
- 逐字稿解析与 ASR 对齐
- 暂停检查点与完整视频恢复
- 字幕智能和质量评估报告
- Web 错误边界
- 精修页 JS 语法
- Impeccable UI 检测
- Git 空白字符检查

也可以单独运行：

```bash
python scripts/check_encoding.py
python scripts/check_pipeline_resume.py
python scripts/build_evaluation_report.py
python scripts/check_editor_timeline.py
python scripts/check_web_error_boundaries.py
python scripts/check_edit_page_js.py
```

## 安全说明

- `web/settings.local.json` 保存本地火山配置，已被 `.gitignore` 排除，不应该提交到公开仓库。
- `jobs/`、`input/`、`output/`、视频文件和模型文件默认不提交。
- 当前版本仍是本地单人 MVP，默认拒绝非本机访问。`AI_CUTTING_ALLOW_UNSAFE_REMOTE=1` 只用于受信任局域网临时调试，不能作为公网部署方案。
- 公网版必须补齐账号与租户隔离、密钥加密、持久化任务队列、对象存储、配额和审计，详见 [公网部署架构蓝图](docs/PUBLIC_DEPLOYMENT_BLUEPRINT.md)。
- AI 决策和用户修改数据规则参见 [docs/DATA_GOVERNANCE.md](docs/DATA_GOVERNANCE.md)。
