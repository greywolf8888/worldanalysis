# P1 documents — 实施与验收证据

## 实际交付

- [原生解析模块](../../worldanalysis/documents.py)：`probe(Path)->dict`、`extract(Path, output_dir, *, options=None)->dict`，输出用户指定 raw 结构，直接复用 Bundle 原枚举。
- [文档测试](../test_documents.py)：53 项全通过；增加 renderer 后最终回归运行 14.079 秒，exit 0（非吞吐基准）。locator/coverageMetric 使用现有 jsonschema 4.25.1 作 schema fragment 校验。
- [真实库存结果](real_inventory_results.json)：5 份自动选取的授权本地资料；源 SHA256 前后相同；每项实际覆盖、状态、缺口、产物路径均留档。
- [真实 CPU OCR 联测](ocr_acceptance_results.json)：通过显式 callable 注册，合成图和真实扫描 PDF 分开标记；保留 source block/file 关联，未把 OCR 当整图解释完成。
- [OCR 验收脚本](run_document_acceptance.py)：只在本独立 fixtures 目录生成合成图片/验收产物，实际资料只读。
- [PDF 渲染器](../../worldanalysis/pdf_render.py)及[渲染测试](../test_pdf_render.py)：16 项通过，最终一次 6.472 秒；另见[准入与验收说明](PDF_RENDER_ACCEPTANCE.md)、[真实渲染页](pdf_render_results.json)和[实际页面渲染→OCR血缘回执](pdf_render_ocr_results.json)。渲染仅补视觉证据，不声称语义解释完成。

本目录 `.runs` 是标为 synthetic 的临时输入/输出，每项测试结束后清理自己的临时目录。`real_evidence` 是通过设置 `WORLDANALYSIS_DOCUMENT_RETAIN_REAL=1` 留存的真实提取文件，不是合成证据。`ocr-acceptance-*` 内的图片、raw 和 OCR 输出分别标注来源，原件没有被修改/移动。

## 接口与行为

```python
from pathlib import Path
from worldanalysis.documents import probe, extract

info = probe(Path("input.docx"))
# output_dir 由调用者提前创建，必须为空；解析器不会创建/占用别处目录。
raw = extract(Path("input.docx"), Path("empty-output"))

# 可选：只在调用者已准入本地 OCR 后显式注册。默认不导入/下载引擎。
from worldanalysis.ocr import ocr_image
raw = extract(Path("image-or-scanned.pdf"), Path("another-empty-output"),
              options={"ocr": ocr_image, "ocr_options": {}})
```

- TXT/MD/JSON/CSV/SRT/VTT：无最短字数阈值；完整解码字符串落盘。每个 block 的 text 最大 16000 字符，连续分块，不截断。JSON/CSV/字幕结构错误也不丢原文。
- DOCX：正文、嵌套表格、段落/图片字符定位、超链接、页眉/页脚/批注/脚注，隐藏字与未渲染形状登记。相同图资源共享真实文件，但出现位置分别建 block；文本框内图片不重复枚举。
- PPTX：原 slide 顺序、隐藏状态、notes、位图、布局/母版的继承图片、图表/形状缺口。不会把仅文字提取成功当页面视觉完成。
- XLSX：逐实际 cell，而非按巨大 dimension 展开；值、原公式、原缓存分离；合并/隐藏 sheet/行/列、图片 anchor、批注和图表。不会执行计算或外链；缺公式缓存明确 gap。
- PDF：pypdf 7.0.0 原生文字和访客矩阵；按页选择 native_text、image_ocr、blank_native、vector render required 等路线。按 Do/inline/Form 指令枚举每次图片出现，保存真实图像像素；同资源复用不会吞掉位置。原内容矩阵与 unrotated PDF user-space bbox 在 metadata，未伪造 glyph bbox 或渲染坐标。
- 图片：Pillow 12.3.0 `verify` 加有界逐帧 `load`，不止看扩展名/文件头；原字节不重编码保留。图像语义未解释始终 `partial`，成功 OCR 也不能移除该语义缺口。
- ZIP/OOXML：先检查成员数、原/解压字节、单成员、压缩比、重复/大小写路径、穿越、符号链接、加密；不向文件系统解压成员路径。ZIP 只输出清单，不执行成员。XML DTD/entity 拒绝；宏/EXE/旧 Office 仅资产状态登记、不生成正文。

