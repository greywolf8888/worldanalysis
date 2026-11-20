---
name: course-material-archive
description: Turns a supplied local course folder into one verified Obsidian textbook with original frames, first-person instruction and per-video review.
---

# 本地课程教材制作

## 已有本地课程文件夹

拿到课程文件夹就直接走[本地教材工作流](workflow.md#本地教材工作流)。在 Worldanalysis 根运行 `_tools/course_production/course.ps1 start "<原件文件夹>"`，以命令返回的课程包为续作入口。已有课程包走 `status` 和 `prepare`。按该包的原件、转写、附件提取和画面候选交叉核对。输出和验收均由课程包自身定位。

每项目发布一篇 Markdown。按原课顺序，用老师第一人称讲实际做法，把准备、入口、关键参数、原案例、纠错和可见结果写在正文。短暂菜单、同屏圈点和无口播操作需要回原片的小窗查看。关键画面或原页紧贴对应步骤，段落与配图出处默认隐藏。

拿到候选帧和转写后继续完成逐集原片复核、教材编写、只读跟做与阅读验收。附件也逐件确认去向。按[本地教材验收](acceptance.md#本地教材验收)填写当前预览的记录，执行 `accept`，再用 `verify` 核验同版原件、正文和原图。脚本负责文件与版式验证，编辑者根据原件判定内容。需要改脚本或排查时看[开发文档](../../../docs/课程资料整理与%20Obsidian%20教材制作手册.md)。
