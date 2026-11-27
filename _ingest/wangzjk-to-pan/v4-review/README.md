# Worldanalysis 本机资源库

资源库和课程表直接显示全部业务分类和平台选项，随屏宽换行。点「小红书」可查看适用项目，再选「电商与带货」看交集。平台识别使用项目标题和已保存的简介，识别实际经营、发布或推广渠道，支持一个项目对应多个平台。GitHub 根据实际仓库或明确的发布、使用和教学语境识别。内容不足的项目可在全部资料和搜索中查看。课程表按原站发布日期分组，详情可以展开其他来源及版本。

主题按项目主要用途划分。课程里的案例、软件和赠品保留在原项目介绍里。独立软件、素材、门店经营和短剧制作各有对应类别。新闻简报在资源库的「新闻与行业资讯」浏览。

业务分类从标题主题、原站简介中的明确目标和实际交付物提取证据。多个用途同时出现时，优先匹配主要教学目标或交付物，降低附赠内容和举例的影响。证据充分时可更新历史自动分类，人工确认仍按对应版本保留。公开内容只提供项目线索、课程资料、经验阅读或操作方法时，归入「综合参考」。站点说明仍可在资源库查阅。

控制台汇集公开采集、会员资料入口、转存、本地下载、正式分析、独立验收、教材保全和清理。公开采集、连接检查和流程读回会写入本机运行记录。各阶段的状态来自现有目录、交接和本地核验记录。

资源库和课程表分别展示转存与分析状态。转存来自已核回执，历史回执显示为「曾转存」。正式分析跟随本地批次和实际教材，按同版验收和已收到的范围显示。只凭唯一完整项目名对应到教材时显示「已有本地分析」。缺少依据的状态留空。浏览、翻页或刷新时读取最新的本机记录。

## 启动

使用 Windows PowerShell 和 Python 3.12 或较新版本。在已有 Worldanalysis 工作区根目录运行以下命令。`-Python` 可指定项目现用的解释器，`-Port` 可选用当前服务端口。服务仅监听本机回环地址。

```powershell
python -m pip install lxml==6.0.2 defusedxml==0.7.1
cd _ingest/wangzjk-to-pan/v4-review
.\ops.ps1 -Action start
```

