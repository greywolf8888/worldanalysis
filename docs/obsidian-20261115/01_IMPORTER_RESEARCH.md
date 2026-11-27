# 百度课程笔记导入器研究与 Worldanalysis 接入建议

研究日期：2026-11-15。

研究对象：`Tyukeying/baidu-course-notes-importer` 及其上游 `hyq2890-blip/netdisk-ai-notes-importer`。

验证方式：通过 GitHub 读取当前提交、目录、README、实际实现、测试与许可。**本研究未运行参考插件、未访问用户网盘，也未验证真实课程提取；本分支的落地实现和验证见其他文件。** 文档中的“源码已实现”不等于“已经在用户账号上跑通”。

## 1. 接入结论

吸收参考项目的字幕探测、笔记转换、图片保存、来源关联和用户内容保护方法。Worldanalysis 继续负责课程任务、证据、验收与恢复；Obsidian 负责阅读、检索、批注和回看。

建议先把现有合格资料输出成普通 Markdown 与本地图片，让 Obsidian 直接打开；随后再接薄采集适配器。不要让是否打开 Obsidian、是否保留某个网页标签，成为所有后台采集任务的前置条件。

用户已经确认的边界仍然有效：允许完整时间轴流式读取；不新增完整视频或完整音频文件，不采用先保存整片再删除的流程；最终保留文案、真实画面及必要的少量追踪信息。已有字幕、文稿和图片优先复用，缺失资料仍需补采，不能要求每个视频预先拥有百度 AI 笔记。

## 2. 本次核查的固定版本

| 项目 | 最新核实提交 | 版本 | 提交日期 |
|---|---|---|---|
| Tyukeying 定制分支 | `5cd6dd9f028c9d34638b94c650dc199a17dc45d0` | 0.5.14 | 2026-09-28 |
| hyq 上游 | `27ab3bd8e1d06710f255bb9d56ff1490576782d6` | 0.8.1 | 2026-09-12 |

