# Worldanalysis 课程教材生产开发文档

给定一个已经下载的课程文件夹，Worldanalysis 对原件做独立入库，准备转写、附件和观察画面，由整理者逐集对照原视频、撰写教材、走查实际步骤，再由脚本验收和发布。每个项目在日常 Obsidian 库沿用原项目目录，只有一篇以项目名命名的教材 `课件/<项目名>.md`。项目入口和交付边界见[项目规则](../AGENTS.md)。

教材保留原课讲次，讲内按学习问题组织。直接说明方法和适用条件，自然引导学习者操作，给讲师亲历或课堂主张明确归属，整理者新增的演算注明示例。图片紧跟它能解释的动作或关系。段落与原图的出处保存在 Markdown 注释里，原片和附件入口放在文末默认折叠的「原课与配套资料」。本地入口由现有的[课程整理 Skill](../.agents/skills/course-material-archive/SKILL.md) 接续，编辑判定见[写作流程](../.agents/skills/course-material-archive/workflow.md#本地教材工作流)和[验收说明](../.agents/skills/course-material-archive/acceptance.md#本地教材验收)。

~~~mermaid
flowchart TD
    A["课程原件文件夹"] --> B["start · 入库与画音、附件准备"]
    B --> I["read · 对照音画 · 记录逐段要点"]
    I --> C["编写教材与取原图"]
    C --> D["preview · 编译带图预览"]
    D --> E["只读跟做并填写验收记录"]
    E --> F["accept · 核验同版证据和隔离阅读"]
    F --> G["deliver · 在原项目目录更新项目名.md"]
    G --> H["verify-reader · 重开同版 · verify --require-vault"]
    E -->|发现缺项| C
~~~

## 1. 从课程文件夹开始

在 Worldanalysis 根目录打开 PowerShell。源文件夹可在其他盘。路径中有空格时加引号。入库复制原始文件并比较 SHA-256，保留输入目录供以后核对。默认输出为 `整理结果/课程教材/<原件文件夹名>`。同名课程可用 `--output` 指明独立包目录。

~~~powershell
$courseTool = '_tools/course_production/course.ps1'
& $courseTool start 'D:\课程原件\一门课'
& $courseTool start 'D:\课程原件\另一门课' --output 'G:\课程教材\另一门课'
~~~

`start` 自动完成原件盘点、附件读取、视频转写和画音候选扫描。随后从它回报的课程包继续。重复 `start` 会核对输入和入库快照，复用可用的处理结果。拿到已有课程包时可直接从快照续作。

~~~powershell
$course = 'G:\Worldanalysis\整理结果\课程教材\一门课'
& $courseTool status $course
& $courseTool prepare $course
~~~

`status` 列出课程包和现有产物。`verify` 核实课程包和日常库的交付状态，`verify --require-vault` 要求当前版本已经在日常库重开验收。课程文件发生变化时，为新输入建独立包，或者恢复已登记的快照来续作旧包。

### 运行环境

- **Python 3.12**。Windows 包装脚本优先使用 `WORLDANALYSIS_PYTHON`，再尝试仓库内的 `.venv` 或 `py -3.12`。也可直接用兼容解释器执行 `_tools/course_production/course.py`。项目本地模块和模型从 `.runtime` 读入。安装或更新第三方依赖时用项目锁定版本。 [依赖清单](../requirements-lock.txt)
- **视频**。需要 PATH 中的 `ffmpeg`、`ffprobe`，以及 PyAV、OpenCV、Pillow、NumPy。CPU 音频转写和人声检测使用 `.runtime/course-method-sensevoice/model` 的已校验 SenseVoiceSmall、词表和 Silero VAD。
- **附件**。PDF 用 pypdf 和 pypdfium2，扫描页和常规图片由 RapidOCR 辅助。旧 DOC 需要 Node.js 和现有的本地 `word-extractor`。提取状态和缺口保存在文档记录。图片识别的数值与小字回原图核对。
- **阅读验收**。需要支持 WebSocket 的 Node.js 和桌面 Obsidian。用 `accept --obsidian-exe <路径>`、`verify-reader --obsidian-exe <路径>` 或 `OBSIDIAN_EXE` 指定安装位置。Windows 也会检查本机常见安装目录。两项阅读核验均在独立的 Obsidian 环境完成，先核课程包，再核日常库实际交付的同版文件。
- **可选 GPU**。用已安装的 CrispASR Vulkan 和本地权重。缺省 CPU 路径可以继续完成。传入 GPU 路径时同时给出对应权重。现有依赖和模型可通过 `WORLDANALYSIS_COURSE_RUNTIME` 指向已准备的本地运行目录。无音轨、无同版补录的项目可以独立运行视觉路径。

新检出仓库时，在兼容的 Python 3.12 环境安装项目锁定依赖与 `sherpa-onnx==2.0.1`。将 `model.int8.onnx`、`tokens.txt` 和 `silero_vad.onnx` 保存在 `.runtime/course-method-sensevoice/model/`。转写与候选扫描会校验模型文件。旧 DOC 处理另需在 `.runtime/course-method-doc` 安装 `word-extractor@1.0.4`。 [SenseVoice 模型与词表](https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/tree/2365baeacb507f821a0c8120fcee3d484dba7a07) · [Silero VAD 模型](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx)

## 2. 课程包与所有权

~~~text
<课程包>/
  原始资料/                    原始文件快照，保留相对路径
  课件/
    00 开始学习.md              课程包内部的验收文件，发布时映射为项目名
    原图/                      供教材使用的原片截图或附件图
  .course/
    project.json                输入身份
    work/
      讲次顺序.json              讲号的阅读顺序
      <讲号> 转写.md              页内快速定位用
      音轨取材/                  保存已解码并识别的长轨窗口
      采用转写/                  当前采用稿与复识采用依据
      连续阅读/                  按时间窗组合当前证据
      逐段要点/                  从原课独立整理的清单
      资料/                      按来源提取的原文、结构、图片及缺口
      媒体补查/                  每集画音候选和联系表
      视觉核查/                  按时间排列的原帧、OCR 和缺口
      源片段复核/                按原时间轴取的小窗，用于辨认短动作
      选用素材/                  已登记的工作原图
      教材.md                    编辑者的单篇稿
      阅读预览/                  当前带图阅读稿和版面回执
      日常库接口验收/             日常入口同路径、同版重开的回执
      验收填写模板.json          由当前预览生成，供人填写
      验收记录.json              编辑者核对原件和只读走查后的记录
      补充内容复核.json          有独立配套条目时登记正文短句与同段原件位置
    ledger/
      intake.json               原件清单、尺寸及 SHA-256
      inventory.json            本课程的讲号与附件身份
      media-summary.json        视频时间轴索引
      documents-summary.json    附件编号及提取状态
      figures.json              每张图的原件、时间或页码、画幅和裁剪
      publications/<SHA>.json   按发布正文指纹存放的版本回执
~~~

课程包用自己的输入清单和状态定位每个阶段。教材内图片和来源链接相对 `课件` 解析，搬动整个包仍然可以检查。`.course` 放编辑缓存和验收证据，读者从教材文件进入。正式发布前会保留旧教材可用，验证完选用图片和当前预览后，以替换教材正文作为提交点。

## 3. 证据准备与模型路线

### 原字幕、转写和语音候选

1. `prepare` 依据稳定的文件路径和哈希建立原片编号。顺序缺省按相对路径自然排序。通过目录、原课开头和内容衔接确认真实教学顺序。调整 `讲次顺序.json` 时，每个稳定编号出现一次。工作稿标记和原帧清单继续用稳定编号，输出的章节、段落出处、配图出处和文末索引使用该编号在数组中的阅读位置。校验时用同一原片的实际时间和身份。
2. 读取同名 SRT/VTT 或唯一内嵌文字字幕，保留独立原字幕轨。有可用音轨时解码全音轨并生成 ASR，保存实际 PCM 样本范围。原字幕与 ASR 各自保留，ASR 没有识别文字时保留可用原字幕。CPU 版在停顿附近分段，并记录音轨起点偏移。若 `.srt` 以 Markdown 标题和分节开头，读取整份说明和其中的时间示例。在附件台账标明实际内容为 `md`，这份说明退出字幕匹配。
3. Silero VAD 找转写未覆盖的人声。约 0.45 秒以上的未覆盖区间纳入初筛，阈值以下另记。对照同区间的原字幕和 ASR，关注参数、否定、短指令和分块交界。候选为空仍要按片段看字幕、画面、可能提早结束的音轨和相关附件。
4. 音轨缺失时，先找包内同版有声视频。使用 `voiced_reference` 时脚本比较两版时长、13 个沿时间轴分布的原画及时间戳，只在画面和时间轴符合时采用有声件并保留两份身份。原视频有可用音轨时沿用原音轨。

需要控制字幕或音轨时，可在 `.course/work/媒体设置.json` 写讲号键。`audio_track` 是从零开始的音轨序号，`subtitle` 是课内原件相对路径，显式 `null` 跳过文字字幕匹配。`language` 可选 `auto`、`zh`、`en`、`ja`、`ko`、`yue`。改完运行 `prepare`。

~~~json
{
  "1": {
    "language": "zh",
    "audio_track": 0,
    "subtitle": "第一讲.zh-Hans.srt"
  }
}
~~~

GPU 初稿用 `prepare` 或 `start` 的 `--gpu-exe` 与 `--model`。先用 CrispASR `--diagnostics` 核对 Vulkan 构建和实际显卡。可以 `--device` 指定诊断中的序号，省略时选择诊断出的最大显存设备。SenseVoiceSmall GGUF 使用 30 秒分段和零重叠。每 600 秒保存已完成的长轨窗口供续作。GPU 运行成功后课程包记住运行配置，`prepare --cpu` 可显式切回 CPU。

CPU 和 GPU 都从 FFmpeg 的 PCM 解码流取样。GPU 路线由这些原始解码样本写出识别器所需的临时 WAV，避免封装阶段把转换后的时间戳抖动报为音轨损坏。仍要查看窗口的起点、实际样本数和原片声音。跨窗听辨有疑问时，可用 `refine --range 起秒 止秒` 取重叠区间复识。

CrispASR 成功退出且明确报告某个输入音频没有语音时，对相应窗口保存已解码采样数和空转写。人声检测继续独立核查，结合音乐、短动作和同屏变化判定用法。缺输出且该窗口无明确提示时仍需排查识别失败。

### 幻灯片内嵌视频

`start` 和 `prepare` 检查 PPTX、PPTM 的包内视频成员，按幻灯片关系记录播放页。同一附件里指向同一成员的多个播放关系保留一条原视频记录，并登记全部可定位的页。没有页关系的媒体保留成员定位，仍需判断用途。父附件和视频成员各留 SHA-256。视频解包到工作区的 `附件内嵌视频`，继续走原片探测、全音轨转写、画面扫描和取图。幻灯片自身的文字、版面和原图继续走附件处理。

编号先分给独立原片，内嵌视频随后编号。旧课续作用 `prepare` 加入新发现的内嵌编号，再按课内播放位置调整 `讲次顺序.json`。例如讲义页正在演示滤镜操作，教材配图和说明放在讲到该步骤的位置，折叠出处链接到原讲义并标注播放页。正文到附件和原片区间都要能反查。

`status` 可查看内嵌来源，验收的 `episodes` 中有 `embedded`。预览和复验以原附件成员和本地提取视频的指纹核对范围。`verify` 的 `original_files` 只计收到的实体文件，`videos` 包含已核的内嵌视频，`embedded_videos` 给出其中的数量。外链或没有随附的录像在附件复核中记录实际可读范围。

### 原视频损坏和可核查的恢复副本

原视频无法探测时，`start` 或 `prepare` 继续生成其他视频和附件的成果，保留原讲号，并在 `media-summary.json` 的 `unreadable` 中登记文件、摘要和探测错误。`status` 显示待补视频。整课预览和验收需要有可读的来源。若找到同一课的完整可读原件，用新来源快照做后续核对，已有核验结果按文件身份续用。

在额外副本上做恢复时，核对原件指纹、音视频包的来源、全段解码、关键画面和实际恢复的声音。确认可作为来源后，在 `.course/work/媒体恢复` 保存可读副本和同目录 JSON 清单，并用 `媒体设置.json` 的讲号键指向该清单，例如 `{"3":{"recovery":"03.json"}}`。清单字段包含 `original_sha256`、相对该目录的 `file`、恢复文件 `sha256`、恢复方法 `method` 和一行说明 `scope`。说明写出哪些画面和音轨可以查到，以及时间位置的依据。把它放到对应章节的正常正文中。

重新运行 `prepare` 后，转写、补查、取图及预览按同一份核验副本和原始身份运行。恢复前的原件在清单和文末保留原名，文末出处注明是恢复定位。接受整课时同时核对恢复清单和文件摘要，换副本后需要重新核验。缺失 MP4 索引可在工作副本上用工具试验，播放成功后还要核对实际像素和声音。参见 [untrunc 官方项目](https://github.com/anthwlock/untrunc)。

~~~powershell
& $courseTool prepare $course --gpu-exe 'G:\本地运行包\crispasr.exe' --model 'G:\本地模型\sensevoice-small-q8_0.gguf'
& $courseTool refine $course --gpu-exe 'G:\本地运行包\crispasr.exe' --model 'G:\本地模型\ggml-large-v3-turbo-q5_0.bin'
& $courseTool refine $course --backend qwen3 --lesson 1 --range 120 162 --gpu-exe 'G:\本地运行包\crispasr.exe' --model 'G:\本地模型\qwen3-asr-0.6b-q4_k.gguf'
& $courseTool read $course 1 --start 120 --end 162
& $courseTool adopt $course 1 'G:\课程工作\采用决定.json'
~~~

`refine` 保存第二识别路线及其时间窗记录。不给 `--range` 时从人声候选定位，给定范围也可复识原字幕或转写内部的争议。环境变量可设 `WORLDANALYSIS_COURSE_ASR_EXE`、`WORLDANALYSIS_COURSE_SENSEVOICE_MODEL`、`WORLDANALYSIS_COURSE_QWEN3_MODEL` 和 `WORLDANALYSIS_COURSE_WHISPER_MODEL`。

`read` 按时间窗口输出采用稿、原字幕差异和画面候选。可用 `--observations` 带入工作目录中的观察 JSON，输出对应的 OCR 文本。`adopt` 的输入含当前底稿 `metadata_sha256` 与不重叠的 `windows` 数组。每个窗口含 `range_s`、`reason`、`proofs` 及替换后的 `cues`。依据记录工作区相对 `path` 和 `sha256`，应引用保留版本的原片观察、附件或复识历史。每条字幕含 `start`、`end`、`text`，只在确认时写回并使相关正文复核。采用区间须包住被替换字幕的整句。底稿或采用依据变化后，先回源核对该区间再更新决定。

### 画面与关键帧

媒体扫描按原片呈现时间解码。常规横屏已有转写每秒约 4 次低清比较，竖屏或无转写每秒约 6 次。全局变化用于定位换页和成品确认。10×8 区域变化和红色标记用于定位小控件、圈点、进度及菜单，也覆盖近黑画面的局部变化。停留画面有周期候选，语音中的动作词用于补定位。转写条目常包含口播和停顿，媒体回执用实际人声活动另标出 `outside_speech` 候选。结合原片判断这些画面有无独立的操作、演示或结果。

依原时间轴查看候选和原片。对短菜单、同屏圈点、滑动和纯画面操作，重点看动作前、动作发生时、动作后的结果。用 `window` 在候选前后连续解码，适应短时状态、小范围裁切及非编码关键帧。用 `keep` 登记要插入正文的原始像素。上一步的结果图与下一步使用时的输入状态尽量连贯。只有字幕或画面无法说明步骤时，按原话和原附件取证。

~~~powershell
& $courseTool window $course 1 12.4 15.2 --label '展开菜单' --step 0.1
& $courseTool window $course 1 12.4 15.2 --label '检查局部' --crop 100 150 960 740
& $courseTool observe $course 1 60 120 --step 0.5 --crop 220 930 1650 1040 --ocr
& $courseTool keep $course 'G:\课程工作\取图清单.json'
~~~

`observe` 返回按原始呈现时间保存的 PNG 和核查记录。按实际画幅选字幕带，OCR 按采样点定位文字变化，连着出现的同一文字合并成时间区间。OCR 失败、难辨数值或术语回原像素复核。静音操作先查状态前后，正文只讲能确认的动作与结果。

`keep` 的 JSON 清单每项有图名和一个来源。视频用稳定讲号及原片秒数。文档用编号和从 1 开始的页码，PDF 可选 `scale`。PPTX 的文字、标记和嵌图需要一起保留时，加入 `"full_slide": true`。Windows 上使用已安装的 PowerPoint 只读打开原附件，完整渲染指定页。预览重渲染并核对实际像素；运行环境没有 PowerPoint 时可继续用已有内嵌原图方式，另登记完整页核查缺口。`crop` 是图像左、上、右、下。选择图片文件名称时省去 `.png`。同名已选图保持同一原件与像素，替换时用新名称。

~~~json
[
  {"name": "展开比例菜单", "lesson": 1, "time": 13.25, "crop": [100, 150, 960, 740]},
  {"name": "核对说明页", "document": "D001", "page": 1, "scale": 1.5},
  {"name": "比例判断示意", "diagram": {"file": "整理示意图/比例.png", "lesson": 1,
    "range_s": [12.4, 15.2], "basis": "原课比例设置的口头讲解", "caption": "比例选择顺序"}}
]
~~~

需要整理示意时引用工作区内已经画好的 PNG，给出原课秒数和依据。发布器将这类配图标注为「根据原课整理」。

### 附件

PDF 先读原文字和图片位置，遇到扫描或难提取页面再渲染、识别并查看原页。原表格保留工作表、单元格、原公式和单位，数值案例对照实际行。PPTX 读页面正文和原生资源，图表、文本框及标注都参与讲解时读整页。Word 和 XMind 按原节点或章节保留定位。原样片、压缩包及提取能力以外的文件也进入清点，按实际使用或缺口登记。每份提取不全的附件明确补读的页、图或表，以及带来的正文修改。无法恢复的内容记录具体位置和缺少的原件。

课程脚本用 Windows 自带的 libarchive `tar.exe` 或系统中的 `bsdtar` 读取 RAR 和 7z。成员以编号存入课程包的 `.course/work/资料`，由资料记录中的 `archive_members` 对应原路径、文件指纹、保留副本与提取结果。档内文字仍按工作表、段落等位置复核，图片读实际画面。特殊成员、加密、超限和无法读取的内容在资料记录中留下缺口。复跑 `prepare` 可补读旧版登记过的 RAR 或 7z，已有音画取材可复用。归档转换方式见 [libarchive 官方手册](https://github.com/libarchive/libarchive/blob/master/tar/bsdtar.1)。

本地 HTML 和 HTM 在 `prepare` 时离线提取静态正文、表行、链接地址和视听引用。资料记录保留行号及外层章节标识，动态模板和嵌入对象的画面或声音登记为复核项。从静态提取稿定位原文件，再看原始图片、媒体或有证据支持的页面状态。原文件随入库快照保存。

复核已有课程时，先比较原目录与旧入库范围，由原片内容确认新文件的课程身份、顺序和本段新增教学。对录制切分重叠保留两个原件，正文只讲一次。独立通读来源提取行业逻辑、盈利方式、应用、失败案例和条件关系，再用旧索引定位已有段落。最后反查正文每个数值、公式、条件和结果，验证同一来源及口径。讲师主张保留身份，外部核验和整理练习单独标明。

## 4. 编写、预览和验收

### 编写约定

在 `.course/work/教材.md` 写一个一级标题。有视频时每集一个二级标题，按 `讲次顺序.json` 排列。长讲用分节把学习问题、方法和示例连起来。理论说明原因与适用条件，公式先给变量及单位再演算，案例把选择与结果对应，操作紧贴可用的界面图。合并重复后对照原始要点，保留新增的数字、条件与例外。转写、取图、分卷与验收过程留在工作区，实际影响使用的条件继续写在正文附近。纯文档项目用二级标题组织原件里的任务。

写稿前先读各段原材料，把独立要点保存到 `.course/work/逐段要点/<两位讲号>.json`。文件为数组，每项填 `range_s`、`kind`（如「输入」「操作」「参数」「条件」「原例」「纠错」「结果」「画面」）和能回到原课的 `source_note`。对照旧稿时，重复教学可以合并，实际参数、例外和操作顺序需要保留。

每节保留可复制的设置和输入。带读用「我准备」「我打开」「我填写」「我确认」推进，把原例、可见结果和遇到故障时怎么修写在实际发生位置。各图紧跟动作段落，图后接后续动作。使用同一素材重新举例时注明为练习。

- 原视频出处 `{{01|00:12.4–00:15.2}}`。使用支撑正文的起止时间。
- 附件出处 `{{D001|第 1 页，尺寸设置}}` 或 `{{D002|Sheet1 C4:O6}}`。
- 在动作段落之后独立一段用 `{{img:展开比例菜单|550}}` 插入已登记的原图。需要多张时连续插入。描述图里要看的对象。
- 发布器将出处转换为隐藏注释和文末折叠资料。过程记录留在工作区。用原资料编号、时间码和实际章节验收正文。

### 审核记录与提交

~~~powershell
& $courseTool preview $course
# 读预览，逐集对照原片，核查附件；按模板填写 .course/work/验收记录.json
& $courseTool accept $course
& $courseTool verify $course
~~~

`preview` 编译图文稿并生成对应的 `验收填写模板.json`。将它另存为 `验收记录.json` 后填入实查结果。脚本预填的来源、哈希、时间轴和语音候选只帮助定位。编辑者填实际看到、听到和跟做到的内容。

| 记录 | 要填写的内容 |
| --- | --- |
| `snapshot` | 复制本次模板的正文、原图和证据指纹。换稿后使用新模板，对变更部分重新复核。 |
| `episodes` | 每个视频保留原件身份、讲号和对应二级标题。`watched` 按顺序列原片的真实时间范围、类别（教学、原例、说明、过场）和观察。看片记录覆盖全片首尾，包括静音、停留画面等。纯文档项目留空。 |
| `source_points` | 从独立逐段要点文件进入模板。补可见 `anchor`，并把该段隐藏出处放在同一正文块。原件本身缺细节时填 `source_limit` 并记实际影响。关键项未解决时保留编辑状态。 |
| `key_steps` | 每个关键步骤填 `range_s`（原视频起止秒数）、`anchor`（预览里能找到的可见动作短句）、`figures`（紧贴该动作的 PNG 名）。短句也出现在标题或其他段落时，验收优先以覆盖该步骤的段落出处定位。有画面的原图时间须在步骤区间内。纯口述步骤可用 `visual_note` 交代依据。 |
| `visual_basis` | 从「原片」「附件原图」「无可教学画面」选择实际依据，在 `visual_note` 说明可辨内容。适用时正文要有本集原帧或相应附件原图。 |
| `audio_checks` | 每个候选回听后记录「已补正文」「正文已涵盖」「非教学声音」「听辨缺口」之一及理由。遇到参数等关键缺口时写清可核实的值和适用条件。 |
| `checked_sources` | 可登记实际核查的局部观察、原图或复识证据。每项含工作区相对 `path` 与 `sha256`。 |
| `documents` | 每件附件有用途和实际核对说明。可用「引用」「用作转写」「参考」「重复」「原样存档」「提取缺口」。引用的附件回到相应页、表或章节。 |
| `follow_along` | 只读预览完成关键任务。每条填所在二级标题、准备/入口/设置/结果/纠错五项可见文字的定位、同章节隐藏出处、实际看到的 PNG 文件名。可用 `visual_note` 说明纯文字依据。 |

单个讲次的记录可以按下面的结构扩展。`watched` 的 `note` 写原片的实际观察。`anchor` 用预览中与配图紧邻、能唯一定位到这一步的原样短句。有多个片段时，为整集的各段补上对应观察。

~~~json
{
  "watched": [
    {"range_s": [0, 11.8], "kind": "说明", "note": "讲师打开画板并说明要检查输出规格"},
    {"range_s": [11.8, 24.6], "kind": "教学", "note": "展开比例菜单，选中设置并打开预览"}
  ],
  "key_steps": [
    {"range_s": [12.4, 15.2], "anchor": "我展开比例菜单", "figures": ["展开比例菜单.png"]}
  ],
  "source_points": [
    {"range_s": [0, 9.5], "kind": "说明", "source_note": "先确认输出规格",
     "anchor": "我先按交付尺寸选择规格", "source_limit": ""},
    {"range_s": [12.4, 15.2], "kind": "操作", "source_note": "从展开的菜单选比例",
     "anchor": "我展开比例菜单", "source_limit": ""}
  ],
  "visual_basis": "原片",
  "visual_note": "比例菜单中的选择项和应用后的界面可见"
}
~~~

`accept` 用同一版预览比对逐集时间、关键步骤邻图、附件去向和跟做证据，再在隔离的 Obsidian 阅读视图从开头滚到底，检查每个二级标题、全部图像加载、跳转、横向溢出、折叠资料和引文隐藏。巡查时限按正文长度和图片数调整，读到页尾前继续记录尚未核阅的章节与图片。本地图片和附件路径保留文件名中的英文逗号和 `@`，再以实际 Obsidian 打开检查。通过后写入课程包。源码定位与结构检查由脚本完成，是否忠实于原课、学员能否按内容做出产物由逐集核对和读者走查负责。

### 交付到用户日常 Obsidian 库

确认用户在用的库及本课既有目录。更新旧课沿用库内同一项目目录，并将唯一的教材放在 `课件/<项目名>.md`。课程包独立保存原件和生产证据，日常库保留教材、配图和折叠区用到的原件。首次绑定用 `deliver`。遇到旧入口正文不一致，先确认同课、保存用户修改，再按提示提供经核对的旧正文 SHA-256 给 `--reviewed-previous`。脚本先核对是否有指向旧笔记的链接，归档旧正文，校验已有配图及原件，补齐缺件后再更新教材。用户要求仅本地时，核实目标库的同步情况。

~~~powershell
& $courseTool deliver $course --vault 'G:\我的日常阅读库' --course-folder '一门课'
& $courseTool verify-reader $course
& $courseTool verify $course --require-vault
~~~

`deliver` 返回日常库实际路径 `entry`、Obsidian 直达链接 `uri`、正文指纹 `sha256` 和落库时间 `installed_at`。链接使用 Obsidian 支持的 `obsidian://open?path=...`，可直接从课单或验收记录打开。 [Obsidian 直达链接](https://obsidian.md/help/Extending%2BObsidian%2BURI)

`verify-reader` 从日常库中读取已交付的正文和链接文件，在独立 Obsidian 环境按该项目路径打开、关闭后重开，读到所有章节和文末，确认全部图片已加载、出处默认折叠以及页面无横向溢出。回执保存于 `.course/work/日常库接口验收/`，其版本摘要记录于 `.course/ledger/obsidian-delivery.json`。有额外实际窗口观察时可运行 `confirm-vault <课程包> <观察记录.json>`。手填记录须有同一版的 `entry`、`sha256`、`installed_at`，以及阅读模式、重开、页尾、折叠、引文、已看到的章节和图片，字段详见命令校验。

已绑定的课程后续用 `accept` 时会更新日常库。正文版本变化后重新执行 `verify-reader` 和 `verify --require-vault`。输出中的 `package_verified` 表示独立课程包通过，`verified` 表示日常库也有本版核验结果。`verify --require-vault` 检查现存正文、图片和所链接原件，同时要求该版本的重开记录。最终交付链接必须指到日常库项目名教材。报告有声、部分无口播、全程纯画面等类型的实际复核范围，保留真实听辨和原件缺口。

### 复核随课材料与作者支持

以入库清单为起点，逐件阅读旧稿标为「参考」「原样存档」「部分提取」的材料，也检查讲师口播未提及的附件。表格核工作表、字段、公式和规则，文档核内嵌图与超链接，压缩包核成员和各自用途。网址快捷方式从本地读取原 URL 字段，其归属和使用条件继续对照来源。把能独立用于学习的规则、练习、案例或可编辑原件写进当前教材。补充内容可以放在相关讲或文末的配套资料节，仍使用原有文档和视频标记保留同段出处。

对作者信息回到原始署名、名片、视频片头片尾和讲师主动介绍。跨课程相同的分发页和水印用于判断归属。作者频道、资料领取入口、只适用于一张参考图的署名及转载联系方式分别记录来源和用途。出现在课件上的历史规则、链接和账户说明按原始语境保存。识别不全、归属未证或原件仅有网址时，记录具体范围。

对于只有安装件的软件资料，核压缩包成员名、大小、文件指纹和作者发布页。可核实为同版本时，将另行找到的原作者指南、更新记录及公开渠道作为补充来源入库，并保留原来件清单、版本和来源链接。文中的步骤标明对应的作者版本。用同版说明与公开示例做案头走查时，记录实际核验范围。

安装件本身也可能附离线指南、平台分册、配置模板或原始画面。先列出成员，再用支持该封装版本的离线工具将所需文档和图读到工作区，记录完整成员路径和指纹。遇到 Inno Setup 可使用支持对应版本的 innoextract。将随安装件提供的正文、配置、图与同版仓库资料对照，记录实际一致的字段及剩余差异。 [innoextract 使用手册](https://constexpr.org/innoextract/innoextract.1)

将逐项确认要发布的内容登记到 `.course/work/补充内容复核.json`。`anchor` 用正文中读到的短句，`sources` 用同一段隐藏出处中完整的原件名与定位，例如 `实操练习.pdf · 第 3 页，输入单与交付检查`。同一条也可以引用 `第 2 讲 01:10–01:35` 等原片区间。预览时该文件纳入版本指纹，生成的 `supplements` 条目需随同版验收记录提交。`accept` 会校验短句和完整出处是否出现在同一可读正文段。写在代码块、折叠块或注释里的短句不能作为正文依据。

新验收记录的 `documents[].inspected_at` 填实际检查的页、工作表、压缩包成员或原件部位。其 `note` 填教学用途、与其他材料的关系或缺口。每个视频的 `episodes[].channel_review` 使用 `opening`、`closing` 和 `screen` 记录片头、片尾和实际画面位置，用 `finding` 登记据此确认的作用或未确认的地方。即使各处均未显示作者渠道，也按实际查看的时码、画面和观察结果填好。脚本核对记录完整性，编辑者据原件判断归属与读者价值。已经完成的发布件可照常复验，重做时使用新预览生成的模板。

### 同一批课程的续作顺序

先按用户指定顺序盘点完整的原件文件夹。未指定时按文件夹名自然升序，记录实际源路径和目标项目名。早期完成项目通过完整原件指纹与有效交付回执识别。可疑同名或转存版本先核新附件和原片差异。每完成一个项目，将已验收入口、正文版本和原件缺口保存到本地记录，再处理下一项。中断时从最近的原件、准备、预览或交付回执接续。具体原件缺失时记录所需文件及影响，按原顺序继续其余可做项目。

## 5. 开发入口与维护

| 代码 | 责任 |
| --- | --- |
| [course.ps1](../_tools/course_production/course.ps1) 和 [course.py](../_tools/course_production/course.py) | 对外单课程命令。传入原件目录后组织准备、预览、审核和验收。 |
| [project.py](../_tools/course_production/project.py) | 课程包位置、快照、哈希、原子写入和单课程锁。 |
| [prepare.py](../_tools/course_production/prepare.py)、[prepare_documents.py](../_tools/course_production/prepare_documents.py) | 讲号、字幕及底稿路由。复用原生文档读取、PDF 渲染和 OCR。 |
| [speech.py](../_tools/course_no_subtitles/speech.py)、[gpu_asr.py](../_tools/course_production/gpu_asr.py) | CPU 转写、SenseVoice Vulkan 转写与 Qwen3-ASR 或 Whisper 复识。 |
| [transcript.py](../_tools/course_production/transcript.py)、[visual.py](../_tools/course_production/visual.py) | 原字幕差异、采用稿和分窗读取、按呈现时间留下原帧及 OCR 轨。 |
| [review_media.py](../_tools/course_production/review_media.py)、[frames.py](../_tools/course_production/frames.py) | 全片画音扫描、同版有声对照、小窗和真帧取图。 |
| [publish.py](../_tools/course_production/publish.py)、[reader_review.py](../_tools/course_production/reader_review.py)、[acceptance.py](../_tools/course_production/acceptance.py) | 同篇编译、隐藏出处、跟做校验、逐集门槛、发布与回读。 |
| [verify_obsidian.cjs](../_tools/course_production/verify_obsidian.cjs) | 在独立 Obsidian 环境验证课程包和日常入口的同版文件，检查全部二级标题、页尾、图片与折叠。 |
| [vault_delivery.py](../_tools/course_production/vault_delivery.py) | 更新日常库的项目名入口，保留旧版正文，生成直达链接，核对重开回执及现存文件。 |
| [test_course_production.py](../tests/test_course_production.py) | 用独立生成的原料验证纯文档、全音轨、短缺口、暗色、硬字幕、采用稿更新、配套正文与出处、附件部位及作者核查、过期验收和续作。 |

- **输入恢复**。入库中断可以用同一 `start` 续作。原件文件完整确认后才生成正式快照记录。课程包的录屏、字幕和附件一致时重跑 `prepare` 复用底稿。不同参数或权重生成新的索引。
- **证据更新**。先更新原始媒体设置或取图记录，再预览；按新预览复核相关讲次。旧回执以正文、图片和提取证据的指纹识别。说过的参数、画面出现的动作和最终结果缺任一项时，回到对应原片、表格或讲义补齐。
- **发布恢复**。先校验同名图，复制新图并保存当前版本回执，再切换正文。新图使用新名，已发布正文版本保存在各自回执的 `body` 中，配图保留。手工改过的正式正文没有匹配回执时保留现场，把需要的编辑合入工作稿后再交验。
- **回归检查**。改原件、缓存、章节、来源或发布逻辑时，跑独立课程测试。要同时真测桌面阅读，设置 `WORLDANALYSIS_TEST_OBSIDIAN` 指向安装的 Obsidian；留空时测试覆盖准备、预览和应拒绝的情况。

~~~powershell
$env:WORLDANALYSIS_PYTHON = '<项目 Python 3.12 的完整路径>'
$env:WORLDANALYSIS_TEST_OBSIDIAN = 'D:\Software\Obsidian\Obsidian.exe'
& $env:WORLDANALYSIS_PYTHON -X utf8 -B -m unittest discover -s tests -p 'test_course_production.py' -v
~~~

## 6. 引用的项目

### 生产依赖与研究资料

- **音频转写与复核**采用 SenseVoiceSmall、sherpa-onnx、Silero VAD。可选 CrispASR Vulkan 和 Whisper Turbo，Qwen3-ASR 用于对照其他中文复识路线。参见 [SenseVoice](https://github.com/QwenAudio/SenseVoice)、[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)、[Silero VAD](https://github.com/snakers4/silero-vad)、[CrispASR](https://github.com/CrispStrobe/CrispASR)、[Whisper large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo)、[Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)。
- **解码和原图**使用 FFmpeg、PyAV、OpenCV。参考 Screex 的屏幕状态思路和 Waken 的控件定位方法。参见 [FFmpeg](https://ffmpeg.org/)、[PyAV](https://pyav.org/)、[OpenCV](https://github.com/opencv/opencv)、[Screex](https://github.com/blueprintparadise/Screex)、[Waken](https://www.research.autodesk.com/app/uploads/2023/03/waken-reverse-engineering-usage.pdf_rec2V6wlIVQVxnwlr.pdf)。
- **附件与阅读**使用原生文件提取、PDFium 和 RapidOCR，采用 Obsidian 的内嵌图片和折叠块。参见 [pypdf](https://github.com/py-pdf/pypdf)、[pypdfium2](https://github.com/pypdfium2-team/pypdfium2)、[RapidOCR](https://github.com/RapidAI/RapidOCR)、[Obsidian 折叠块](https://help.obsidian.md/callouts)、[Google 长文组织](https://developers.google.com/tech-writing/two/large-docs)、[Clarity 编辑规则](https://github.com/addyosmani/clarity/blob/main/references/edit.md)、[文档协作与读者测试](https://github.com/anthropics/skills/blob/main/skills/doc-coauthoring/SKILL.md)。
- **后续可比较的方法**包括从稳定帧识别幻灯片、按任务找界面区域、检测漏转、处理复杂文档版式和扫描小字。参见 [slide-detect](https://github.com/mizorewww/slide-detect)、[VG-GUI-TASKER](https://github.com/VG-GUI-TASKER/VG-GUI-TASKER)、[whisper-audit](https://github.com/xr843/whisper-audit)、[Docling](https://docling-project.github.io/docling/usage/advanced_options/)、[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)。

### 参考过的课程整理项目

- 先独立建立来源要点，再核实产出的覆盖及出处。来源清单与可见正文的关系参考长文本综合任务的覆盖与归因思路。 [SummHay 论文](https://aclanthology.org/2024.emnlp-main.552/)

- 视频、字幕与 PDF 进入同一教材，以及真实取图和链接校验。 [vid2courseware](https://github.com/Aster925/vid2courseware) · [video-to-chapter-book](https://github.com/flyfei-cmd/video-to-chapter-book)
- 主题、来源、章节的稳定关联，以及多份原料的归并。 [study-notebook](https://github.com/LucyGuo0422/study-notebook) · [note-organizer](https://github.com/Renakoni/note-organizer)
- 媒体探查、Markdown 出品、字幕优先与分讲检查。 [DeepTutor](https://github.com/HKUDS/DeepTutor) · [video2book](https://github.com/LINJIANG12/video2book)
