# baidu-ai-batch 采用范围

核对版本：`jing1312/baidu-ai-batch`，`master`，`9fc7495edb03b5fde8d59b6aeef59ab5da0c3ab3`。本轮通过 GitHub 插件读取源码，不依赖营销描述估算完成度。

| 源码 | 可借鉴部分 | 我们需要补上的部分 |
| --- | --- | --- |
| [list-files.cjs](https://github.com/jing1312/baidu-ai-batch/blob/9fc7495edb03b5fde8d59b6aeef59ab5da0c3ab3/bin/list-files.cjs) | 按目录取得视频清单 | 保留文件 ID/路径/版本，递归和分页结束证据；不能把文件名列表当完整身份清单 |
| [extract-manuscript.cjs](https://github.com/jing1312/baidu-ai-batch/blob/9fc7495edb03b5fde8d59b6aeef59ab5da0c3ab3/bin/extract-manuscript.cjs) | 文稿标签与正文定位 | 不以超过 100 字代表全文；不以同名 TXT 存在代表可复用 |
| [export-ppt.cjs](https://github.com/jing1312/baidu-ai-batch/blob/9fc7495edb03b5fde8d59b6aeef59ab5da0c3ab3/bin/export-ppt.cjs) | 课件区导出入口 | 上游点击后等待约 2 秒便记录成功；本项目要求取得实际文件并验证 |
| [export-notes.cjs](https://github.com/jing1312/baidu-ai-batch/blob/9fc7495edb03b5fde8d59b6aeef59ab5da0c3ab3/bin/export-notes.cjs) | 图文笔记的独立 iframe 思路 | 用户本轮未选择额外生成笔记，不自动启用 |
| [lib/cdp.js](https://github.com/jing1312/baidu-ai-batch/blob/9fc7495edb03b5fde8d59b6aeef59ab5da0c3ab3/lib/cdp.js) | 作者自部署浏览器的接入实现 | 不接管调试端口来绕过当前受管浏览器拒绝 |

上游的文稿和课件标签使用 `.vp-tabs__header-item`，文稿正文使用 `.vp-ai-draft`，课件导出使用 `.ai-course__export-container`。这些是固定版本源码线索，实际执行必须先观察当前页面，不假设网站永不改版。

第一遍把文稿与课件分开检查；第二遍只处理已确认缺口。平台生成的课件页保留 `courseware_page` 性质，不自动升格为原视频关键帧。

上游使用 [MIT 许可证](https://github.com/jing1312/baidu-ai-batch/blob/9fc7495edb03b5fde8d59b6aeef59ab5da0c3ab3/LICENSE)，版权为 `Copyright (c) 2026 jing1312`。本轮为独立实现的调度与校验代码；若后续复制上游实质源码，必须随相应代码保留完整许可和版权声明。