版本核查入口：[定制分支提交](https://github.com/Tyukeying/baidu-course-notes-importer/commit/5cd6dd9f028c9d34638b94c650dc199a17dc45d0)、[上游提交](https://github.com/hyq2890-blip/netdisk-ai-notes-importer/commit/27ab3bd8e1d06710f255bb9d56ff1490576782d6)。两条分支的版本号不能直接用数值大小判断功能包含关系。

定制分支的主要实现是 4312 行打包 `main.js`，当前树中没有拆分的 `src/`。上游保留 `src/converter.js`、`src/webview.js` 等模块。定制分支的 manifest 声明仅桌面端、最低 Obsidian 版本 1.8.0；这属于插件声明，不等于本研究验证过所有桌面系统或 Obsidian 版本。

来源：[main.js](https://github.com/Tyukeying/baidu-course-notes-importer/blob/5cd6dd9f028c9d34638b94c650dc199a17dc45d0/main.js)、[manifest.json](https://github.com/Tyukeying/baidu-course-notes-importer/blob/5cd6dd9f028c9d34638b94c650dc199a17dc45d0/manifest.json)、[README](https://github.com/Tyukeying/baidu-course-notes-importer/blob/5cd6dd9f028c9d34638b94c650dc199a17dc45d0/README.md)。

## 3. 已经存在的设计与可复用部分

下表描述参考源码的实际行为。后面的接入建议是针对 Worldanalysis 的判断。

| 能力 | 源码入口 | 实际行为 | 适合复用的部分 |
|---|---|---|---|
| 笔记快照 | `extractFcbSnapshot`、`extractVideoPageNoteSnapshot` | 读取笔记编辑区，提取标题、HTML、时间信息和视频候选 | 小而明确的页面快照；采集后统一处理 |
| 多来源字幕 | `extractOnlineSubtitles`、`collectOnlineSubtitles` | 探测播放器字幕轨、SRT/VTT、字幕清单、JSON、DOM 和可见文稿 | 候选来源标签、有限重试、不同页面适配 |
| 字幕清单 | `selectSubtitleResourceUrls`、`subtitleUrlsFromHls` | 筛选百度相关资源，读取字幕清单引用的字幕文件 | 字幕资源与媒体资源分开处理 |
| 字幕整理 | `normalizeOnlineCues`、`parseSrtCues` | 整理时间、清理文本、排序、去重 | 可测试的纯转换函数；保留原始来源 |
| HTML 转 Markdown | `convertHtmlToMarkdown` | Turndown/GFM，并补 Quill 列表、表格、加粗、公式和时间戳规则 | 语义转换规则，不依赖页面截图反推文字 |
| 图片保存 | `localizeImages`、`validateDownloadedImage` | 请求图片字节，检查响应，保存附件 | 真实本地图片文件；失败信息需要加强 |
| 图片去重 | `stableImageIdentity`、`binaryHash`、`findExistingImageByContent` | 排除部分易变签名参数，并使用 SHA-256 查重 | 内容哈希及引用复用，避免签名变动造成重复图片 |
| 准备后提交 | `prepareWebviewImport`、`importFcbWebviewWithSubtitles`、`commitPreparedWebviewImport` | 单篇组合入口先采集字幕，再创建包含笔记和字幕的正文 | 准备、验收、发布分开；失败不覆盖已有资料 |
| 用户内容保护 | `replaceManagedSection`、`refreshCurrentNote` | 更新标记区域，保留区域外内容 | 对机器内容与人工编辑建立明确边界 |
| 回看原视频 | `seekFromNote`、`waitAndSeek` | 拦截时间戳并控制 WebView 播放位置 | 可选播放定位功能 |
| 诊断 | `formatImportDiagnostics` | 记录阶段、来源、候选数、重试和错误，并脱敏 | 诊断接入现有任务记录，不另建权威任务库 |

以上函数位于固定版 [main.js](https://github.com/Tyukeying/baidu-course-notes-importer/blob/5cd6dd9f028c9d34638b94c650dc199a17dc45d0/main.js)。可同时参考拆分更清楚的上游 [converter.js](https://github.com/hyq2890-blip/netdisk-ai-notes-importer/blob/27ab3bd8e1d06710f255bb9d56ff1490576782d6/src/converter.js) 和 [webview.js](https://github.com/hyq2890-blip/netdisk-ai-notes-importer/blob/27ab3bd8e1d06710f255bb9d56ff1490576782d6/src/webview.js)。

### 3.1 课程公式和表格的转换值得保留

`mathMarkdownFromNode` 优先读取 Quill/KaTeX 中的语义 TeX，而不是拼接渲染后的 MathML 与 HTML 文本。`simplifyQuillTables` 会在表格转换时保留公式；新版还修复图片后列表被误作代码块的问题。对于数学、技术和操作课程，这比直接抓取 `innerText` 更能保留可用信息。

这些行为在 [CHANGELOG](https://github.com/Tyukeying/baidu-course-notes-importer/blob/5cd6dd9f028c9d34638b94c650dc199a17dc45d0/CHANGELOG.md) 的 0.5.10、0.5.11、0.5.14 中有记录，实际实现见上述转换函数。

### 3.2 导入链路没有主动保存完整媒体，不等于整个宿主没有媒体缓存

核查的笔记与字幕导入路径读取平台已经提供的文字、字幕和图片资源，未见保存整片视频、抽出完整音频或运行本地 ASR 的步骤。但插件会打开视频页面，部分回退提示要求短暂播放。因此，仅靠这些源码不能证明 Electron/Web Viewer 的全部缓存行为符合用户的存储约束，真实样本仍应检查运行期间的媒体文件与缓存增长。

## 4. 不能照搬的缺口

本节均以固定版 [main.js](https://github.com/Tyukeying/baidu-course-notes-importer/blob/5cd6dd9f028c9d34638b94c650dc199a17dc45d0/main.js) 为依据。

### 4.1 五条字幕不能证明全文，候选排序也受到这一判断影响

`hasCompleteTimedSubtitles` 仅检查字幕数量至少为 5。候选排序先区分是否达到该数量，再比较来源优先级；外层 `collectOnlineSubtitles` 取得符合条件的候选后就退出重试。

结果是：高优先级来源的少量片段可能压过低优先级来源的长稿。只修改最后的“完成”标签不够，候选选择和终止重试的条件也要调整。

建议分别记录来源可信程度、目标视频身份、媒体时长、字幕时间范围、尾段证据和可疑空洞。字幕之间没有文字可能是静音，不能把每个间隔都当作漏转写；开头和结尾都有字幕也不能自动证明中间完整。无法证明的部分应显示“待核验”，不能被界面标题“完整字幕”掩盖。

### 4.2 批量入口没有使用完整的单篇组合流程

`importAllOpenNotes` 的范围是当前已打开的 FCB 笔记页，它调用 `importWebview`。后者没有调用字幕收集。另一个 `importFcbWebviewWithSubtitles` 才会收集字幕并把笔记与字幕合并。

因此，不能把“支持批量导入”与“支持带时间戳字幕”组合宣传为“已支持整盘批量全文”。Worldanalysis 的队列应复用同一个单视频处理函数，单篇和批量执行相同的验收与发布步骤。

### 4.3 图片失败不会阻止整篇成功，也没有证明图片就是关键帧

`localizeImages` 中单图失败仅记录警告，笔记仍可提交。命中已有稳定文件名时直接复用，也没有重新证明本地图片未损坏。

图片的 20 MB 检查发生在响应 `arrayBuffer` 已取得后，不能当作网络读取阶段的严格内存限制。后续适配应按单图记录预期、已取得字节、可解码状态、哈希及失败原因。

这些图片来自平台笔记的 `img` 元素。它们可能包含原视频截图，但该导入器没有对原视频解码抽帧，也没有逐图验证源视频时间。因此应先标记为“平台笔记附图”；只有具备相应证据时，才标记为“已核验的视频帧”。没有时间戳时留空，不从相邻段落猜一个时间。

### 4.4 按标题追加的同步不能承担内容补洞

`mergeMissingHeadingSections` 按标题树寻找同级同名章节。发现相同标题时，只继续比较子标题；不存在时才追加章节。已经存在的标题正文不会刷新。

这种行为有利于保护用户修改，但不能同步远端修正的文字，也不能修复已有章节中的缺句、漏图、坏图。同名章节还有误合并可能。

Worldanalysis 宜保留原始证据版本，并将机器生成展示区与人工批注分开。机器区没有人工改动时可以重建；发现人工改动时保留原文并生成待处理差异。不要把这个标题合并算法称为已有的“精确补缺”。

### 4.5 缓存匹配和路径身份需要加强

`getFcbIdentity` 优先使用 FCB 的 fsid，其次使用路径；`sameVideo` 主要比较 URL 或路径。缓存路线的 `matchAiNoteToSubtitle` 使用标题相似程度和修改时间评分。`matchTranscriptToCues` 则采用长度、开头和几个中间片段来匹配可见文稿与缓存 SRT。

后者比只比文件名更可靠，但仍不是完整身份凭证或全文验收。前者对重名课程、不同账号、移动改名后的文件尤其不足。

建议接入 Worldanalysis 现有的可信来源身份与允许目录校验，使用已验证的网盘文件身份关联笔记、字幕和图片。标题、日期和页面上的候选链接只能帮助寻找资料，不能单独决定最终对应关系。模糊缓存匹配保留为候选，不能直接计入课程完成数。

### 4.6 普通 Markdown 中的 `#t=` 不保证百度播放器定位

插件注册了 Markdown 后处理器和编辑器扩展，会拦截时间戳，调用 `seekFromNote`、`waitAndSeek`，再控制 WebView 中的播放器位置。

因此，普通 Markdown 可以呈现文本、图片和来源链接；没有对应插件控制时，不能只凭 `#t=` 地址承诺百度视频自动跳到某一秒。基础导出可保留时间值和原视频引用，并明确播放定位依赖已验证的适配器。

## 5. 建议的职责划分

本节是接入建议，不是对现有功能的宣称。

| 层次 | 承担职责 | 不应成为其职责 |
|---|---|---|
| Worldanalysis 核心 | 文件与课程身份、允许目录、任务队列、证据保存、验收、补缺和恢复 | 为了呈现而依赖某个 Obsidian 窗口或标签始终存在 |
| 百度采集适配器 | 在已有授权范围内读取笔记、字幕、图片，返回来源与失败信息 | 自行判定整课完成，或产生另一套权威任务状态 |
| Obsidian 导出 | 生成课程首页、逐视频页面、图片引用、来源和缺口 | 用摘要替代全文，或把未核验附图标为真实关键帧 |
| 可选 Obsidian 薄插件 | 页面采集入口、原视频定位、与核心状态交互 | 把整个采集和分析系统重写到插件 `data.json` 中 |

初版可直接输出普通文件，不必新增服务端口、安装插件或引入另一套数据库。需要在 Obsidian 内一键采集时，再接薄适配器；同一资料合同也应允许已有浏览器采集方式使用。

## 6. 采集桥接的最少信息

这些字段应映射到现有模型；只有现有模型不能表达时才补字段，不要求新建一套平行存储格式。

| 对象 | 最少信息 | 目的 |
|---|---|---|
| 视频来源 | 可信文件身份、允许路径、标题、时长、原视频引用 | 防止跨课程误配，并为覆盖检查提供依据 |
| 文案 | 类别、取得方式、来源版本、文本或时间段、完整性状态 | 区分字幕、普通文稿与 AI 笔记 |
| 图片 | 相对路径、哈希、格式、尺寸、来源类别、可选时间、核验状态 | 图片真实落盘；区分附图与源帧 |
| 采集结果 | 时间、适配器版本、成功/部分/失败、缺口、重试原因 | 为续跑和课程进度提供真实依据 |

来源引用不应把临时签名地址、Cookie 或令牌写进可分享的 Markdown。播放身份、证据来源与用于本次请求的短期授权应分开保存；复用现有脱敏和授权边界。

## 7. 实施顺序与验收重点

1. **先交付现有资料的 Obsidian 呈现。** 每视频一页，图文、出处、状态和缺口明确；课程首页能够进入各视频。图片使用本地路径，不能依赖临时图片网址保持有效。
2. **接入已有笔记、字幕和图片读取。** 借鉴上述小模块；单篇与批量使用相同处理入口。读取已有内容与触发平台新生成继续区分，不因打开某页就擅自改变采集策略。
3. **补实际缺口。** 校验文字身份与覆盖、图片可解码与缺图状态；重复导入不重复创建；更新保留人工批注，冲突可见。
4. **保留无现成产物的路径。** 没有平台资料时继续通过获授权的流式处理补齐；新的 Obsidian 呈现不能代替远程媒体入口的实际实现。
5. **再验证播放定位。** 在目标 Obsidian 与百度页面上确认时间戳行为；未验证前只承诺来源可追溯与本地资料可读。

应使用真实课程检查“文案能用、关键步骤有图、所有已列出图片可打开”，并观察处理期间是否生成完整媒体文件。单纯文件存在、字幕条数或截图条目数，都不能代替这项验收。

## 8. 许可与验证口径

### 8.1 真正引用或移植代码时保留声明

定制分支与上游均使用 MIT。定制分支的 `THIRD_PARTY_NOTICES.md` 明确列出上游项目，以及打包的 Turndown 和 `turndown-plugin-gfm`。

若复制、修改或分发这些实现，保留相应版权、MIT 许可与第三方声明，并记录来源版本。只采用设计思路时，也宜在开发文档中记录参考来源，避免日后把改写代码误认成上游原版。

来源：[LICENSE](https://github.com/Tyukeying/baidu-course-notes-importer/blob/5cd6dd9f028c9d34638b94c650dc199a17dc45d0/LICENSE)、[THIRD_PARTY_NOTICES.md](https://github.com/Tyukeying/baidu-course-notes-importer/blob/5cd6dd9f028c9d34638b94c650dc199a17dc45d0/THIRD_PARTY_NOTICES.md)。

不要直接把打包 `main.js` 嵌进 Worldanalysis 核心。应抽取必要转换或适配逻辑，保持 Obsidian API、网页操作与业务数据之间的边界，并保留实际移植部分的来源说明。

### 8.2 参考项目测试不是用户网盘实测

参考仓库的核心测试大量使用 Obsidian stub、模拟 DOM、缓存 fixture 和 VM 加载。它们覆盖转换、身份关联、错误处理和部分提交顺序，适合借鉴回归场景；不能证明当前百度页面、用户账号权益、整课文字覆盖或运行期间缓存行为已经通过。

来源：[plugin-core.test.mjs](https://github.com/Tyukeying/baidu-course-notes-importer/blob/5cd6dd9f028c9d34638b94c650dc199a17dc45d0/tests/plugin-core.test.mjs)。本研究没有运行这些测试。Worldanalysis 本分支实际新增内容及其验证结果，应由对应实现和交付记录单独说明。
