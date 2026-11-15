# 百度课程资料接入与 Obsidian 呈现

日期：2026-11-15。研究基线：Worldanalysis `45d436cdf911b6d7119010401b89a01e30e3f146`。

## 结论与本轮交付

采用同一条交付链：**复用已有文字和图片 → 发布可追溯证据 → 在 Obsidian 阅读与批注；缺失的语音和画面由后续获授权的流式采集补齐。**

参考 `Tyukeying/baidu-course-notes-importer` 的资料快照、字幕发现、图片去重、来源关联和用户内容保护方法。保留 Worldanalysis 已有的 Python、SQLite 和不可变 Bundle，不把任务台账搬进 Obsidian，也不另建网站或第二个数据库。

本分支增加两个独立入口：

| 入口 | 解决的问题 | 真实边界 |
| --- | --- | --- |
| `worldanalysis.platform_materials` | 把已有本地字幕、文稿、平台笔记和图片按稳定资料身份接入现有 Store | 离线资料接入；不会登录百度、读取网页、请求 AI 生成或取得媒体流 |
| `worldanalysis.obsidian` | 把已发布 Bundle 导出为课程入口、逐素材资料页、个人笔记和本地图像 | 只读证据的呈现出口；不会更改采集完成状态或分析阅读回执 |

这两项连接了“已有材料”与“可阅读结果”。**它们不代表百度网盘在线采集已经打通，更不代表完整课程已经交付。** 真实课程的阻断和下一项工作见 [03_ONLINE_BRIDGE_HANDOFF.md](03_ONLINE_BRIDGE_HANDOFF.md)。

## 用户要求与执行边界

允许读取完整媒体时间轴，允许网络传输；不新增完整视频或完整音频文件，包括先下载整片再删除。最终主要保存全文、真实关键帧，以及少量来源、时间、状态和恢复索引。

已有资料优先，但不能要求视频预先拥有 AI 笔记。没有现成资料时，仍需要某个处理端完整检查音频，并按内容检查画面。摘要不能替代全文；平台附图不能在没有来源证据时冒充原视频帧。

本次本地资料接入延续默认策略：不发起新百度 AI 生成，不外发材料，不引入商业调用。网盘来源声明限于现有授权根 `/apps/教育分析学习/`，但路径声明本身并不构成实际读回验证。

## 为什么不直接装参考插件就宣布完成

[研究记录](01_IMPORTER_RESEARCH.md) 对固定版本源码逐项核对。最需要修正的是：

- 参考实现用至少 5 条字幕判断“完整”；这不能证明全文，还会影响候选选择及停止重试的时机。
- 批量入口处理已打开的笔记页，没有使用单篇“笔记＋字幕”的组合入口，不能推出整盘批量全文能力。
- 笔记附图未经原视频身份和时间核验；图片失败只警告，也不能判整套资料齐全。
- 按标题追加内容的同步不会刷新相同标题下修正的正文，不能承担精确补洞。

可复用的是小模块和设计方法。本分支没有复制打包 `main.js`，也没有引入 Obsidian 或 Electron 运行依赖。

## 本地使用

以下示例使用 PowerShell。`$py` 指向已安装现有最小依赖 `jsonschema`、`Pillow` 的 Python；从包含 `worldanalysis/` 的源码目录运行。`G:\Worldanalysis` 是示例工作区，按实际位置修改。

### 1. 创建或选择项目

```powershell
$py = ".\.venv\Scripts\python.exe"
$waRoot = "G:\Worldanalysis"
$waVault = "G:\WorldanalysisVault"
New-Item -ItemType Directory -Path $waRoot -Force | Out-Null

# 项目只需创建一次；已有项目请直接使用其 ID
& $py -B -m worldanalysis --root $waRoot create-project --project-id course-demo --topic "课程资料"
```

Vault 必须与源码目录、证据工作区分开，不能建在其内部，也不能把两者放进 Vault。示例使用同级文件夹。这样 Obsidian 的编辑和同步不会直接改写正式证据。

### 2. 为已经取得的文字和图片创建资料清单

把资料放在工作区内，例如 `手动投放/课程资料/lesson-01/`。下列 `manifest.json` **只是格式示例，不是真实网盘采集记录**：

```json
{
  "schema_version": "1.0",
  "material_id": "course-demo-lesson-01",
  "title": "第 1 节：已有资料示例",
  "source": {
    "provider": "baidu",
    "file_id": "example-file-id",
    "path": "/apps/教育分析学习/示例课程/第1节.mp4",
    "duration_ms": 120000
  },
  "transcript": {
    "path": "transcript.srt",
    "kind": "timed_subtitle",
    "origin": "asr",
    "source_method": "existing_platform_subtitle_export"
  },
  "note": {"path": "note.md"},
  "images": [
    {
      "path": "images/001.png",
      "source_kind": "platform_note_image",
      "timestamp_ms": 15000
    }
  ]
}
```

各路径相对 `manifest.json` 的目录，不允许绝对路径、跳出目录或符号链接。没有字幕、笔记或图片的字段可以省略，但至少应声明一类资料；不用为凑格式生成虚假空文件。

