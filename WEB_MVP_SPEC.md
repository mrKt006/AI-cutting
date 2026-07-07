# AI-cutting Web MVP Spec

## 1. 项目目标

把当前命令行版中文竖屏口播自动剪辑工具封装成本地 Web 应用。

用户不需要记命令，只需要在浏览器中上传视频、填写标题、选择剪辑强度和字幕识别方式，点击开始后等待任务完成，并下载成片、封面、字幕和报告。

第一版目标是本地可用、流程清晰、结果可复用，不做公网部署、多用户账号和复杂在线编辑器。

## 2. 核心用户场景

用户有一条竖屏口播视频，希望自动完成：

- 剪掉明显停顿和气口
- 调用火山引擎识别真实口播内容
- 生成简体中文字幕
- 烧录字幕到视频
- 生成封面图
- 输出剪辑报告

理想使用流程：

```text
打开 http://127.0.0.1:8000
上传视频
填写封面标题
选择剪辑预设
点击开始处理
等待进度完成
下载 final.mp4
```

## 3. 产品边界

### 3.1 第一版要做

- 本地 Web 页面
- 单任务提交
- 任务状态展示
- 视频上传
- 标题输入
- 剪辑强度预设
- 火山引擎 ASR 字幕
- 本地 Whisper tiny 作为备用选项
- 简体中文字幕
- 输出文件下载
- 每个任务独立目录，避免互相覆盖
- 失败时展示可读错误

### 3.2 第一版不做

- 公网部署
- 用户登录
- 多用户权限
- 在线字幕逐句编辑
- 在线时间轴剪辑器
- 多视频批处理
- 手动拖拽剪辑点
- 自动 B-roll
- 自动花字动画
- 自动音乐音效
- 自动复杂语义纠错
- 云端存储管理

## 4. 推荐技术方案

### 4.1 后端

推荐：

```text
FastAPI
Uvicorn
Jinja2 templates
```

原因：

- Python 生态，能直接复用当前 `src/` 逻辑
- 本地启动简单
- 后续容易加任务状态、下载接口、API

### 4.2 前端

第一版使用：

```text
HTML + CSS + 少量原生 JavaScript
```

不引入 React/Vue，避免第一版复杂化。

### 4.3 任务执行

第一版可以使用后台线程：

```text
POST /jobs
-> 保存上传文件
-> 创建 job_id
-> 后台线程运行剪辑流程
-> 前端轮询 /jobs/{job_id}
```

不需要 Celery/Redis。

## 5. 目录结构

建议新增：

```text
web/
  app.py
  templates/
    index.html
    job.html
  static/
    style.css
    app.js

jobs/
  {job_id}/
    input/
      video.mp4
      title.txt
    output/
      final.mp4
      cover.jpg
      subtitle.ass
      subtitle.srt
      edit_report.json
      volcengine_segments.json
    job.json
```

`jobs/` 不提交到 Git。

## 6. 页面设计

### 6.1 首页

首页就是实际工具，不做营销型 landing page。

页面结构：

```text
顶部栏
  产品名：AI-cutting
  当前状态：本地模式

主区域
  左侧：任务表单
  右侧：最近任务/输出说明
```

### 6.2 表单字段

必填：

- 视频文件
- 封面标题

推荐默认：

- 字幕来源：火山引擎
- 剪辑强度：标准
- 中文输出：简体

高级设置默认收起：

- 静音阈值 `noise`
- 最短静音 `min_silence`
- 剪辑缓冲 `padding`
- 字幕整体偏移 `subtitle_delay`
- 是否检测卡壳/重复
- 是否保留中间音频文件

### 6.3 剪辑强度预设

使用分段控件，不让普通用户直接面对参数。

```text
自然
  noise=-30dB
  min_silence=0.45
  padding=0.12

标准
  noise=-28dB
  min_silence=0.35
  padding=0.10

紧凑
  noise=-26dB
  min_silence=0.30
  padding=0.08

激进
  noise=-24dB
  min_silence=0.25
  padding=0.06
```

默认选：

```text
标准
```

### 6.4 字幕来源选项

```text
火山引擎：推荐，准确率更高，需配置 VOLC_APP_ID 和 VOLC_ACCESS_TOKEN
本地 Whisper tiny：离线可用，准确率较低
外部 ASR JSON：高级入口，用已有识别结果生成视频
```

第一版默认：

```text
火山引擎
```

### 6.5 任务状态页

状态流：

```text
等待中
保存上传文件
剪辑停顿
提取剪后音频
火山识别
生成字幕
渲染视频
生成封面
完成
失败
```

页面展示：

- 当前状态
- 简短日志
- 已耗时
- 完成后下载按钮
- 失败时错误信息

下载区：

```text
final.mp4
cover.jpg
subtitle.srt
subtitle.ass
edit_report.json
```

