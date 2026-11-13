# 主控与数据契约

## 最小部署

复用当前 Python 服务/CLI + 一个业务 SQLite + 文件系统。采集与分析可各自运行，使用同一个项目身份体系，通过不可变证据包交接。MCP 是 Agent 调用工具的接口，不是任务数据库；浏览器只处理必须交互的授权页面，不负责连续观看百小时视频。

业务台账不随解析器/模型切换重建。若未来引入 Prefect/LangGraph，其内部运行记录不是第二套业务完成状态，业务真相仍以验证后的证据和项目台账为准。

## 对象关系

- SourceRecord：网站文章/指定分享/本地输入的来源记录。多篇文章可以指向同一项目。
- Project：课程或资料集合的分析隔离单元；author、topic、edition 是证据支持的属性，不靠标题定死。
- Asset：单文件/压缩包成员/视频/音频等，稳定 asset_id；原始云盘 fs_id 与路径保留。
- Artifact：原始文件、正文、表格、图片、转写、画面片段等实得产物。
- Block：可被模型消费的段落/表格/公式/图像区域/时间片，带 locator 和 origin。
- Job：有明确输入版本、操作、provider、资源预算和输出凭证的工作项。
- Bundle：一组已发布的 Artifact/Block 及 coverage/gaps，不可原位覆盖。
- AnalysisRevision：基于指定 Bundle 版本和模型/提示词版本的理解结果。

分享侧与用户盘侧身份必须分开：保存 `source_share_ref + source_file_id → destination_account + destination_fs_id + verified_path`。转存后ID可以不同，不能拿分享ID直接读用户盘。每次用户盘操作都解析当前真实路径并按路径段校验授权根；`/apps/教育分析学习其他/`不匹配。素材被移动到范围外后停止读取；下载链接只绑定通过校验的目标素材。

关联表可沿用现有 schema 加字段/索引。不要仅为上述名字另造一套数据库。轻量幂等键建议由 asset_id、源修订、操作、provider版本、配置版本组成；大视频不用先下载求哈希。已经读取到的字节可顺便计算摘要用于防重复，不做全盘门禁。

## 采集工作流

发现来源 → 分页/递归清单 → 候选去重 → 必要的授权转存 → 目标读回 → 文件预检 → 提取计划 → 分块执行 → 程序质检/语义缺口检查 → 发布证据包。

本地已有材料不需要绕去网盘；网站直接可读小文档也不为流程整齐强制上传再下载，保留它在项目来源中的角色即可。只有实际资源在百度时才经过百度适配器。

## 分析工作流

领取证据包 → 生成阅读计划 → 主段逐批理解 → 事实/步骤/关系合并 → 关键证据回读 → 外部核验 → 项目总览/实操路线 → 缺口补采申请 → 新证据版本增量更新。

小材料可以在一批完成；大项目不能只取检索相似的几段就声称读完。引用定位和未读范围必须保留。

## 状态与成功条件

| 状态 | 语义 |
|---|---|
| queued | 已登记，尚未运行 |
| probing | 获取元信息和可用能力，不等于已读正文 |
| submitted / waiting | 有真实远端任务或下一次合法轮询时刻 |
| submission_unknown | 请求可能已送达但未收到任务号；保留预算预留，先对账，不自动重发 |
| running | 有正在执行的本地任务，崩溃后需核对产物再恢复 |
| partial | 已取得可用材料但仍有明确/未知缺口 |
| ready | 此证据包已发布可分析，不代表整个课程完全覆盖 |
| verified_complete | 本操作应覆盖的范围已逐项对账；非“理解绝对正确” |
| blocked | 权限、范围、配额、密码、资源预算或不支持条件阻断 |
| failed | 有限重试后仍失败，保留具体原因 |

远端请求结果、解析完成、证据包发布、分析完成、外部核验完成分开记录。HTTP 成功、CLI 退出 0、文件存在、点击导出或文本长度不能直接映射 verified_complete。

## 内部工具接口草案

这些是待实现的 Worldanalysis 接口，不是上游现成命令：

