# 平台文稿与课件先遍历，再按缺口补采

这是用户在 2026-11-16（UTC+08）选择的新采集顺序。本目录替代旧交接中“先反复打磨一个媒体样本，再扩展整课”的执行次序；保留原项目的来源、不可变证据、范围和资源要求。

## 交付目标与本轮状态

先完整枚举选定网盘目录，再对每个视频分别取得平台文稿和课件。整个目录第一遍结束后，按确认的文字或画面缺口补采。完整视频、完整音频不落本地；已有好材料继续复用，最终文案、图片、必要的课件原件和索引进入已有证据库及 Obsidian。

本轮实现的是本地调度、产物验证和补采计划，**没有内置或绕接一个百度浏览器客户端，也没有完成真实目录采集**。访问 `pan.baidu.com` 时宿主站点安全策略拒绝了观察权限，没有用户授权提示，也没有执行自动审批。第一遍实际访问视频数、取得文稿数、取得课件数均为 0；平台生成数和完整媒体下载数也为 0。详见 [本轮执行记录](02_EXECUTION_RECORD.json)。

起始目录沿用历史课程：`/apps/教育分析学习/待分析/163378_AI视频创作`，包括其子目录。历史读回清单曾列出 7 个视频，只用于定位；恢复访问后必须重新枚举，不把历史数量作为本轮已读或永久完整的证明。任务不扩展到其他课程，不重复转存既有课程。

## 第一遍的固定顺序

1. 通过当前实际获准的访问方式，完整枚举目标目录及子目录。保留账号别名、个人网盘文件 ID、完整路径、版本线索、大小和已知时长。文件 ID 用字符串保存；同名不同 ID 不共用状态。
2. 对每个视频核对身份，检查文稿和课件两个独立产物。第一项缺失或生成失败，仍检查第二项，再继续下一视频。
3. 已有结果直接读取；本次用户已选择的平台生成仅使用现有免费或会员权益。打开标签可能触发生成，也按生成动作对待。额度不足或付费提示关闭新增生成，仍可以检查后续已有结果；不购买、不充值、不自动改用收费 API。
4. 文稿保存实际正文；课件导出后还要确认文件身份并取回可读 PPTX/PDF。点击导出、页面出现成功提示或文件名存在，都不能代替本地文件验证。
5. 每项记录状态及回执。等待中或提交结果未知时只查询、对账，不重新点击生成。
6. 完整清单中的每条视频两项都已经实际检查后，才能结束第一遍并建立补采计划。第一条缺材料时，不能立刻播放整条视频补采而跳过剩余平台检查。

本轮没有新增图文笔记生成任务：用户当前选的是课件和文稿，不能顺便消耗第三种产物额度。

## 第二遍如何补

| 第一遍取得的证据 | 后续处理 |
| --- | --- |
| 已有合格完整文稿，缺真实画面 | 只补画面，保留文稿 |
| 已有足够真实画面，缺文稿 | 只转写缺失音频范围，保留画面 |
| 文字或画面有明确缺失区间 | 仅规划这些区间，必要的上下文重叠由执行器另记 |
| 只有摘要或整理笔记 | 保留辅助材料；全文转写仍是缺口 |
| 文稿、课件已到本地，但覆盖未知 | 进入质量复核，不自动把整条视频重新处理 |
| 平台任务仍在生成或提交结果未知 | 留在查询/对账队列，不与补采重复运行 |
| 原已验证本地文件后来丢失或被修改 | 报告文件失效并要求复核，不复用旧哈希假报成功 |
| 登录、权限、来源身份或宿主策略拒绝 | 停止该次运行；不将安全拒绝改道为媒体补采 |

“课件缺失”由我们的办法补成真实截图和文字时，产物仍应叫源帧/派生资料，不能冒充百度原生课件。平台生成的 PPT 页面、原视频截图、模型画面说明分别保留来源。

## 代码入口

- [`worldanalysis/platform_first.py`](../../worldanalysis/platform_first.py)：同一个 `Store.connection` 中保存目录运行、逐产物观察和第一遍状态；平台生成使用现有 `jobs` 提交与对账机制。
- [`worldanalysis/platform_products.py`](../../worldanalysis/platform_products.py)：只读校验实际本地文稿、PPTX/PDF，计算内容哈希；复用已有文档解析，不运行 OCR、模型、Office 或远程请求。
- [`worldanalysis/platform_pass.py`](../../worldanalysis/platform_pass.py)：独立命令行入口，管理真实适配器带回的材料和回执，不自行访问网页；原媒体/文档入口不改，避免配置指纹失效导致重复提取。
- [`worldanalysis/platform_materials.py`](../../worldanalysis/platform_materials.py)、[`worldanalysis/documents.py`](../../worldanalysis/documents.py)：继续承担资料包和文档解析。
- [`worldanalysis/obsidian.py`](../../worldanalysis/obsidian.py)：继续从已发布 Bundle 生成阅读库；不在 Obsidian 复制一套业务状态。

