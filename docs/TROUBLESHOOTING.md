# 故障排查

## 网页无法启动

1. 在项目目录运行 `python --version`，需要 Python 3.10 或更高版本。
2. 运行 `python -m pip install -r requirements.txt`。
3. 运行 `python scripts/check_all.py` 查看第一个失败项目。
4. 如果 AI-cutting 已经运行，一键脚本会直接打开现有页面；如果 8000 被其他程序占用，才会自动尝试 8001-8009。

## FFmpeg 或 FFprobe 不可用

分别运行：

```powershell
ffmpeg -version
ffprobe -version
```

两条命令都必须成功。最终字幕依赖 FFmpeg 的 libass 支持。设置页的“运行环境”会显示应用实际检测结果。

## 火山识别失败

- 确认设置页填写的是录音文件识别服务对应的 APP ID 和 Access Token。
- 确认服务已开通且仍有可用时长。
- 代理环境下先确认终端可以访问 `openspeech.bytedance.com`。
- 失败任务的技术日志不会显示完整 Token；具体 Python 调用栈保存在任务目录的 `debug_traceback_*.txt`。

## DeepSeek 没有生效

1. 设置页启用语义分析。
2. Base URL、Model 和 API Key 缺一不可。
3. 点击连接测试。
4. 新任务会在 `work/<视频编号>/ai_decisions.json` 记录模型、提示词版本、调用状态、token 和缓存命中。
5. 旧任务不会自动补做新版 AI 分析，需要重新创建任务。

## 字幕识别不准或断句奇怪

- 优先为每条视频上传对应的 UTF-8 `.md` 或 `.txt` 逐字稿。
- 格式参见 [TRANSCRIPT_FORMAT.md](TRANSCRIPT_FORMAT.md)。
- 音频决定实际说了什么，逐字稿用于纠正写法；逐字稿中没有说出口的内容不会凭空加入字幕。
- 字幕保持预设字号和单行显示，系统会根据真实字体宽度重新断句。

## 暂停后如何继续

- 点击“暂停任务”后状态先变为“正在安全暂停”，系统会完成当前原子步骤再停下。
- “已暂停”后可点击“继续任务”。火山 ASR、DeepSeek、剪辑源、成片和封面会复用已验证检查点。
- 服务意外关闭后，环境和凭证完整的任务会自动恢复；否则变为“已暂停”。

## C 盘空间持续减少

- 临时 FFmpeg 目录会在单次任务结束后清理。
- 持久数据主要位于项目的 `jobs/` 和 `web/.cache/`。
- `jobs/` 保存上传视频、检查点、精修代理和输出；确认不需要旧任务后可删除对应任务目录。
- `web/.cache/llm/` 保存 DeepSeek 结果以避免重复扣费，可以在没有运行任务时清理。

## 出现乱码或 `Invalid \\escape`

- 文本、逐字稿和 JSON 必须使用 UTF-8。
- Windows 路径不要手工写进 JSON 字符串；通过网页选择文件。
- 运行 `python scripts/check_encoding.py` 检查仓库文本。
- 运行 `python scripts/check_web_error_boundaries.py` 检查 Web JSON 错误边界。