```text
source.discover(source_id, cursor, limit) -> source_records, next_cursor
storage.inventory(root_ref, cursor) -> assets, next_cursor
storage.inspect_share(authorized_share_ref) -> manifest
storage.transfer(manifest_ref, destination, idempotency_key) -> receipt
storage.reconcile(receipt_or_manifest_ref) -> verified_items, gaps
storage.probe_media(asset_id) -> capability_snapshot
storage.open_reader(asset_id, permitted_range?) -> bounded_reader
extract.probe(asset_id) -> format_profile, estimates
extract.plan(project_id, asset_ids, policy_version) -> Plan
extract.run(plan_id, job_id) -> artifacts, usage, errors
bundle.publish(project_id, validated_artifact_ids) -> Bundle
analysis.next(project_id?) -> task_envelope
analysis.complete(task_id, result_path) -> validation_result
analysis.request_evidence(project_id, locators, reason) -> supplement_jobs
```

不存在的能力返回 unsupported；权限拒绝返回 blocked，不伪造 success。远端 URL、认证和下载 headers 留在适配器中，模型只收到脱敏引用；禁止把 Cookie 或签名凭据写入报告。

## 能力预检

每个 provider/账号/素材分别记录 observed_at、实现版本、state、evidence_ref：

`list_paginated`、`list_recursive`、`transfer`、`download_stream`、`range_206`、`audio_only`、`renditions`、`existing_transcript`、`image_export`、`quota`、`expires_at`。

state 为 unknown/documented/verified_available/unsupported/blocked。只看到 README 是 documented；必须在授权样本上观察才是 verified_available。范围、质量、码率不能靠扩展名猜测。预检最多读取必要元信息和受限小范围，不能为了探测先下载全视频。

Range探针设置累计实际读取字节硬上限和超时，验证206及Content-Range。若服务器忽略Range返回200，关闭响应并记unsupported/unknown，不把完整响应交给arrayBuffer或解码器。顺序全流是另一条需重新预算的路线，不是探针自动降级。请求头限范围不等于实际读取已限范围。

## Locator 与来源类型

定位至少支持：文档页与 bbox、slide编号、sheet名称与单元格范围、段落序号/字符偏移、视频/音频起止毫秒、压缩包成员路径、工程文件节点ID。DOCX 未渲染时不臆造稳定页码；XLSX 不强塞页码。

bbox还必须记录所指页面/原图、原始尺寸、单位（pixel/pdf_point/normalized）、坐标原点、旋转和裁切/缩放变换；有变换时保存到原页的矩阵或显式映射。转正截图不能沿用原页坐标而不转换。无可靠坐标时保留页面级引用并记定位不确定，不编造bbox。

origin 必须区分 author_original / native_extract / ocr / asr / visual_observation / provider_ai_note / model_inference。原文、纠错与推断分层；三个来自同一百度 AI 的摘要不是三份独立证据。

## 交接与目录

沿用本地根：

| 相对位置 | 内容 |
|---|---|
| `整理结果/项目ID/原始材料/` | 小原文件、实得原始导出，不改写 |
| `整理结果/项目ID/证据/vNN/` | manifest.json、blocks.jsonl、images、tables、transcripts、coverage |
| `整理结果/项目ID/分析/vNN/` | 结构化理解、主报告、当前核验、学习/实践路线 |
| `整理结果/项目ID/项目总览.md` | 当前可读入口，链接原证据和历史版本 |
| `下载暂存/任务ID/` | 有所有权登记和上限的媒体/中间缓存 |
| `手动投放/` | 用户主动放入的原资料；不得移动或删除 |

发布使用临时文件写完→校验→同卷原子重命名→数据库事务登记。分析只消费已发布版本。大块音视频、图像字节不放 SQLite、LLM 消息或 MCP 返回体。

文件重命名与SQLite不是一个原子事务。先登记publish_id和staging状态，再写/验证/重命名，最后提交published。启动时对账staging、已重命名但未登记目录、已登记但缺文件；按publish_id幂等续作或隔离异常。分析只有在数据库状态与文件清单双重一致时才消费；不得把孤立目录自动当新版本。

## Agent 模式与自主 API 模式

- agent_assisted：当前用户已授权的开发/运营 Agent 领取任务、按路径读证据、产出 JSON+报告；程序核对 task_id、bundle版本、覆盖和附件。不提取订阅会话令牌给脚本。
- api_worker：独立配置合法模型/搜索 API 后使用相同契约。没有 API 不伪造无人值守，仍可先使用 agent_assisted。
- 仅在批准的模型和解析器注册表中选路。模型不能临时创建计费账户、安装来源代码、提升权限或自动更改预算。
