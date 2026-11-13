# 选型与一手证据

检索日：2026-11-10。以下为公开源码/官方文档核查与工程建议，不是用户账号或设备的兼容性实测。链接与更多能力证据见 `sources.json`。开源代码免费不等于推理、存储、维护和商用许可均无成本。

## 1 首期采用的组合

| 层 | 明确选择 | 引入条件 |
|---|---|---|
| 项目主控 | 复用当前 Python + SQLite + 本地文件 | 不为新框架重建已能工作的流程 |
| 网站 | 复用现有索引/抓取；必要时参考 Crawlee 的持久队列/节流 | 简单HTML不启动浏览器；复杂授权页面才交互 |
| 百度文件层 | 当前已正式授权的接口；官方 Go SDK 作为优先适配候选 | OAuth/路径必须真实满足；不存在可用权限则blocked |
| 普通文本/Office | 原生格式读取 + MarkItDown，另保留媒体和关系 | 不把Markdown转换当图像已读 |
| PDF快速通道 | LiteParse | 先小样本确认Windows依赖/格式兼容；已有可靠快通道可继续复用 |
| 复杂文档/OCR | Docling + 一个本地OCR后端 | 按页/区域启用，CPU优先，不全量上VLM |
| 媒体 | FFmpeg/PyAV + faster-whisper，OCR复用RapidOCR | 外层限窗、单GPU worker、预算内校准 |
| 独立分析 | 当前授权通用Agent + 本包结果契约 | API模式是后续可替换执行器，不要求额外订阅才能起步 |
| 商业备用 | 已批准时 LlamaParse难页、Qwen ASR难段 | 无权限/预算默认不调用；不外发整套课程作为默认 |

不是要求同时安装表内全部工具：P1无视频就不安装ASR；现有解析器够用就不加同功能副本；一个OCR后端在文档和媒体中共用。

## 2 百度相关候选

| 项目 | 结论与原因 | 许可及维护观察 |
|---|---|---|
| baidu-drive-sdk-go | 官方文件API适配候选，Download返回io.ReadCloser；不自带已验证Range/低清/独立音轨 | Apache-2.0；默认分支提交2026-09-26 |
| bdpan-storage | 官方Skill/CLI，新增正文能力有价值，但目录与用户范围不相容；暂不启用 | Apache-2.0；提交2026-10-29，BETA |
| qjfoidnh/BaiduPCS-Go | 条件性CLI文件/转存工具，仅用于正常授权和平台允许的能力 | Apache-2.0；提交2026-09-09 |
| baidu-ai-batch | 只借鉴浏览器已有AI材料的批量取得；不是主提取引擎 | MIT；提交2026-08-01 |
| skipping-lectures | 借鉴来源路由、任务编排；平台专项实现不能当通用网站采集器 | MIT；提交2026-08-01 |
| baidu-transfer-helper | 分页/递归/退避适合补“分享→转存”；必须修成功判定和重复提交 | MIT；提交2026-07-12 |
| baidu-netdisk-knowledge-mcp | MCP/selection/模板入口可参考；不采用其解析和分析核心 | MIT；提交2026-06-17 |
| OpenList | 多云盘时备用；只评估官方访问模式，本期不新增常驻平台 | AGPL-3.0；提交2026-11-08 |
| bypy | 不选：维护模式且只能/apps/bypy，与用户范围不符 | MIT；提交2025-04-02 |

源码重要发现：