| 字段 | 约束与意义 |
| --- | --- |
| `material_id` | 稳定资料 ID，同一节课更新时保持不变；不要用会改变的标题作身份 |
| `source.file_id` / `path` | 本地资料声明的原视频身份；本入口不会将其升级为“已验证百度来源” |
| `transcript.kind` | `timed_subtitle` 配 SRT/VTT；`plain_transcript` 配 TXT |
| `transcript.origin` | `asr` 或 `native_extract`；如实记录取得方式，不把 AI 笔记填入此字段 |
| `note` | 平台笔记，保持 `provider_ai_note` 来源，与文案区分 |
| `images[].source_kind` | `platform_note_image` 或 `claimed_video_frame`；后者仍是声明，不自动获得已核验关键帧身份 |
| `timestamp_ms` | 只有已知实际对应时间才填写，不知道则省略或填 `null` |

凭据、Cookie、带签名临时链接不进入资料清单。笔记允许 `![说明](images/001.png)` 这类相对本地图片语法，但目标必须列入 `images`；原语法按字面保留，实际图片由附件证据呈现。本入口不抓取外链，拒绝远程图片、HTML 资源嵌入和未声明图片。参考插件的原始 HTML 或带临时链接的 Markdown 需要先由采集适配器转换，不能声称任意导出都能直接导入。

```powershell
& $py -B -m worldanalysis.platform_materials `
  --root $waRoot --project course-demo `
  --manifest "$waRoot\手动投放\课程资料\lesson-01\manifest.json"
```

默认资料包总上限 64 MiB、单文本 8 MiB、单图 20 MiB。导入保留附件内容哈希和缺失状态；修正文稿或补上缺图会形成新修订，重复输入复用已有结果。图像需要实际可解码；没有取得的图片仍为缺口，不按清单条目数算成功。

本入口处理的是一个本地资料包，来源绑定保持 `local`，原视频覆盖保持待核验。返回 `partial` 并不表示文本不可使用，而是提醒“已有材料可读”和“源视频全部资料已齐”是两个不同事实。

### 3. 导出 Obsidian

```powershell
& $py -B -m worldanalysis.obsidian `
  --root $waRoot --project course-demo --vault $waVault
```

默认读取项目最近发布的 Bundle；可用 `--bundle <真实Bundle-ID>` 指定已发布版本。**最近一个 Bundle 不等于自动聚合整个课程。** 若逐视频导入得到多个 Bundle，先选择各视频当前有效版本并用现有 `compose` 聚合，再导出返回的组合版本：

```powershell
& $py -B -m worldanalysis --root $waRoot compose `
  --project course-demo --bundle "<第1节的Bundle-ID>" --bundle "<第2节的Bundle-ID>"

& $py -B -m worldanalysis.obsidian `
  --root $waRoot --project course-demo --vault $waVault --bundle "<组合Bundle-ID>"
```

尖括号均为占位符，应替换为实际返回 ID。不要把同一资料不同版本未经选择地混为一门课。

在 Obsidian 选择“打开文件夹作为仓库”，打开 `$waVault`，再进入命令返回的 `overview_path`。普通 Markdown 和本地图片即可阅读，不要求安装参考插件。此处未在用户电脑执行安装或 UI 验证。

| 导出内容 | 用途 |
| --- | --- |
| `Worldanalysis/project-<稳定标识>/课程总览.md` | 当前选定 Bundle 的素材目录、范围和缺口 |
| `资料/<稳定标识>/资料.md` | 全文内容块、来源、时间和对应图片 |
| `资料/<稳定标识>/我的笔记.md` | 首次创建后由用户维护，后续导出不覆盖 |
| `附件/<SHA256>.<格式>` | 实际本地图像；相同字节复用一份，保留出现位置 |
| `worldanalysis-export.json` | 导出版本、受管文件哈希和冲突记录；不是第二套业务台账 |

程序页改动前检查上次生成哈希。检测到人工修改时保留原稿，提供待合并候选；不会把相同标题正文当作不可变化的依据。导出时避免同时编辑正在更新的程序页：跨进程的哈希检查不能充当 Obsidian 的全局编辑锁。个人笔记无需放进程序页。

首版不自动追踪用户移动、重命名程序管理文件，不提供全面双向同步，不自动删除历史视图。固定导出目录中保存机器资料，把个人组织和学习状态放在个人笔记中。

## 研究与验证入口

- [参考项目设计和源码核查](01_IMPORTER_RESEARCH.md)
- [Obsidian 呈现设计与官方依据](02_OBSIDIAN_PRODUCT_DESIGN.md)
- [真实在线采集桥接与验收交接](03_ONLINE_BRIDGE_HANDOFF.md)
- [本分支实际验证记录](04_VALIDATION.md)

验证记录分别说明代码测试、本地合成资料和真实网盘能力。没有真实来源结果时，不使用“整课完成”“全文保证”或“全核心画面已覆盖”作为结论。