- [资源库](http://127.0.0.1:8767/)
- [课程表](http://127.0.0.1:8767/courses)
- [控制台](http://127.0.0.1:8767/control)

使用项目内已有的 `../state/` 和 `data/public-details.json`。公开目录、转存草稿、分析顺序和运行记录也保存在 `data/`。本地进度读取已部署的正式投影，DSH 连接检查读取已安装的原 MCP 适配器。公开仓库提供通用代码和 [隔离测试](test/)，真实目录、会员分享地址、凭据和课程材料由各工作区在本机保管。

## 日常操作

```powershell
.\ops.ps1 -Action status        # 查看各阶段及最近运行
.\ops.ps1 -Action catalog-progress # 核对目录中的转存与分析状态
.\ops.ps1 -Action collect       # 启动一次公开采集并读回资源库
.\ops.ps1 -Action verify        # 核对整条流程的现有读回
.\ops.ps1 -Action check_dsh     # 只读检查原 DSH 会话
.\ops.ps1 -Action member        # 读取会员补录断点
.\ops.ps1 -Action member-review # 查看需复查的会员入口
.\ops.ps1 -Action member-access # 查看单篇权限受限项目
.\ops.ps1 -Action next          # 使用正式入口查询下一项分析任务
```

公开采集使用同一文件锁，控制台与脚本共享同一运行记录。会员资料通过用户已登录的浏览器核对并导入。选定资源后使用原有确认流程，DSH 按确认范围执行转存。下载沿用本地入口。提交分析顺序后，正式分析与独立验收各自沿用当前工作路径。清理前核对教材可独立阅读，以及原件身份和实际范围。

分析 Agent 完成验收、发布和批次登记后，网站会读回同一台账。项目改名或出现多版时，具名分析 Agent 可以在正式接力里补齐站点、文章和当前资料版本的对应依据。核对单篇状态可执行以下命令。

~~~powershell
.\ops.ps1 -Action catalog-progress -CatalogArguments @('--article','164145')
~~~

## 简介识别与续作

简介、分类、关系和人工修订保存在已有的 `data/pipeline-runs.sqlite3`。公开采集沿用现有唯一入口和 12 小时计划，先复用已经保存的正文。新文发现和旧文修改检查各自续跑。正文不足的项目可以继续浏览及搜索。

站点适配器通过 `catalog_evidence(article_id)` 提供本条目的目录元数据和可见简介。资源库按站点与文章号读取同一版本的证据并运行统一分类。新采集或更新的正文在资源库刷新时自动生效。后续采集会单独保留正文可见的 GitHub 仓库地址，已脱敏的历史正文可继续用明确的标题和简介识别平台。

在本目录用项目解释器运行下列命令。导出后，当前宿主 Agent 阅读 JSONL 里提供的完整段落范围，再提交结构化 JSONL。日常浏览只读本机结果。

```powershell
.\ops.ps1 -Action catalog -CatalogArguments @('inventory')
.\ops.ps1 -Action catalog -CatalogArguments @('status')
.\ops.ps1 -Action catalog -CatalogArguments @('audit-topics', '--category', 'news', '--sample', '8')
python -X utf8 -B catalog_tasks.py export --limit 20 --output data/catalog-tasks/next.jsonl
python -X utf8 -B catalog_tasks.py import data/catalog-tasks/next-results.jsonl
python -X utf8 -B catalog_tasks.py resume --output data/catalog-tasks/unfinished.jsonl
python -X utf8 -B catalog_tasks.py export --kind relation --targets 20 --candidate-limit 30 --output data/catalog-tasks/relations.jsonl
```

- 每份结果行原样带回任务的 `schema`、`rules`、`task_id`、`generation`，判断写入 `result`。依据逐条使用 `title`、`paragraphs` 或 `excerpt` 中可找到的片段。关系结果要注明 `left`、`right`。字段、枚举及验证逻辑见 [catalog_semantics.py](catalog_semantics.py)。
- `resume` 重建尚能继续的任务文件。已失效的旧任务需重新 `export`。确认要重新领取相同输入时加 `--reclaim`，之前领取的结果将被拒绝。重复导入同一结果会读回原状态。
- 分类修正用 `catalog_tasks.py correct --article 文章号 data/catalog-tasks/correction.jsonl`。文件包含一行带原文依据的完整分类结果。关系修正用 `correct-relation --pair 文章号一,文章号二 文件`。已有关系可用 `undo 关系标识` 撤销。人工修订会保留记录，原文有变化时待重新核对。
- 确认同版的来源可以折叠查看。存在明确版次或日期证据时才优先展示新版。选择转存或分析时仍以具体来源和现有资料身份为准。归属未能唯一确认的条目可浏览，操作待核对。

待补原文的存量可用同一公开采集入口按清单续作。采集完成后，资源库会用新取得的原文重新判断用途。

~~~powershell
.\ops.ps1 -Action catalog -CatalogArguments @('audit-topics', '--category', 'unsorted', '--queue-output', 'data/catalog-tasks/pending-intros.ids')
python -X utf8 -B collect_public.py --pages 0 --details 300 --detail-list data/catalog-tasks/pending-intros.ids
~~~

清单每行一个原站文章号。已有成功记录的同版正文会跳过，来源访问受限时保存断点。原站 12 小时的公开更新仍走同一采集入口。

## 验证

```powershell
python -B -m unittest discover -s test -p test_course_calendar.py
python -B -m unittest discover -s test -p test_pipeline_control.py
```

控制台的「检查全链路」读回目录、百度入口、转存复核、本地进度、分析顺序和清理条件。实际转存和删除需要相应项目范围、当前现场证据及用户确认。

交互参考 [课程日期列表](https://vip.abab9.cn/post/)、[Airflow 运行概览](https://airflow.apache.org/docs/apache-airflow/stable/ui.html)和 [Dagster 运行记录](https://dagster.io/docs/guides/operate/webserver)。
