# Worldanalysis 专用 DSH

操作沿[专用 DSH Skill](../.agents/skills/worldanalysis-dsh-operations/SKILL.md)和[教材归档规则](收尾实施与教材归档规则.md)。部署时单独配置会话、账号授权、接收目录与确认范围。控制台只按实际记录显示转存和下载状态。

## Windows 桌面启动

先安装官方 DeepSeek Harness 和 Python 3.11 或以上版本。双击项目根目录的 `启动 Worldanalysis DSH.cmd` 即可打开桌面程序。也可以在项目根目录运行以下命令，生成无控制台窗口的 `Worldanalysis 专用 DSH.lnk`，以后双击该快捷方式打开。

```powershell
& .\_tools\New-WorldanalysisDshShortcut.ps1
```

在 DSH 欢迎页选择「添加 API Key」→「稍后配置」，即可免登录进入本地工作区，无需填写凭据。完全退出后重新启动时，当前官方桌面版会再次显示欢迎页。调用模型时需要配置该模型，百度网盘操作仍使用原有授权。 [官方首次启动说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md#startup-onboarding)

桌面启动器使用已登记且签名有效的官方安装。若项目已准备官方发行副本，则优先使用与该副本的程序及应用包一致的安装，以兼容本机项目内副本在窗口显示前退出的情况。会话、桌面数据和启动记录都保存在项目的 `.runtime/dsh-worldanalysis`。

## 专用 Web 与桥接

桌面启动无需配置桥接。准备 Web 与桥接时，先将指定版本的插件包保存在项目运行目录。准备步骤核对插件包指纹和官方程序签名，安装文件、配置、插件缓存和验证回执均保存在项目的 `.runtime/dsh-worldanalysis`。

```powershell
New-Item -ItemType Directory -Force .runtime/v4-dsh-bridge-install | Out-Null
npm pack dsh-mcp-bridge-server@1.1.0 --pack-destination .runtime/v4-dsh-bridge-install
```

```powershell
python -B -X utf8 _tools/dsh_dedicated.py prepare '<已安装的官方发行目录>'
python -B -X utf8 _tools/dsh_dedicated.py verify
python -B -X utf8 _tools/dsh_dedicated.py status
python -B -X utf8 _tools/dsh_dedicated.py preflight
python -B -X utf8 _tools/dsh_dedicated.py preflight --task-id <已有的任务编号>
python -B -X utf8 _tools/dsh_dedicated.py serve --open
python -B -X utf8 _tools/dsh_dedicated.py desktop
```

`verify` 使用专用 `DSH_HOME` 启动本机 Web Profile。Web 和 MCP 各自使用系统分配的本机端口。核对发现记录、运行进程和健康端点，再检查无凭据请求被拒绝、初始化协议、工具菜单、专用会话和官方 Web 页面的读回。验证结束后关闭本次启动的 Host 进程。Web 的一次性登录凭据只在读回时保留在内存中。MCP 本地凭据由插件生成，存于专用服务发现文件。临时工作文件使用项目目录内的 `spill`。

`serve --open` 在本机打开官方界面，可按正常流程登录专用 Profile。关闭时在启动终端按 Ctrl+C。正式切换前可使用该入口验证本机访问和人工登录。

## 派发前检查

`preflight` 输出版本为 `1` 的 JSON。`bridge.connected` 来自专用发现记录与本机健康端点一致的当前读回。`bridge.last_verified_at` 记录完整握手的最近成功时间。带任务编号时，检查共享任务和既有回执是否匹配任务、原执行会话及文件指纹。`handoff.authorization` 只报告授权引用是否存在，正式授权由现有 V4 门禁核验。检查过程只读取记录并访问本机健康端点。

正式执行初始暂停。使用正常授权入口核对当前专用会话、实际工作区、账户、容量、任务接收和回执。正式任务须绑定当前专用会话。平台验证、旧队列暂停与任务授权分别检查。遇到宿主工具发布不可用，保留已提交尝试和原回执位置，从专用宿主的授权界面恢复所需能力。

V4 转存以 `_exchange/control/tasks/<任务>.json` 的指纹绑定任务。该任务须引用 `_exchange/control/decisions/` 下用户明确确认的当次范围和固定顺序，每项须引用 `_exchange/dsh/catalog/` 中按实际目录展开核实的范围。DSH 核对实时账户与容量，提交前仍须走 `_exchange/dsh/plans/` 与 `_exchange/dsh/authorizations/` 的正式门禁。下载按对应同版实际收件核对范围并交接分析。清理由独立门禁处理。

读回旧 DSH 转存确认时，由部署环境以 `WORLDANALYSIS_LEGACY_DSH_SESSION_ID` 提供已核实的原执行会话。只在本机配置该值。进程缺少绑定时，保留相关待核交接，完成配置核对后再读回。

## 网页申请和正式派发

1. 在网页选择确切资料及顺序，打开「核对转存」后提交范围。申请与只读任务分别写入 `_exchange/control/transfer-requests` 和 `_exchange/control/tasks`。只读任务编号以 `-scope` 结尾。
2. 在项目根用 `python -B -X utf8 _tools/dsh_dedicated.py notify-readback <申请编号>-scope` 通知专用接收端。回执引用原申请和按顺序排列的递归清单。带任务编号调用 `preflight` 可读回尝试、接收及回执状态。
3. 路由暂停且平台已由正常授权解除当次暂停时，操作者进入资源库代码目录，运行 `python -B -X utf8 transfer_requests.py approve <申请编号>`。看到实际文件、目录、容量和范围后，亲自输入显示的确认文字。浏览器申请和只读回执不生成用户授权。
4. 在项目根以 `cutover inspect` 读回版本，再执行 `cutover enable --revision <当前版本> --task-id <申请编号> --readback-task-id <申请编号>-scope`。满足实时门禁后，可运行 `python -B -X utf8 _tools/dsh_dedicated.py notify-transfer <申请编号>`。在途任务先读回真实执行结果。退回暂停使用 `cutover rollback --revision <当前版本>`。

项目相对路径保存只读任务、回执和新任务的收件位置。跨目录部署时重新登记宿主与实际工作区，再核当前会话与账号。换专用会话前对账已尝试的任务，随后用 `rebind-desktop <会话编号>` 读回并登记新会话。

公共仓库提供桌面入口、专用派发、路径读回及正式范围校验。现场转存适配器、平台账号和下载能力由部署环境另行安装和核实。部署后的执行、下载状态由专用 DSH 的实际回执与实物确认。

## 插件与浏览器

专用 `package.json` 的 `dsh.profile.bundles` 使用官方 `@deepseek-ai/dsh-base`、`@deepseek-ai/dsh-web-app` 和项目内 `dsh-mcp-bridge-server`。可通过官方插件管理加载专属插件。签名发行件随 Web 组合包提供创造预设 `cordis`。项目专用 Profile 已启用官方内置的自定义插件索引，官方配置读回确认插件市场、发现页面和创造预设均已纳入配置。打开专用界面后在「插件 → 发现」管理索引。新增索引及安装其中的插件按实际授权执行。

专用实例停止时可运行 `python -B -X utf8 _tools/dsh_dedicated.py market enable` 启用，或用 `market disable` 关闭。两条命令只修改专用 Profile 的内置组合包选择，并通过官方配置组合检查结果。检查失败会恢复原选择。

该发行件默认只在 `desktop` Profile 启用内置浏览器侧栏。官方桌面壳另用 Electron `userData` 保存数据，并持有单实例锁。正式接管前需核对受支持的专用浏览器入口、独立浏览器数据及实际授权会话。