- baidu-ai-batch 的清单只处理当前目录和有限页数，且主要筛视频；导出PPT点击后短等待就写done，文稿按DOM文字阈值/文件存在判成功。这些不能作为本项目完整性检查。[源码](https://github.com/jing1312/baidu-ai-batch/tree/master/bin)
- skipping-lectures 实际编排香樟云课堂与百度两条专项链，不能直接转化为“所有网站/网盘均支持”。[说明](https://github.com/jing1312/skipping-lectures/blob/main/SKILL.md)
- knowledge-mcp 的PDF/DOCX主要读纯文本；摘要是截取前280字符等启发式，并非通用模型推理；下载用arrayBuffer整文件入内存。只复用接口设计。[读取](https://github.com/capwitf/baidu-netdisk-knowledge-mcp/blob/main/src/contentReader.ts)、[技能实现](https://github.com/capwitf/baidu-netdisk-knowledge-mcp/blob/main/src/skills.ts)、[下载](https://github.com/capwitf/baidu-netdisk-knowledge-mcp/blob/main/src/tools.ts)
- transfer-helper 默认newcopy、缺errno可能被解释成成功，且未完整读回目标目录。改造时以状态未知和对账优先，不能失联后重复转存。[源码](https://github.com/kola-official/baidu-transfer-helper/blob/main/src/core.js)
- 官方SDK流返回可减少整文件内存，但当前实现不等同Range客户端。[Download](https://github.com/baidu-netdisk/baidu-drive-sdk-go/blob/main/baidudriver/api/download_file.go)、[client](https://github.com/baidu-netdisk/baidu-drive-sdk-go/blob/main/baidudriver/api/client.go)

官方bdpan正文服务：TXT/MD与部分Office/PDF可读，部分类型异步，需结果查询；存在日配额、截断和长结果签名URL，不能当无上限的全模态解析器。图片/音频/压缩包并非该正文接口的支持对象。当前SKILL与CLI版本配套、系统支持和会话参数/遥测也要部署前审查；本包不请求启用。[命令](https://github.com/baidu-netdisk/bdpan-storage/blob/main/skills/baidu-drive/reference/bdpan-commands.md)、[范围](https://github.com/baidu-netdisk/bdpan-storage/blob/main/skills/baidu-drive/SKILL.md)

## 3 混合文档与分析候选

| 候选 | 采用位置 | 不选作万能核心的原因 |
|---|---|---|
| LiteParse | 本地数字PDF快速读取、复杂度探测、bbox与图像/页面 | 复杂图表语义仍需进一步解释；Office转换有依赖 |
| Docling | 统一结构化文档、复杂布局/表格/OCR、图片和来源关系 | pipeline能力不同，需显式导出真实图片及结构JSON |
| MarkItDown | 普通Office/文本的轻量通道 | Markdown不是全保真证据包；OCR插件需配置模型 |
| MinerU | 中文复杂版式/公式备用；Basic CPU等分层评估 | 4.x与旧接口存在断层；定制Apache-based条款需另核对 |
| Unstructured | 已有其生态时可替代Docling | 不同时安装两套全栈；增强描述不能覆盖原表格证据 |
| LlamaParse | 商业难页/难图备用 | credits、外发权限、异步产物与图像保存仍要管理 |
| Azure Document Intelligence | 已有Azure环境的文档备用 | 直接Office/HTML路径不分析嵌入/链接图像 |
| Azure Content Understanding | 文档/音视频统一商业备用 | 多模态计量和生成token另计，不是全课低价无上限服务 |
| Google Document AI | 已有GCP环境的文档布局备用 | 格式、版本和页/批处理限制，非百度连接器 |
| DocETL | 分批理解、事实合并/去重、成本统计参考 | 不是文件获取或OCR；自动优化需预算 |
| RAG-Anything/LightRAG | 后续跨项目检索与综合 | 首次全面理解仍需覆盖；构图也花费模型资源 |
| NotebookLM/Notebook Enterprise | 阅读对照/可选消费端 | 已核实的管理API不等于全面自动分析/导出API；不是取材基础 |

已核查的关键工程限制：

1. LiteParse无需LLM/API，可本地解析与评估PDF复杂度；LlamaParse MCP已有按页复杂度选轻重路线的接口设计。参考其逻辑，不等同于使用hosted MCP就本地不外传。[本地能力](https://developers.llamaindex.ai/liteparse/)、[MCP](https://developers.llamaindex.ai/llamaparse/for-agents/mcp/)
2. Docling原生PDF快速通道不等同标准布局重建。位图提取/输出需相应配置，矢量流程图还可能需要渲染。[选项](https://docling-project.github.io/docling/usage/advanced_options/)、[参数](https://docling-project.github.io/docling/reference/pipeline_options/)
3. MarkItDown当前官方OCR插件没有llm_client时可静默跳过OCR；程序要显式登记OCR是否执行。[官方README](https://github.com/microsoft/markitdown)
4. MinerU当前文档库CLI读取默认可能只取前10页，要核对页集合并继续读取；stateless命令默认范围不同。[README](https://github.com/opendatalab/MinerU)
5. RAG-Anything当前文档明确MinerU依赖为3.x，不能直接与4.x混装。若采用新MinerU，应独立适配标准产物/环境，而不是全局降级所有解析器。[README](https://github.com/HKUDS/RAG-Anything)
6. Azure DI原生Office/HTML图片限制说明即使商业产品也需单独保全图片；免费F0输出还可能只覆盖前两页。[官方限制](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/service-limits?view=doc-intel-4.0.0)

维护快照：Docling、LiteParse、MinerU、LightRAG查询日仍有推送；MarkItDown推送2026-11-07；Unstructured 2026-11-06；DocETL 2026-11-09；RAG-Anything 2026-11-02。推送时间不等于稳定发行或本机性能，不据此自动升级。

许可证：Docling、MarkItDown、DocETL、RAG-Anything、LightRAG代码MIT；LiteParse和Unstructured代码Apache-2.0；MinerU须阅读实际定制条款。模型权重与依赖分别核查，不在本文作法律结论。

## 4 媒体候选

| 候选 | 主备取舍 | 许可/维护注意 |
|---|---|---|
| FFmpeg/PyAV | 默认媒体读取与限窗解码 | FFmpeg取决于编译选项，PyAV BSD；只读远程并不自动可seek |
| faster-whisper | 首个ASR基线，INT8/小批量 | MIT；release旧于最新提交不是弃用理由；必须外层限窗 |
| SenseVoice + sherpa-onnx/FunASR | 中文/CPU路线经样本确认后可替换 | 代码许可不覆盖模型权重；输入和时间戳协议有版本差异 |
| Silero VAD | 减少无声语音计算 | MIT；不等于无声画面无信息 |
| RapidOCR | 文档/画面共用CPU OCR | Apache-2.0代码，权重另核查 |
| PaddleOCR | 复杂结构/公式备用 | 不默认再加一套大模型；权重另核查 |
| PySceneDetect | 视觉事件候选检测 | BSD；必须搭配局部文字变化和保底采样 |
| Qwen ASR filetrans | 已授权预算内的难段/加速备用 | 商业异步；记录任务、外传和按时长费用 |
| Gemini视频理解 | 少量动态难片段 | 商业；采样与分辨率都可能影响细节，非全量默认 |

最重要的源码事实：faster-whisper的音频decoder会将整个输入累积为数组，应用外层必须切窗。[源码](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/audio.py)

第三方ASR benchmark的显卡、语言、batch和计时范围不等于用户6GB设备整链路表现。首个部署由Agent自动记录短样本读取/解码/识别/落盘的端到端耗时，随后给ETA。

## 5 工作流平台的取舍

- Crawlee：可复用持久URL队列与资源节流，但不要求替换已有有效抓取代码。单机文件存储不是多进程共享队列；续跑配置要避免启动即清空。
- Prefect：确需可视运行台时再加；基础自托管可用SQLite，扩展多服务有额外数据库/消息依赖。本期不为工作流外观引入它。
- LangGraph：需要复杂Agent分支/持续运行时可用，必须持久checkpointer；内存MemorySaver不跨进程重启恢复。
- n8n：适合外围调度/通知/现成API连接，不把巨型音视频二进制传过每个节点。其Sustainable Use License不是无限制OSI开源商用许可，未来对外服务需另核对。

## 6 不再重复造轮子的边界

复用来源连接、文档解析、ASR、OCR、编排接口；自己只实现项目身份/课程关系、授权预算、自动路由、覆盖对账、证据包和独立分析交接。这些用户专属规则没有被本轮找到的任何单一候选完整满足，不能只装一个“知识库MCP”就宣称闭环。
