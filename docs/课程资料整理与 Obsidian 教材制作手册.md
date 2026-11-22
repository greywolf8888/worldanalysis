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

   CPU、GPU 与语音补查共用的解码器从容器开头直接读取。带 AAC 编码预卷的 MP4、M4A 从 0 秒跳转可能跳过约一帧可播放的音频，片头复核时将采用稿的样本起止与无跳转解码对照。正起点的局部复识沿用按时码定位。取音版本更新会刷新 `prepare` 及 `refine` 的缓存。已采用的人工修订仍保留在工作区，需要结合新底稿与原音重新确认后 `adopt`。
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

GPU 初稿用 `prepare` 或 `start` 的 `--gpu-exe` 与 `--model`。先用 CrispASR `--diagnostics` 核对 Vulkan 构建和实际显卡。可以 `--device` 指定诊断中的序号，省略时选择诊断出的最大显存设备。GPU 长轨从文件起点连续解码一个 PCM 流，按样本分组送入模型。SenseVoiceSmall GGUF 使用 30 秒分段和零重叠，同一轮最多送 20 段。每轮保存实际样本数、原时间轴起点和转写，最后一轮保留实际剩余样本。续作时重新顺序解码并复用同版已完成的轮次。模型与取音版本记在取材配置的签名里。GPU 运行成功后课程包记住运行配置，`prepare --cpu` 可显式切回 CPU。

长轨版本变更时，把正式窗口的首样点、末样点与整轨独立解码对齐。每个相邻窗口的起点应等于上一窗口起点加该窗口样本数除以采样率，全部样本相加等于独立解码的样本总量。选有声片头、分块交界和片尾对照转写，微小的样本差值留在取材复核记录。

CPU 和 GPU 都从 FFmpeg 的 PCM 解码流取样。GPU 路线由这些原始解码样本写出识别器所需的临时 WAV，避免封装阶段把转换后的时间戳抖动报为音轨损坏。仍要查看窗口的起点、实际样本数和原片声音。跨窗听辨有疑问时，可用 `refine --range 起秒 止秒` 取重叠区间复识。

CrispASR 成功退出且明确报告某个输入音频没有语音时，对相应窗口保存已解码采样数和空转写。人声检测继续独立核查，结合音乐、短动作和同屏变化判定用法。缺输出且该窗口无明确提示时仍需排查识别失败。

### 幻灯片内嵌视频

`start` 和 `prepare` 检查 PPTX、PPTM 的包内视频成员，按幻灯片关系记录播放页。同一附件里指向同一成员的多个播放关系保留一条原视频记录，并登记全部可定位的页。没有页关系的媒体保留成员定位，仍需判断用途。父附件和视频成员各留 SHA-256。视频解包到工作区的 `附件内嵌视频`，继续走原片探测、全音轨转写、画面扫描和取图。幻灯片自身的文字、版面和原图继续走附件处理。

编号先分给独立原片，内嵌视频随后编号。旧课续作用 `prepare` 加入新发现的内嵌编号，再按课内播放位置调整 `讲次顺序.json`。例如讲义页正在演示滤镜操作，教材配图和说明放在讲到该步骤的位置，折叠出处链接到原讲义并标注播放页。正文到附件和原片区间都要能反查。

`status` 可查看内嵌来源，验收的 `episodes` 中有 `embedded`。预览和复验以原附件成员和本地提取视频的指纹核对范围。`verify` 的 `original_files` 只计收到的实体文件，`videos` 包含已核的内嵌视频，`embedded_videos` 给出其中的数量。外链或没有随附的录像在附件复核中记录实际可读范围。

### 原视频损坏和可核查的恢复副本

原视频无法探测时，`start` 或 `prepare` 继续生成其他视频和附件的成果，保留原讲号，并在 `media-summary.json` 的 `unreadable` 中登记文件、摘要和探测错误。容器可探测、选定音轨解码失败时，同一清单还记录音轨来源、故障窗口的起点及已解码的样本时长。该集可读画面和原字幕照常保留。`status` 显示缺口，整课预览和验收等缺口核查后再继续。若拿到同一课的完整可读原件，用新来源快照做后续核对，已有核验结果按文件身份续用。

