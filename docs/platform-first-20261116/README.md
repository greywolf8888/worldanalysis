# 平台资料采集与归档

平台流程从明确授权的目录和稳定文件身份开始。先用一个资料项目核对来源，再处理目录中已取得的文稿、课件和附件。记录缺失部分，在取得授权和原件后补齐。目录移动不改变已核实的资料身份和本地证据。

运行云端变更前，在本机设置 `WORLDANALYSIS_PROTECTED_SESSION`，或通过调用方配置 `LifecycleConfig.protected_sessions` 提供已受保护的原会话标识。缺少配置时，变更会等待完成权限配置。状态和队列可继续只读核对。

## 命令入口

在项目根运行以下命令查看各自支持的输入和核对方式。

~~~powershell
python -B -m worldanalysis.platform_pass --help
python -B -m worldanalysis.platform_materials --help
python -B -m worldanalysis.cloud_lifecycle --help
python -B -m worldanalysis.obsidian --help
~~~

- 平台资料接入保留原文、素材身份和已取得范围。普通平台图片与能够核回原视频的画面分别记录。
- 云端位置变化需要当前权限、会话所有权、资料身份和目标位置再次核对。云端任务由现有授权流程执行。
- 本地结果可导出到 Obsidian。内容验收和云端归档分别查看各自的现场结果。

已有字幕、笔记、课件和图片可按[本地资料接入指南](../obsidian-20261115/README.md)整理。平台接口和证据类型见[上游研究](01_UPSTREAM_REVIEW.md)。