## 7. UI/UX 设计原则

### 7.1 视觉风格

应该像一个本地生产工具，而不是宣传页。

关键词：

```text
清爽
可靠
密度适中
参数不吓人
结果导向
```

建议颜色：

- 背景：浅灰白
- 主色：蓝色或青蓝色
- 文本：深灰
- 成功：绿色
- 错误：红色
- 警告：橙色

避免：

- 大面积渐变
- 过多装饰
- 花哨卡片
- 复杂动画

### 7.2 表单体验

- 上传区域支持点击和拖拽
- 文件名、大小、时长展示
- 标题输入框显示当前标题
- 剪辑预设用分段控件
- 高级参数默认折叠
- 开始按钮在参数合法后可点击
- 处理中按钮禁用，避免重复提交

### 7.3 错误提示

常见错误要给人能理解的提示：

```text
未配置火山 Access Token
视频文件不存在
FFmpeg 不可用
火山识别超时
字幕渲染失败
磁盘空间不足
```

错误提示应给下一步动作，例如：

```text
请在 CMD 中设置 VOLC_APP_ID 和 VOLC_ACCESS_TOKEN 后重启 Web 服务。
```

## 8. 后端任务模型

`job.json` 示例：

```json
{
  "id": "20260703-153000-ab12",
  "status": "running",
  "stage": "volcengine_asr",
  "created_at": "2026-07-03T15:30:00+08:00",
  "updated_at": "2026-07-03T15:31:12+08:00",
  "input": {
    "video": "jobs/.../input/video.mp4",
    "title": "我的标题"
  },
  "params": {
    "subtitle_source": "volcengine",
    "preset": "standard",
    "noise": "-28dB",
    "min_silence": 0.35,
    "padding": 0.1,
    "subtitle_delay": 0
  },
  "outputs": {
    "final": "jobs/.../output/final.mp4",
    "cover": "jobs/.../output/cover.jpg"
  },
  "error": null
}
```

## 9. 后端接口

### 9.1 首页

```text
GET /
```

返回上传表单。

### 9.2 创建任务

```text
POST /jobs
```

表单字段：

```text
video
title
subtitle_source
preset
noise
min_silence
padding
subtitle_delay
detect_disfluency
```

返回：

```json
{ "job_id": "...", "status_url": "/jobs/..." }
```

### 9.3 查询任务

```text
GET /jobs/{job_id}
```

返回任务页面或 JSON。

### 9.4 下载文件

```text
GET /jobs/{job_id}/download/final
GET /jobs/{job_id}/download/cover
GET /jobs/{job_id}/download/subtitle-srt
GET /jobs/{job_id}/download/report
```

## 10. 环境配置

火山引擎凭证仍然走环境变量：

```cmd
set VOLC_APP_ID=你的 APP ID
set VOLC_ACCESS_TOKEN=你的 Access Token
```

Web 启动时检查：

- `ffmpeg`
- `ffprobe`
- 火山环境变量是否存在
- 本地 Whisper tiny 模型是否存在

如果火山凭证不存在：

- 火山选项显示警告
- 仍允许使用本地 Whisper tiny

## 11. 处理流程

火山推荐流程：

```text
上传视频
-> 保存到 jobs/{job_id}/input/video.mp4
-> 根据预设剪静音
-> 得到 jobs/{job_id}/work/cut.mp4
-> 提取 cut.mp4 音频
-> 调火山 ASR
-> 得到 volcengine_segments.json
-> 生成 subtitle.srt / subtitle.ass
-> 烧录字幕
-> 生成 cover.jpg
-> 写 edit_report.json
-> 任务完成
```

## 12. 卡壳和重复处理

第一版只做检测，不自动删除。

检测结果写入：

```text
edit_report.json -> disfluency.repeat_candidates
```

原因：

```text
自动删除口播内容有误删风险，需要先观察候选质量。
```

后续再增加：

```text
--cut-disfluency
```

并提供 UI 开关：

```text
检测卡壳
自动删除明显重复
```

默认：

```text
关闭自动删除
```

## 13. 验收标准

本地启动：

```bash
uvicorn web.app:app --reload
```

浏览器打开：

```text
http://127.0.0.1:8000
```

必须做到：

- 能上传视频
- 能填写标题
- 能选择剪辑预设
- 能提交任务
- 能看到任务状态
- 能调用火山识别剪后音频
- 能生成最终视频
- 能下载输出文件
- 每次任务输出独立目录
- 失败时能看到错误原因

## 14. 后续迭代方向

- 在线字幕编辑
- 字幕时间微调
- 视频预览
- 多任务队列
- 批量处理
- 自动卡壳删除
- 剪辑点可视化
- 输出参数预设
- 封面样式配置
- 云端部署版本
