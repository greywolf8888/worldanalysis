# 验证记录

日期：2026-11-16（UTC+08）。环境：Linux / Python 3.12；在与远端 PR #1 的 `3068dd43cd36f92d3bb253aaf4a16a075a67058f` 文件树一致的隔离工作树中实施。本地原有未提交修改没有覆盖或清除。

## 新流程

```bash
python -B -m unittest tests.test_platform_first tests.test_platform_products tests.test_platform_pass_cli -v
```

首次组合运行：25 项通过，0 项失败，3.049 秒。随后调度恢复边界的小修完成后，对最终调度与 CLI 再运行：

```bash
python -B -m unittest tests.test_platform_first tests.test_platform_pass_cli -v
```

最终 13 项通过，0 项失败。产物校验模块未在两次运行间改动，其 12 项验证继续有效。以上合计 **25 个不同的新流程用例**，重复验证不重复计数。

覆盖实际风险：第一遍未过完不能补采；同名不同文件身份；不完整目录；只补缺失模态或明确区间；摘要与课件页不自动变成完整原文/源帧；生成提交未知的重启及跨枚举对账；额度/付费与安全拒绝的区别；源文件丢失/修改；短有效文字；空白和平台模板；字幕时间；坏/加密 PDF；PPTX 容器限制；路径与伪装媒体；本地校验不调用网络/Office/OCR、不写提取文件。

CLI 用例实际启动独立 Python 进程，完成构造清单登记、逐视频两项记录、第一遍屏障、生成精确计划、进程间恢复和文件变更复核。材料均明确标记 synthetic，不是用户课程内容。

## 既有资料接入与 Obsidian 回归

```bash
python -B -m unittest tests.test_platform_materials tests.test_obsidian tests.test_materials_obsidian -v
```

24 项通过，0 项失败，12.301 秒。验证资料导入、不可变证据发布、图像与内容变更、组合、Obsidian 导出及个人编辑保护。

本轮合计验证 **49 个不同用例**。这些结果证明所测本地合同与代码行为，不证明百度账号权益、云端生成质量、实际处理速度、并发或整课完成。

## 额外检查

- `python -B -m worldanalysis.platform_pass --help` 正常返回，命令与交接文档一致。
- 新增 Python 模块语法解析通过；`git diff --check` 通过。
- 原 `__main__.py`、`media.py`、`documents.py`、`store.py`、`platform_materials.py`、`obsidian.py` 与本次基线一致；新命令没有使既有提取器的源码指纹变化。
- 独立代码审阅发现并修复了“生成恢复时未持久化待对账状态”和“每记一项都重新解析所有旧课件”两个问题；最终复核未发现阻止离线模块提交的剩余明确缺陷。

## 真实网盘验收

访问 `pan.baidu.com` 被当前浏览器宿主的站点安全策略拒绝。拒绝说明明确：没有用户权限提示，没有执行自动审批，不得通过替代通道实现同一被拒绝动作。

因此本轮：最新目录未枚举；视频访问 0；文稿取得 0；课件取得 0；平台生成 0；收费 API 0；远程补采执行 0。

新模块没有内置浏览器采集或远程媒体执行器。用户此前描述的本机播放器捕获实现尚未在当前 GitHub 代码中核到；不能把补采计划宣称为已经自动补齐资料。

下一次真实执行以站点访问确实恢复为前提，重新枚举既定目录，先完成平台两项第一遍，再执行已确认缺口。不能用历史 7 个视频的清单或这 49 项本地验证替代这一验收。
