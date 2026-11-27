# Worldanalysis 本机资源库

资源库按主题筛选资料，并保留转存选择与顺序。课程表按发布日期分组，向下滚动自动续载，也可按主题、来源、关键词和日期定位。点击课程名进入同一份项目详情。

控制台汇集公开采集、会员资料入口、转存、本地下载、正式分析、独立验收、教材保全和清理。公开采集、连接检查和流程读回会写入本机运行记录。各阶段的状态来自现有目录、交接和本地核验记录。

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
.\ops.ps1 -Action collect       # 启动一次公开采集并读回资源库
.\ops.ps1 -Action verify        # 核对整条流程的现有读回
.\ops.ps1 -Action check_dsh     # 只读检查原 DSH 会话
.\ops.ps1 -Action member        # 读取会员补录断点
.\ops.ps1 -Action member-review # 查看需复查的会员入口
.\ops.ps1 -Action member-access # 查看单篇权限受限项目
.\ops.ps1 -Action next          # 使用正式入口查询下一项分析任务
```

公开采集使用同一文件锁，控制台与脚本共享同一运行记录。会员资料通过用户已登录的浏览器核对并导入。选定资源后使用原有确认流程，DSH 按确认范围执行转存。下载沿用本地入口。提交分析顺序后，正式分析与独立验收各自沿用当前工作路径。清理前核对教材可独立阅读，以及原件身份和实际范围。

## 验证

```powershell
python -B -m unittest discover -s test -p test_course_calendar.py
python -B -m unittest discover -s test -p test_pipeline_control.py
```

控制台的「检查全链路」读回目录、百度入口、转存复核、本地进度、分析顺序和清理条件。实际转存和删除需要相应项目范围、当前现场证据及用户确认。

交互参考 [课程日期列表](https://vip.abab9.cn/post/)、[Airflow 运行概览](https://airflow.apache.org/docs/apache-airflow/stable/ui.html)和 [Dagster 运行记录](https://dagster.io/docs/guides/operate/webserver)。
