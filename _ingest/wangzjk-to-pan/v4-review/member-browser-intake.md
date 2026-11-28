# 网坠金库会员链接补采

这套操作接在 Worldanalysis 现有新加坡时间 06:00 和 18:00 的公开采集之后。先完成公开采集与正式资源库读回。会员来源使用用户已登录的应用内浏览器，续采前核对该站登录和会员状态。登录凭据留在浏览器内，项目记录站点、授权范围与断点。会话过期时保留断点，请用户在原站重新登录。所有转存和网盘操作仍由 DSH 按用户明确选择处理。

1. 在本目录运行 `python -B member_queue.py --limit 128` 检查首批新项目，再加 `--resume` 从历史断点继续。已有链接、已核过的非项目、当前修订已登记的权限项目和待复查项目会从常规批次剔除。需要核对这些项目时分别使用 `--only-access-required` 和 `--only-review`。原文修订发生变化时重新入队。
2. 用受支持的应用内浏览器在背景打开网坠金库站内页。按名单给出的原文 URL 正常导航，并使用 `member_browser_projection.js` 中的 `readMemberArticle` 做只读页面取值。原文路由、标题和可见下载控件必须相符。取到的分享地址只留在当前浏览器的工作变量里，勿把地址、提取码、临时跳转参数或会员凭据打印到聊天、终端和证据文件中。
3. 每组完成后马上导入已核实的地址。把每条地址整理成 `site_id`、`article_id`、`article_url`、`share_url`，用 `wa-v4-owner-share-locations.v1` 的 `items` 数组提交到 [本机导入页](http://127.0.0.1:8772/member-import)。输入用浏览器工作变量直接填写。先读回导入成功，再读正式 `/api/meta` 或该条详情。只保留与该分享同一处明确给出的有效四位提取码。
4. 出现站点级限流、验证码、登录失效或采集结果无法读回时，保留最近确认的断点。个别文章需要权限、仅提供原站下载或其他网盘、按钮指向图片、地址有异常字符或未显示入口时，保留原文，登记实见情况并继续其余项目。资料入口按当前原文复核。
5. 每组用 `member_progress.py --input -` 从标准输入写入 `wa-v4-member-batch-evidence.v1`。`items` 数组只包含 `article_id`、`article_revision`、`outcome`。历史组同时填开始时的 `before_id`，并使用 `--advance`。工具按正式读回验证 `linked`，按连续队列验证断点。没有看见入口记 `no_baidu_entry`，有按钮无地址记 `baidu_entry_pending`，文章需要权限记 `article_access_required`，当前页面无法读取记 `page_not_readable`。历史到底后重新核对待复查项目及页面已有变更的项目。

优先使用站点原始发布批次。拿到站长授权导出的 WordPress 文章 XML 后，先用 `publisher_wxr.py --owner-authorized --input <导出文件>` 预检，同已收录项目的真实链接交叉核对下载字段。读到可确认的字段后可加 `--meta-key <字段>` 看待补数量，并加 `--protected-out <本项目私有目录中的新 owner-*.dpapi 文件>` 收录。若原发布批次能直接提供原文和网盘链接清单，则使用 `share_locations.py --owner-authorized --input <清单> --protected-out <本项目私有目录中的新 owner-*.dpapi 文件>`。两路都按站点、文章 ID 和原文匹配，并通过正式资源库读回。
