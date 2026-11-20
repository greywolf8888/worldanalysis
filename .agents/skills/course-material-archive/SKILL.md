---
name: course-material-archive
description: Turns a local course folder into one verified Obsidian textbook by reading the original audio, captions, screen actions and attachments in order.
---

# 本地课程教材制作

## 已有本地课程文件夹

按[项目规则](../../../AGENTS.md)在 Worldanalysis 根运行 `_tools/course_production/course.ps1 start "<原件文件夹>"`，以命令返回的课程包为续作入口。已有包用 `status`、`prepare` 和已有验收记录接续。更新旧课时先核完整原件与原入口，对得上的证据可以复用。长课先用实际原片完成一讲的取材、编写和阅读验证，再推广到其余视频。详见[本地教材工作流](workflow.md#本地教材工作流)。

每项目发布一篇 Markdown。保留原课讲次和必要的先后关系，讲内按学习问题组织。直接解释方法，操作自然面向学习者，讲师亲历和课堂主张明确归给讲师，需要保留原话时用引述，新增计算标为演算示例。正文给足准备、入口、参数、案例、纠错和结果。短暂菜单、同屏圈点和无口播操作回原片查看，能说明动作或状态的图紧贴相应段落。出处、转写复核与分卷记录默认留在隐藏注释或工作区。

从 `read` 的采用转写出发逐段对照原片和附件，独立记下来源要点，再写正文。用 `observe` 查看短动作、字幕和暗界面，用 `refine` 复识争议，用 `adopt` 纳入已经核实的结果。按[本地教材验收](acceptance.md#本地教材验收)填写同版内容记录。课程包经 `accept` 验收后，将这一版更新到用户日常 Obsidian 库的既有入口。实际重开、读到新增章节、邻图和页尾后登记 `confirm-vault`，用 `verify --require-vault` 核验交付。Agent 判定教学信息与真实窗口可学性，脚本校验身份、文件和回执版本。命令、字段和恢复方式见[开发文档](../../../docs/课程资料整理与%20Obsidian%20教材制作手册.md)。