在额外副本上做恢复时，核对原件指纹、音视频包的来源、全段解码、关键画面和实际恢复的声音。确认可作为来源后，在 `.course/work/媒体恢复` 保存可读副本和同目录 JSON 清单，并用 `媒体设置.json` 的讲号键指向该清单，例如 `{"3":{"recovery":"03.json"}}`。清单字段包含 `original_sha256`、相对该目录的 `file`、恢复文件 `sha256`、恢复方法 `method` 和一行说明 `scope`。如果容器已经可读，脚本重新验证原选定音轨确实解码失败，再检查副本选定音轨和媒体时长。逐包复查时用原时间戳定位缺损，并维持恢复副本上的音画同步。说明写出可查到的画面、音轨和仍缺损的时段，放到对应章节的正常正文中。

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

讲师让人截图、保存参数或看画面时，前后检查到页面切走。时间很短时用 `window` 回看，用 `observe --ocr` 定位只写在屏幕上的提示词、规则或参数，再从清晰原帧读全条目。能教读者用的内容放在相关步骤，短时原屏留图，角标遮挡或原画模糊的部分按实际可辨范围记录。

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

PDF 先读原生文字和图片位置。带内嵌截图的页面同时保留整页渲染和辅助 OCR，即使页面已有可选文字。核对按钮小字、同屏标记、参数和作者渠道时，以对应原页的实际画面为准。原表格保留工作表、单元格、原公式和单位，数值案例对照实际行。PPTX 读页面正文和原生资源，图表、文本框及标注都参与讲解时读整页。Word 和 XMind 按原节点或章节保留定位。原样片、压缩包及提取能力以外的文件也进入清点，按实际使用或缺口登记。每份提取不全的附件明确补读的页、图或表，以及带来的正文修改。无法恢复的内容记录具体位置和缺少的原件。

PDF 的 URI 链接注释离线保存到提取结果的 `metadata.pdf_hyperlinks`，`coverage` 按原页列出。记录原始目标、PDF 用户空间中的矩形以及它是否与有效页面范围相交。相交只是定位信息，仍要看渲染页确定实际可见内容与归属。提取只记录本地原件中的目标和位置。按原链接阅读时另记可访问到的内容。有两处点击区域指向同一个教学资料时，读者入口合并，原件回执保留两处。文本层残留的页外广告、图内的新规则和作者小字均用原页或局部放大图定位。

旧版二进制 Word 用 `word-extractor` 读正文、正文和页边文本框、页眉页脚、脚注、尾注及批注。本机 7-Zip 按索引只读展开可用的 `Data`、表流和 `WordDocument`，识别并校验 OfficeArt 中的 JPEG、PNG 和 TIFF 原图。提取结果的 `metadata.word_pictures` 保留图号、尺寸、原数据指纹，以及各重复项的原始流和字节范围。图号供定位源资源，段落安排仍以文档和原课实际内容为准。疑似损坏或未能解码的图保留缺口。 [Word 内嵌图片记录](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/dedb7505-8cc4-4053-aa2d-431b18d4bd97) · [OfficeArt 图片类型](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-odraw/c67b883b-8136-4e91-a1a3-2981d16e934f)

用 `keep` 选旧 DOC 原图时，图片条目填 `document`、`picture` 和 `name`，裁切框可选，图片保持原倍率。段落用 `{{D003|第 2 幅内嵌原图}}` 关联到附件图片。预览会从 DOC 重取原图并核像素。只读验收须逐个确认为学习样例、操作界面、作者渠道或没有独立教学用途。