默认上限在模块 `DEFAULT_LIMITS`：输入 128 MiB、产物 256 MiB、容器 10000 项/解压合计 256 MiB/单成员 32 MiB、XML 16 MiB、图像合计 6400 万像素/256 帧、页/slide/sheet 10000、cell 50 万、block 10 万、PDF 单流 32 MiB/操作 100 万/Form 深度 16。碰到上限返回明确 resource gap，不假装枚举结束。

## 真实覆盖（未注册 OCR 的原生验收）

| 类型 | 自动选取 | 实际结果 |
|---|---|---|
| 短 TXT | 美团项目“项目资料（必读）”，54 字节/26 字符 | 1 block、1 文件、完整字符保留，native verified_complete |
| DOCX | “创金思维团队制作无限生成虚拟资料程序”，830777 字节 | 15 blocks、12 产物、3 个真实图出现位置；partial |
| 扫描 PDF | “泽泽学员内部加热框架”，47141 字节 | 1 页、1 图、2 blocks；未 OCR 和未解释矢量明确 partial |
| 数字 PDF | “泽7”，105015 字节 | 8 页、16 blocks、8 个 native text 产物；矢量/布局未解释，partial |
| PPTX | “58.带货视频：如何正确推销你的产品 (1).课件”，129801 字节 | 1 slide、20 blocks、2 个实际图出现位置；继承形状未渲染，partial |

原始绝对路径、源摘要、每项缺口和实际产物绝对目录都在[真实库存结果](real_inventory_results.json)，没有按源名称覆盖旧课件或台账。

## CPU OCR 实测（另一次显式注册验收）

`rapidocr_onnxruntime==1.4.4`，调用的是共享 OCR adapter（本分支不安装依赖）：

- 合成图：OCR 31 字符，0.937 秒；source 原图和派生文字均留存，`partial`，语义解释 gap 仍在。
- 上述真实扫描 PDF：OCR 76 字符，1.156 秒；源 SHA256 前后相同；`partial`，保留语义解释和 PDF 矢量渲染缺口。
- 这是“确实运行了 OCR”的证据，不是 OCR 零错误或视觉解释完成的证明。详细 lineage、坐标与 uncertainty 见 [OCR 验收结果](ocr_acceptance_results.json) 指向的 raw 文件。

## 明确尚缺

- 当前授权库存没有选到真实 XLSX，故公式/缓存/截图仅有明确 synthetic 的接口测试，真实 XLSX 验收待完成。
- 真实数字 PDF 与真实扫描 PDF 分开验证；真实“同一 PDF 混合数字/扫描页”本次未验证。合成三页（数字、重复扫描图、空白）和 Form/inline 图像已验证。
- 已新增可选 PDF 单页 CPU 渲染，需显式注册 `options['render_pdf_page']`；未注册时仍保留原缺口。Office 页面渲染和复杂表格/公式/矢量图/SmartArt/叠加布局语义解释仍未提供，相应材料保留 `partial` 和可恢复 gap。
- 旧 DOC/PPT/XLS、加密 PDF、宏和执行文件不转正文；没有调用转换器、运行宏/公式、执行工程内容、读取外链或使用浏览器。

## 复现

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 \
  PYTHONPATH='G:/Worldanalysis/.runtime/site-packages' \
  WORLDANALYSIS_DOCUMENT_RETAIN_REAL=1 \
  'D:/桌面/awesome-dsh/.dsh-home/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe' \
  -m unittest discover -s tests -p test_documents.py -v

PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 \
  'D:/桌面/awesome-dsh/.dsh-home/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe' \
  tests/document_fixtures/run_document_acceptance.py
```

解析器实际运行依赖为 Python 标准库、Pillow 12.3.0、pypdf 7.0.0；合成样本另使用现有 python-docx 1.2.0、python-pptx 1.0.2、openpyxl 3.1.5（运行时实测版本，不是假设 3.2）。没有新服务费用或外发。
