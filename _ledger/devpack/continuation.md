# DevPack 续作检查点

## 当前事实（2026-11-13，Asia/Singapore）

- 最新覆盖：[第七轮](<round7-progress.json>)用户已明确选择保持原约束，不传出TXT/MP4下载链接；禁止再次询问/探测或换渠道下载。外部reader接口已实现并接Analysis完整receipt校验，45项27.799s全过，但一次pinned公共GET失败，无HTTP状态、无external读回执，未重试；不能报真实外部核验完成。已有075派生PPTX小样本2页/0hidden/0notes，2图真读+4文本回执，6read16pending、19open gaps，partial正常接受，SHA6afa6d5...，未新生成。此前[第六轮](<round6-progress.json>)真实PDF基报告已验收28块（10不同图/19视觉回执、9文本1887字），保留23open gaps。随后同项目v0007增量报告已正常committed并Root get_result复核：28条kind=reused历史真读＋1条新text真读6317字、0新image；29/29已知块，32主张，25open gaps。accept SHA d87ef72640e3d58819700d1da3511f79cb359a33e2d2be5abe05eac1465e1c15。严格同完整agent配置/源原件摘要/图字节/正文/定位/依赖，否则不可复用；stat-only媒体、legacy无journal、不同模型、多跳均不冒充已读。CLI analysis delta/reuse已接，49项55.764s全通过。一个合成媒体校准task9ea1b...仍running/pending（不符合严格复用条件，不伪标已完成）；后续可正常续读。此前见[第五轮进展](<round5-progress.json>)：混合Bundle组合、自动ingest路由、旧producer回执保持、完整正文联系人索引与完成转存的只读续权已接通。相关59项28.145秒全部通过。真实源/目标18条绑定不变，未再转存。正式UI已签发TXT下载票据，但不导出签名URL的浏览器私有读取失败（无HTTP状态，不得伪称403）；没有本地文件，不得换后端绕过。当前下载保护已启用直到self-host host退出，必须走dsh_browser_download及目录准入。旧分享/重复标签页已关闭，仅保留授权个人目标页；所有自建观察器已恢复。后续先解决受保护的票据交接或接收用户本地材料，再做真实课程校准。此前见[第四轮真实进展](<round4-progress.md>)。真实课程已单次转存到严格授权目标，18条独立目标ID/路径/大小已逐项读回，转存操作verified_complete；本地下载未尝试，不能误报P2全完成或再点保存。用户要求减少无效分析和重复测试，快速推进。[第三轮检查点](<round3-quick-review.json>)保留20项针对性检查4.857秒全通过，未跑完整回归。目标 `goal-432da048-e464-4fb3-89ff-2927dfba6d37` 实时revision 6、**active/armed**，不需要再次要求用户恢复。下述为前轮历史记录，涉及未接media/未有逐资产cache等状态以新检查点为准。全计划仍未完成。
- 实现代码提交：`326a7c2`；原开发包保全提交：`45a4ccc`。旧材料未覆盖、未移动、未删除；旧075核验仍为78文稿、12原生课件、785页、完整性问题0。
- [版本绑定回归](<verification-20261113T134802Z.json>)：188项，187通过、1项OS无symlink创建权限跳过，0失败/错误；源码测试前后SHA一致，暂存代码SHA也逐个对照通过。旧Node两套回归通过。
- 实现模型与完整剩余范围见[执行模型](<执行模型.md>)、[21场景矩阵](<acceptance-matrix.json>)；使用入口见[运行说明](<../../WORLDANALYSIS.md>)。不能只看此检查点报完成。
- 唯一业务数据库：[SQLite](<../../整理结果/worldanalysis.sqlite3>)；媒体内部检查点不是另一套业务完成台账。文件/模型/原始课程均留在工作区，不把运行时、原图或原件提交为代码。

## 已闭合的实际小样本

1. [短TXT主报告](<../../整理结果/real-shorttxt-meituan/分析/analysis-task-0193d2b5749b418c84f724b4b43154be/accepted/report-00.md>)：54字节、保留26字符及CRLF；全1块已读，报告partial，1个本地补采申请。
2. [3图DOCX主报告](<../../整理结果/real-docx-virtual-materials/分析/analysis-task-74a65236b89342fb91ed27c8d623a1d7/accepted/report-00.md>)：实际已读8正文和3原图，4空正文容器pending，原7gaps保留并新增1项缺口；2本地补采申请未执行。
3. [数字＋扫描PDF当前证据](<../../整理结果/real-pdf-163545/证据/v0003/manifest.json>)：9页真实渲染、1个OCR派生块、28块、23明确缺口，仍partial。反序同组重跑已实际复用同bundle。早期重跑缺陷产生的v2/v3均保留，没有删除证据隐藏问题。
4. [媒体校准](<media-provider-acceptance.json>)全部明确synthetic：2小时静音仅解码（不是ASR完成）、25窗、最大9.6MB PCM、约111.7MB峰commit；另8.938秒离线TTS实际Whisper CPU推理成功。不是生产课程/百小时验收。

## 修复与恢复注意

- 已独立复核关闭跨项目Job被错误完成、分析目录先落盘而DB失败后不能恢复两项问题。分析提交有task/submitted SHA journal，篡改隔离不盲删。
- 同源同配置的单文件与确切多文件组复用已验Bundle；配置含实际解析器代码/依赖/CPU OCR权重SHA。**混合新增/改变输入组合的逐资产raw复用尚未实现**，不能称全部增量完成。
- 旧TXT早于历史validation hash字段；只追加明确标记 `legacy_cross_checked_not_historical_hash` 的索引，没有重写旧event、报告或校验字节。两真实accepted结果当前get_result均通过。
- 内置文件工具若仅报Publication授权未发布，可先用 `ops_discover_tools` 的 `select:确切工具名` 正式发布，必须得到Selected才能在新一步使用；实际权限拒绝仍不得换壳/换通道。源ZIP已正式读通，不需要用户重传。

## 恢复后优先动作

1. 确认实时goal与用户控制状态；只按新的直接授权或用户在目标栏恢复后续作，不伪造恢复。运行应用status，读取现有补采队列，不重复生成已完成报告。
2. 继续P1的本地补采与空容器确认；真实XLSX及同文件混合PDF样本目前缺，合成测试不可冒充真实验收。全页渲染已存在，但图/公式含义未必已理解。
3. P2真实[范围观察](<p2-browser-observation-20261113.json>)确认 `/apps/教育分析学习/` 可读，目标 `/apps/教育分析学习/待分析/163378_AI视频创作` 原先为空，网站文章 `https://www.wangzjk.com/163378.html` 的特定分享可见。仅做过目录观察，**本轮没有转存/下载/生成提交，无待对账的云端副作用**。先实现只读页面响应的元数据投影（不读Cookie/通用token、不回传签名URL）、完整递归分页清单与实际当前授权桥接，再登记持久操作后正常UI转存；目标独立ID与路径读回后才下载。不要直接复用分享ID或转存到旧 `/wangzjk.com`。
4. P3先将 `Store.ingest_media` 的明确blocked分支真正接上有界worker与通用Bundle，不回到整片hash/float32/重复混流。只在授权目录取得真实小视频后测端到端读取/解码/ASR/OCR/发布，语种与准确性另验，再估长课ETA。
5. 补P4逐资产增量、来源关联与外部核验reader。新商业服务仍0预算/0外发，默认不请求百度AI新生成；独立host授权器不是Plan中的字符串。

未创建定时器、常驻循环或新外部服务。无须为常规解析器选择反复问用户；不可替代的登录、范围、费用或确实缺失的真实输入再合并说明。