课程脚本用 7-Zip 或 libarchive 读取 RAR 和 7z。可通过 PATH 中的 `7z` 或 `7zz` 使用 7-Zip，也可以将 `WORLDANALYSIS_7ZIP` 指向本机的绝对路径。没有配置 7-Zip 时使用 Windows 的 `tar.exe` 或系统中的 `bsdtar`。如果归档工具漏读 Unicode 文件名，将运行环境指向 7-Zip 后复跑 `prepare`，再核成员清单、字节与原片。成员以编号存入课程包的 `.course/work/资料`，由资料记录中的 `archive_members` 对应原路径、文件指纹、保留副本与提取结果。档内文字按工作表、段落等位置复核，图片读实际画面。特殊成员、加密、超限和无法读取的内容在资料记录中留下缺口。旧版已核的音画取材可以复用。 [7-Zip](https://www.7-zip.org/) · [libarchive 官方手册](https://github.com/libarchive/libarchive/blob/master/tar/bsdtar.1)

媒体盘点独立查看 ZIP、RAR、7z 的原路径和可读成员，逐层登记教学音视频。`status` 报告待核媒体和 `.course/ledger/media-inventory.json` 的位置。核对待定媒体的实际用途后，在 `.course/work/媒体归类.json` 以完整来源定位为键，填 `role` 和 `reason`。角色使用 `教学`、`参考` 或 `重复`。重复项用 `target` 指向已确认的教学源。字节相同可共用取材，画面或转写相似的版本先核特有操作、配音和时间轴。回跑 `prepare` 后查看讲次顺序，已有讲号会保留，新增教学媒体使用新讲号。音频讲解完整转写，沿录音时间定位正文。有并行原片时保留音画对应证据。

本地 HTML 和 HTM 在 `prepare` 时离线提取静态正文、表行、链接地址和视听引用。资料记录保留行号及外层章节标识，动态模板和嵌入对象的画面或声音登记为复核项。从静态提取稿定位原文件，再看原始图片、媒体或有证据支持的页面状态。原文件随入库快照保存。

复核已有课程时，先比较原目录与旧入库范围，由原片内容确认新文件的课程身份、顺序和本段新增教学。对录制切分重叠保留两个原件，正文只讲一次。独立通读来源提取行业逻辑、盈利方式、应用、失败案例和条件关系，再用旧索引定位已有段落。最后反查正文每个数值、公式、条件和结果，验证同一来源及口径。讲师主张保留身份，外部核验和整理练习单独标明。

## 4. 编写、预览和验收

### 编写约定

在 `.course/work/教材.md` 写一个一级标题。有视频时每集一个二级标题，按 `讲次顺序.json` 排列。长讲用分节把学习问题、方法和示例连起来。理论说明原因与适用条件，公式先给变量及单位再演算，案例把选择与结果对应，操作紧贴可用的界面图。合并重复后对照原始要点，保留新增的数字、条件与例外。转写、取图、分卷与验收过程留在工作区，实际影响使用的条件继续写在正文附近。纯文档项目用二级标题组织原件里的任务。

写稿前先读各段原材料，把独立要点保存到 `.course/work/逐段要点/<两位讲号>.json`。文件为数组，每项填 `range_s`、`kind`（如「输入」「操作」「参数」「条件」「原例」「纠错」「结果」「画面」）和能回到原课的 `source_note`。对照旧稿时，重复教学可以合并，实际参数、例外和操作顺序需要保留。

每节保留可复制的设置和输入。按学习任务引导读者准备、操作和核对结果。讲师经历具名，实际原例、结果和纠错放回发生的位置。各图紧跟动作段落。使用同一素材重新举例时注明为练习。

- 原视频出处 `{{01|00:12.4–00:15.2}}`。使用支撑正文的起止时间。
- 附件出处 `{{D001|第 1 页，尺寸设置}}` 或 `{{D002|Sheet1 C4:O6}}`。
- 在动作段落之后独立一段用 `{{img:展开比例菜单|550}}` 插入已登记的原图。需要多张时连续插入。描述图里要看的对象。
- 发布器将出处转换为隐藏注释和文末折叠资料。过程记录留在工作区。用原资料编号、时间码和实际章节验收正文。

### 审核记录与提交

~~~powershell
& $courseTool coverage $course --write
& $courseTool preview $course
# 读预览，逐集对照原片，核查附件；按模板填写 .course/work/验收记录.json
& $courseTool check-review $course
& $courseTool accept $course
& $courseTool verify $course
~~~

写稿前用 `coverage` 根据入库指纹、原始时长、附件结构和可定位作者线索核对范围。`preview` 编译图文稿，生成对应的 `验收填写模板.json`。另存为 `验收记录.json` 并填实查结果。`source_scope` 是当前原件范围，`source_coverage` 是检查结果。先在来源侧覆盖各段时间、页、表或档内成员，并记录作者渠道的核验地点和归属。教学要点和确认应保留的渠道需要可见正文及同段隐藏出处。完整核过的静默、过场、重复材料记录具体依据。仍有待核来源时，用 `check-review` 定位后继续补查。

编译时用原片时戳和裁切框重新取图，逐张核对真实像素。视频原图最多并行检查 6 张，PDF、PowerPoint 和整理示意图逐张检查。配图、裁切或源帧不一致时修复后再生成预览。

逐段完成 `claims`，从正文反查原片时间与附件实际位置，并核对同一案例的条件、顺序和结果。编辑衔接、整理示例和外部核验按实际身份登记。`check-review` 与 `accept` 使用同一道内容核验。历史回执可用 `verify` 检查既有交付，新发布和更新使用新版模板。

| 记录 | 要填写的内容 |
| --- | --- |
| `snapshot` | 复制本次模板的正文、原图和证据指纹。换稿后使用新模板，对变更部分重新复核。 |
| `episodes` | 每个视频保留原件身份、讲号和对应二级标题。`watched` 按顺序列原片的真实时间范围、类别（教学、原例、说明、过场）和观察。看片记录覆盖全片首尾，包括静音、停留画面等。纯文档项目留空。 |
| `source_points` | 从独立逐段要点文件进入模板。补可见 `anchor`，并把该段隐藏出处放在同一正文块。原件本身缺细节时填 `source_limit` 并记实际影响。关键项未解决时保留编辑状态。 |
| `key_steps` | 每个关键步骤填 `range_s`（原视频起止秒数）、`anchor`（预览里能找到的可见动作短句）、`figures`（紧贴该动作的 PNG 名）。短句也出现在标题或其他段落时，验收优先以覆盖该步骤的段落出处定位。有画面的原图时间须在步骤区间内。纯口述步骤可用 `visual_note` 交代依据。 |
| `shared_visual` | 多个原片实查是同一教学画面时，后讲的 `visual_basis` 用「复用同一原画」。`chapter` 填本篇前面已经验收的二级标题，`figure` 填该节已经插入的原片 PNG 名，`proof` 填本集原画对照在工作区的 `path` 和 `sha256`，同时将该条写入 `checked_sources`。在本讲的正常正文中用 `[[#前面二级标题|原画位置]]` 定位这张图，仍逐段核对本讲独立口述。原画里新增的动作、参数、提示和状态按本讲各自配图。 |
| `visual_basis` | 从「原片」「附件原图」「无可教学画面」选择实际依据，在 `visual_note` 说明可辨内容。适用时正文要有本集原帧或相应附件原图。 |
| `audio_checks` | 每个候选回听后记录「已补正文」「正文已涵盖」「非教学声音」「听辨缺口」之一及理由。遇到参数等关键缺口时写清可核实的值和适用条件。 |
| `checked_sources` | 可登记实际核查的局部观察、原图或复识证据。每项含工作区相对 `path` 与 `sha256`。 |
| `documents` | 每件附件有用途和实际核对说明。可用「引用」「用作转写」「参考」「重复」「原样存档」「提取缺口」。引用的附件回到相应页、表或章节。 |
| `follow_along` | 只读预览完成关键任务。每条填所在二级标题、准备/入口/设置/结果/纠错五项可见文字的定位、同章节隐藏出处、实际看到的 PNG 文件名。附件出处填原文件名和位置，验收按原标点读取正文中的 HTML 转义。可用 `visual_note` 说明纯文字依据。 |

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

`accept` 用同一版预览比对逐集时间、关键步骤邻图、附件去向和跟做证据，再在隔离的 Obsidian 阅读视图从开头滚到底，检查每个二级标题、全部图像加载、跳转、横向溢出、折叠资料和引文隐藏。巡查时限按正文长度和图片数调整，读到页尾前继续记录尚未核阅的章节与图片。验收逐个对应显示出的原件链接，兼容 Obsidian 已解码的路径及文件名中的 `%`、`&`、英文逗号和 `@`。通过后写入课程包。源码定位与结构检查由脚本完成，是否忠实于原课、学员能否按内容做出产物由逐集核对和读者走查负责。

### 交付到用户日常 Obsidian 库

确认用户在用的库及本课既有目录。更新旧课沿用库内同一项目目录，并将唯一的教材放在 `课件/<项目名>.md`。课程包独立保存原件和生产证据，日常库保留教材、配图和折叠区用到的原件。首次绑定用 `deliver`。遇到旧入口正文不一致，先确认同课、保存用户修改，再按提示提供经核对的旧正文 SHA-256 给 `--reviewed-previous`。脚本先核对是否有指向旧笔记的链接，归档旧正文，校验已有配图及原件，补齐缺件后再更新教材。用户要求仅本地时，核实目标库的同步情况。

~~~powershell
& $courseTool deliver $course --vault 'G:\我的日常阅读库' --course-folder '一门课'
& $courseTool verify-reader $course
& $courseTool verify $course --require-vault
~~~

`deliver` 返回日常库实际路径 `entry`、Obsidian 直达链接 `uri`、正文指纹 `sha256` 和落库时间 `installed_at`。链接使用 Obsidian 支持的 `obsidian://open?path=...`，可直接从课单或验收记录打开。 [Obsidian 直达链接](https://obsidian.md/help/Extending%2BObsidian%2BURI)

首次发布会在读者看不到的同盘临时区准备副本，核对正文和链接后把完整项目目录一次移入库。这样 Obsidian 看到新项目时，正文、原图和引用的原件已经到齐。首次 `deliver` 和已绑定课程的 `accept` 都更新 `00 入口/00 项目总览.md`。Obsidian 文件列表按名称升序，展开最前面的 `00 入口` 即可打开总览。总览正文按已发布教材的最近更新时间排列。`refresh-catalog <日常库路径>` 可手动重建程序维护区，并迁移根目录的纯程序生成旧版总览。已有手写内容的旧页保留原位。若日常窗口曾漏掉外部写入，先在实际窗口重载一次并打开总览。后续在打开的库中确认新项目显示，独立阅读回执仍记录整篇核对。

`verify-reader` 从日常库中读取已交付的正文和链接文件，在独立 Obsidian 环境按该项目路径打开、关闭后重开，读到所有章节和文末，确认全部图片已加载、出处默认折叠以及页面无横向溢出。回执保存于 `.course/work/日常库接口验收/`，其版本摘要记录于 `.course/ledger/obsidian-delivery.json`。有额外实际窗口观察时可运行 `confirm-vault <课程包> <观察记录.json>`。手填记录须有同一版的 `entry`、`sha256`、`installed_at`，以及阅读模式、重开、页尾、折叠、引文、已看到的章节和图片，字段详见命令校验。

已绑定的课程后续用 `accept` 时会更新日常库。正文版本变化后重新执行 `verify-reader` 和 `verify --require-vault`。输出中的 `package_verified` 表示独立课程包通过，`verified` 表示日常库也有本版核验结果。`verify --require-vault` 检查现存正文、图片和所链接原件，同时要求该版本的重开记录。最终交付链接必须指到日常库项目名教材。报告有声、部分无口播、全程纯画面等类型的实际复核范围，保留真实听辨和原件缺口。

### 复核随课材料与作者支持

以入库清单为起点，逐件阅读旧稿标为「参考」「原样存档」「部分提取」的材料，也检查讲师口播未提及的附件。表格核工作表、字段、公式和规则，文档核内嵌图与超链接，压缩包核成员和各自用途。网址快捷方式从本地读取原 URL 字段，其归属和使用条件继续对照来源。把能独立用于学习的规则、练习、案例或可编辑原件写进当前教材。补充内容可以放在相关讲或文末的配套资料节，仍使用原有文档和视频标记保留同段出处。

对作者信息回到原始署名、名片、视频片头片尾和讲师主动介绍。跨课程相同的分发页和水印用于判断归属。作者频道、资料领取入口、只适用于一张参考图的署名及转载联系方式分别记录来源和用途。出现在课件上的历史规则、链接和账户说明按原始语境保存。识别不全、归属未证或原件仅有网址时，记录具体范围。

口述邀请关注自己的频道、在视频下留言领取示例，即使转写没有读出账号，也会出现在 `coverage` 的作者候选中。附件中带明确标签的中文频道名同样进入复核。顺着口述回看名片和对应讲义，区分作者公开入口、资料下载包分发标记与案例账号。作者说会另外提供的文件要在原件目录核实，教材交代当前能学到的内容和仍缺的文件。候选只提示去哪里看，账号、归属和用途以原件为准。

自动候选为空也须查原画。遇到移动、倾斜或贴边的文字，从不同时点选清楚的原帧，核对联系方式和原件标注的用途。确认的账号或网址在 `source_coverage.videos[].channels[].findings` 或对应附件单元的 `channels` 里填 `kind: 作者渠道`、正文定位 `anchor` 和原值 `value`。视频还填能解释该判断的 `range_s`。验收校对同一可见正文段的完整标识和隐藏出处。只有二维码或读到部分文字时，写清其原件状态和可确认的用途。

附件截图的 GitHub、Gitee 项目链接以及 PDF 中相同类型的链接注释也进入候选，按原页定位。换行只影响排版时可拼回完整地址；省略号或缺字按片段登记。逐页对照原画里的链接、署名和相邻说明，检查同份附件是否另有完整账号，再用作者公开项目核归属。若公开仓库有新版本，教材保留原课实际用法，同时标明来自官网或作者新版本的补充。PDF 的原生文字只有分发页脚时，同样通读每页的图像内容。

口播中请学员向值班老师、师傅、助教或团长提问、领提示词，也进入渠道复核。核对说话前后的画面、课内聊天记录和附件，实际写明谁可以问、在哪里问、能拿到什么。未给公开账号的课内渠道可留空 `value`，使用同段可见说明和原片时码。来源层叠有多套联系方式时分别记其主体和用途，未确认属于作者的角标保留核查依据。

只有网盘或站内分享入口的项目先保存来件及指纹。用原件给出的提取信息核对正常入口，记录实际可见的文件、目录、大小和仍需权限的位置。能取得的原文件按层级留在本地并核对清单，再加入转写、逐页阅读和出处验收。目录能够帮助定位原件。缺课片或附件时，在批次里留原顺位及缺件，保留已查明的作者与支持入口，并接续其余项目。

附件文本或 OCR 中独立一行的 HTTP(S) 地址和明确域名进入来源候选，即使没有「领取」或「联系方式」等标签。PDF 的 HTTP(S) 注释目标也进入同一检查，链接印在页上或藏在可点的文字、图形里都要看与版面的关系。保留路径和查询值的大小写。检查具体内容前先从原件确认链接用途。课程专用的样本、模板和设备参考在教学文字旁提供能直接选择的入口，同一目标只保留一次。普通访问只到登录或授权页时，在记录里注明可见范围，等官方或作者给出可读取的原件后从原顺位续作。

对于只有安装件的软件资料，核压缩包成员名、大小、文件指纹和作者发布页。可核实为同版本时，将另行找到的原作者指南、更新记录及公开渠道作为补充来源入库，并保留原来件清单、版本和来源链接。文中的步骤标明对应的作者版本。用同版说明与公开示例做案头走查时，记录实际核验范围。

安装件本身也可能附离线指南、平台分册、配置模板或原始画面。先列出成员，再用支持该封装版本的离线工具将所需文档和图读到工作区，记录完整成员路径和指纹。遇到 Inno Setup 可使用支持对应版本的 innoextract。将随安装件提供的正文、配置、图与同版仓库资料对照，记录实际一致的字段及剩余差异。 [innoextract 使用手册](https://constexpr.org/innoextract/innoextract.1)

将逐项确认要发布的内容登记到 `.course/work/补充内容复核.json`。`anchor` 用正文中读到的短句，`sources` 用同一段隐藏出处中完整的原件名与定位，例如 `实操练习.pdf · 第 3 页，输入单与交付检查`。同一条也可以引用 `第 2 讲 01:10–01:35` 等原片区间。预览时该文件纳入版本指纹，生成的 `supplements` 条目需随同版验收记录提交。`accept` 会校验短句和完整出处是否出现在同一可读正文段。写在代码块、折叠块或注释里的短句不能作为正文依据。

新验收记录的 `documents[].inspected_at` 填实际检查的页、工作表、压缩包成员或原件部位。其 `note` 填教学用途、与其他材料的关系或缺口。每个视频的 `episodes[].channel_review` 使用 `opening`、`closing` 和 `screen` 记录片头、片尾和实际画面位置，用 `finding` 登记据此确认的作用或未确认的地方。即使各处均未显示作者渠道，也按实际查看的时码、画面和观察结果填好。脚本核对记录完整性，编辑者据原件判断归属与读者价值。已经完成的发布件可照常复验，重做时使用新预览生成的模板。

### 同一批课程的续作顺序

先按文件夹名自然升序盘点原件。`batch plan` 只读显示范围。`batch init` 将顺位、目录和日常库保存在本地清单。迁移已有批次时指定 `--legacy-directory`，脚本将历史回执与现存原件、课程包和日常入口核对。合并了经验证补料的历史课程用 `--supplemented <顺位>` 逐项检查原件与课程包的关系。用 `--defer-history` 可以先验证当前在做的项目、保留历史同版回执，再逐课复验原始文件。历史行明确标为「历史验收待复验」，完成全面复查后用 `batch finish` 更新。

~~~powershell
& $courseTool batch plan $sourceRoot --vault $vault
& $courseTool batch init $sourceRoot --manifest $batchManifest --vault $vault
& $courseTool batch status $batchManifest
& $courseTool batch claim $batchManifest 1 $course --course-folder '项目原入口'
# 完成单课的取材、内容核验、发布和日常库重开后登记
& $courseTool batch finish $batchManifest 1 $course
~~~

`claim` 绑定现有包、原件指纹和目标文件夹，同一项目中断后从绑定信息续做。遇到同源重复时查原包和重复内容，确需独立项目笔记用 `--separate` 记录依据。待补的项目用 `batch defer` 记录所需原件和影响，下一项可继续。补件后用 `batch reopen`，核实变动时再明确登记变更。批次目录新增一级项目时用 `batch refresh` 纳入，既有顺位编号保留。`finish` 复验本项目的原件、已验收正文及同版 Obsidian 阅读回执后更新批次。清单、原件和回执留在本地。

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
| [test_course_audio_recovery.py](../tests/test_course_audio_recovery.py) | 损坏一帧测试音频，验证其余集与原画继续取材、整课预览提示缺口，以及登记同源可读副本后按原身份续作。 |

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
