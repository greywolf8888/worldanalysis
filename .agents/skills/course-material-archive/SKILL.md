---
name: course-material-archive
description: Turns a local course folder into one verified Obsidian textbook by reading the original audio, captions, screen actions and attachments in order.
---

# 本地课程教材制作

## 从本地原件开始

从课程工作树运行 `_tools/course_production/course.ps1 start "<原件文件夹>"`，以输出的课程包路径接续。已有包先看 `status`，按需用 `prepare` 补取材。长课先完整跑通一讲的取材、编写和阅读验证，再处理其余讲次。整目录从 `batch plan` 或现有 `batch status` 开始，待补原件留在原顺位。

## 取材、编写和验收

- 写稿前用 `coverage` 建立独立范围。逐集检查原音、字幕和画面，逐件检查附件内部结构与作者渠道。把独立音频、档内录像和幻灯片内嵌视频纳入核对。先核实同版或重复关系，再复用取材。无法解码时保留原件、登记故障，继续处理可读材料。详见[工作流](workflow.md#本地教材工作流)。
- 写成一篇可跟做的教材。操作面向学习者，讲师亲历具名，新增演算标明示例。关键图贴对应动作，文末收起原课和资料链接。逐条核对原件是否写进正文，再逐段反查正文出处、案例条件与结论。发现缺项先补查并修正文。
- 用 `preview` 生成同版模板，按[验收说明](acceptance.md#本地教材验收)填写后运行 `accept`。首次绑定用 `deliver`，已有绑定由 `accept` 更新。用 `verify-reader` 重开原入口同版读到文末，再运行 `verify --require-vault`。详细参数见[开发手册](../../../docs/课程资料整理与%20Obsidian%20教材制作手册.md)。
- 发布时同步刷新日常库的 `00 入口/00 项目总览.md`，入口按 Obsidian 名称升序排列。
