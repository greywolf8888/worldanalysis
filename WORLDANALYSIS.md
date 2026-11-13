# Worldanalysis：混合证据与独立分析

这是在原工程上新增的可运行第一版，不是完整P0–P4计划已经验收完成。完整范围已登记为持续目标；当前剩余项必须继续逐条核实。

## 入口

在项目目录使用[Windows启动器](<worldanalysis.cmd>)，它固定使用宿主Python3.12运行时，避免PATH中的Python3.14与本地原生依赖ABI不兼容：

```bat
worldanalysis.cmd --help
worldanalysis.cmd capabilities
worldanalysis.cmd status
worldanalysis.cmd create-project --project-id my-project --topic 我的资料
worldanalysis.cmd ingest --project my-project 手动投放\资料.txt
worldanalysis.cmd ingest --project my-project --ocr 手动投放\资料.pdf
worldanalysis.cmd verify --project my-project
worldanalysis.cmd analysis next --project my-project --model-version 当前实际模型版本
worldanalysis.cmd analysis read --task 真实任务ID --block 真实块ID
worldanalysis.cmd analysis complete --task 真实任务ID 整理结果\项目ID\分析\任务ID\result.json
worldanalysis.cmd control collection pause
worldanalysis.cmd control analysis pause
```

`ingest`只接收工作区内明确输入，小原件保留；不移动/删除手动投放，不执行内容。多文件可一次传入。媒体不走64MiB小文件预哈希入口；已自动接单源有界worker与通用Bundle。原媒体仅登记stat描述，不冒称整片摘要/不可变复制；多媒体及文档/媒体组合发布仍明确unsupported。

`analysis next`返回真实已发布版本和任务输出目录；当前授权Agent实际读取并产出结果，程序核验身份、全部块状态、引用、读取回执、报告字节和补采请求。**没有配置商业模型API时不会伪造无人值守摘要**。图像必须真实打开并留下像素/字节关联回执，不能拿路径充数。

## 当前真实产物

- [执行模型与边界](<_ledger/devpack/执行模型.md>)：对象、状态、授权、路由及未完成项。
- [设备/库存实测](<_ledger/devpack/p0-capabilities-20261113.json>)：真实32GB级系统RAM与8GB显存分别测量；导入库不算解析验收。
- [真实短TXT主报告](<整理结果/real-shorttxt-meituan/分析/analysis-task-0193d2b5749b418c84f724b4b43154be/accepted/report-00.md>)：完整读取54字节原文，保留联系人、原CRLF和出处；报告partial，已入队一项本地补采。
- [真实3图DOCX主报告](<整理结果/real-docx-virtual-materials/分析/analysis-task-74a65236b89342fb91ed27c8d623a1d7/accepted/report-00.md>)：实际阅读8正文块和3原图；4个空正文容器仍pending，未把领取宣传当完整教程。
- [真实数字/扫描PDF证据](<整理结果/real-pdf-163545/证据/v0005/manifest.json>)：两个真实PDF、9页原页渲染、实际本地OCR；同组反序重跑复用同版本。未解释图像仍partial。
- [云盘范围实测](<_ledger/devpack/p2-browser-observation-20261113.json>)：允许目录和具体分享可读，但精确递归清单、真实转存/读回/下载尚未闭环。

## 运行与安全模型

一套SQLite业务台账＋不可变证据目录；历史075材料和旧JSON不覆盖。采集、分析可分别暂停，换模型不重新采集未变文件。Bundle先登记publish_id，再写/验/原子重命名，最后SQLite登记；重启对账。任务提交未知时保留状态与预算，不盲重发。

新云盘只允许 `/apps/教育分析学习/`，每次核对账号、真实ID和当前路径；分享ID与用户盘ID分开。默认零新增商业预算/外发/百度AI生成；模型计划不是授权。联系人、二维码和重复附件保留来源，不自动删除。原始图像、OCR/ASR、作者说法、外部事实和模型推断分层。

## 验证与仍需完成

上一版[完整版本绑定回执](<_ledger/devpack/verification-20261113T134802Z.json>)记录：188项测试、187通过、1项因OS不能创建symlink跳过、0失败/错误；测试前后源码SHA一致，旧Node回归通过。合成接口样本、真实本地库存和真实授权浏览器观察三类不混报。完整[21场景验收矩阵](<_ledger/devpack/acceptance-matrix.json>)保留所有未完成项。运行回归：

```bat
worldanalysis.cmd --help
"D:\桌面\awesome-dsh\.dsh-home\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" -B -m unittest discover -s tests -p "test_*.py" -v
node --test _tools/course-flow.test.mjs _tools/materials-ingest.test.mjs
```

尚未全计划验收：真实云盘整链、多源媒体/文档组合Bundle、真实课程与百小时生产校准、真实XLSX与同文件混合PDF样本、复杂页/矢量/公式语义、外部核验reader及更完整增量调度。不要把离线通过、合成语音识别或这两份partial报告说成整个计划完成。

本轮[快速复核](<_ledger/devpack/round3-quick-review.json>)仅跑受改动影响的20项，全部通过，未重复整套测试。真实双PDF已验证新增B只提取B，反序重复不重提取；合成语音已实际ASR→Bundle→Agent partial报告，不是生产课验收。目标实时已为active/armed，保留继续；未创建新目标、定时器或常驻循环。