文件验证只证明“这个本地产物可读且哈希匹配”。来源声明不自动变成已核实百度绑定；文件可打开也不代表全文或全部关键操作已覆盖。现有 Bundle 中保守的 `temporal_gap`、`missing_visual` 等标记，不直接触发整片重采。

## 适配器输入与命令

适配器需要一份最新枚举清单，格式如下。这里是契约示意，不是本轮真实清单：

```json
{
  "schema_version": "1.0",
  "inventory_id": "current-enumeration-reference",
  "account_ref": "account-alias",
  "root_path": "/apps/教育分析学习/待分析/163378_AI视频创作",
  "enumeration_complete": true,
  "evidence_ref": "actual-enumeration-receipt",
  "evidence_class": "real_authorized_browser",
  "files": [
    {
      "file_id": "123456",
      "path": "/apps/教育分析学习/待分析/163378_AI视频创作/lesson.mp4",
      "source_revision": "observed-file-version",
      "size_bytes": 100000,
      "duration_ms": 115880
    }
  ]
}
```

`evidence_class` 说明回执种类，不授予访问权限，也不是完整采集证明。旧枚举记录不可伪装成新读回。构造验证须使用 `synthetic` 并显式传入 `--synthetic-input`。

以下使用现有工作区和项目 ID；尖括号为适配器实际返回值。清单、观察 JSON 和产物路径必须在工作区中：

```powershell
python -m worldanalysis.platform_pass --root G:\Worldanalysis begin --project <project-id> --inventory <inventory.json>
python -m worldanalysis.platform_pass --root G:\Worldanalysis next --run <run-id>
python -m worldanalysis.platform_pass --root G:\Worldanalysis record --run <run-id> --file-id <fs-id> --product manuscript --observation <observation.json>
python -m worldanalysis.platform_pass --root G:\Worldanalysis record --run <run-id> --file-id <fs-id> --product courseware --observation <observation.json>
python -m worldanalysis.platform_pass --root G:\Worldanalysis seal --run <run-id>
python -m worldanalysis.platform_pass --root G:\Worldanalysis plan --run <run-id>
python -m worldanalysis.platform_pass --root G:\Worldanalysis status --run <run-id>
```

取得文稿的观察示意：

```json
{
  "state": "present",
  "evidence_ref": "actual-manuscript-export-receipt",
  "relative_path": "手动投放/course/lesson/文稿.txt",
  "content_kind": "transcript",
  "coverage": {"state": "unknown"}
}
```

覆盖充分必须附独立质量回执，文稿依据为 `full_transcript`，真实画面依据为 `source_frames`。已知局部缺口使用 `coverage.state=partial` 和 `missing_intervals`，每段用整数毫秒 `start_ms/end_ms`。不因字数、图片张数或文件存在自动生成这些质量结论。

已有合格产物不能被后续一次失败观察覆盖；视频源版本改变应建立新的枚举版本，不沿用旧完成状态。

## 生成权限与执行边界

仓库全局 `Policy.request_new_generation` 继续默认关闭。本次任务的宿主可以根据用户已经给出的选择，为本目录文稿/课件建立任务级 `Policy(request_new_generation=True)`；这不需要再次询问同一业务选择。

真正发出生成请求仍要通过现有 `Store.begin_submission()` 和宿主的 `submission_authorizer`，先持久化提交未知状态，再发生页面动作。命令行没有伪造授权的参数，也不会把一份清单、配置或模型计划当作权限。当前宿主已经拒绝的网盘访问必须保持阻断。

公开 GitHub 尚未包含用户在本机测试的那套播放器捕获代码；现有 `media.py` 仍只支持本地媒体。新模块输出的是按缺口划定范围的补采计划，不会把本地文件入口或历史补尾伪装成已经实现的纯远程自动补采。

恢复后交给 Agent 的下一步是：重新枚举真实目录，按第一遍顺序取得文稿和课件，再把计划交给实际存在、当前获准且能力匹配的补采执行器。不把本轮离线检查数当作网盘视频完成数。

## 参考与验证

[上游源码审阅](01_UPSTREAM_REVIEW.md) 固定到 `jing1312/baidu-ai-batch@9fc7495edb03b5fde8d59b6aeef59ab5da0c3ab3`。本次参考其页面动作和批量流程，未复制其 CDP 客户端或以之绕过宿主。

[实际验证记录](03_VALIDATION.md) 分别记录离线测试与真实网盘阻塞，二者不互相替代。
