---
name: course-material-archive
description: Turns a local course folder into one verified Obsidian textbook by reading the original audio, captions, screen actions and attachments in order.
---

# 本地课程教材制作

## 已有本地课程文件夹

在 Worldanalysis 根运行 `_tools/course_production/course.ps1 start "<原件文件夹>"`，以命令返回的课程包为续作入口。已有包用 `status`、`prepare` 和已有验收记录接续。长课先用实际原片完成一讲的取材、编写和阅读验证，再推广到其余视频。详见[本地教材工作流](workflow.md#本地教材工作流)。

每项目发布一篇 Markdown。按原课顺序，用老师第一人称讲实际做法，把准备、入口、关键参数、原案例、纠错和可见结果写在正文。短暂菜单、同屏圈点和无口播操作需要回原片的小窗查看。关键画面或原页紧贴对应步骤，段落与配图出处默认隐藏。

从 `read` 的采用转写出发逐段对照原片和附件，独立记下来源要点，再写正文。用 `observe` 查看短动作、字幕和暗界面，用 `refine` 复识争议，用 `adopt` 纳入已经核实的结果。按[本地教材验收](acceptance.md#本地教材验收)填写同版内容记录，执行 `accept`，再用 `verify` 核验原件、正文与原图。Agent 判定实际教学信息，脚本校验来源、版本和阅读结果。命令、字段和恢复方式见[开发文档](../../../docs/课程资料整理与%20Obsidian%20教材制作手册.md)。
