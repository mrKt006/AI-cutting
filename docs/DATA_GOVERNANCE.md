# 本地 AI 决策与用户修改数据规则

## 当前范围

- 所有 AI 决策和用户修改数据默认只保存在当前电脑的任务目录中。
- 项目不会自动上传逐字稿、AI 决策、修改记录、视频、音频或 API Key。
- `training_feedback.json` 的 `training_consent` 默认固定为 `false`，当前版本不把本地记录当作已授权训练数据。
- API Key、Authorization 请求头和本地设置文件不得进入决策日志、修改记录或 Git。

## 本地文件

- `work/<视频编号>/ai_decisions.json`：模型、提示词版本、输入摘要、候选、原始 JSON 响应、解析决策、校验结果、耗时和 token 用量。
- `work/<视频编号>/auto_edit_baseline.json`：用户首次精修前的结果快照。
- `work/<视频编号>/training_feedback.json`：文本、删除、恢复、断句、时序、样式、内容标题和封面修改差异。

这些文件随任务保留，用户删除任务目录时一并删除。只删除修改反馈时，可调用：

```text
DELETE /api/jobs/<任务ID>/training-feedback?item=<视频编号>
```

该接口会删除 `training_feedback.json` 和 `auto_edit_baseline.json`，不会删除视频输出、逐字稿或运行所需的检查点。

## 公网版前置条件

公网部署前必须另外完成：

1. 运行日志、诊断日志和可训练数据分库存储。
2. 用户主动授权、撤回授权和删除数据的可视化入口。
3. 按账号和租户隔离任务、媒体、密钥与反馈数据。
4. 定义可训练数据保留期限、脱敏规则和删除审计。
5. 禁止管理员、日志平台和错误追踪系统读取明文 API Key。
6. 未授权数据不得进入提示词评估集、人工标注集或模型训练集。
