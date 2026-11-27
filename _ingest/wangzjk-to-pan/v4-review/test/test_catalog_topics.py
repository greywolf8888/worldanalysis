import unittest

from catalog_topics import CATEGORIES, displayed_secondary_topic, intro_preview, project_topic


class CatalogTopicTest(unittest.TestCase):
    def test_project_purpose_and_use_of_tools(self):
        cases = {
            'AI写小说投稿实战课': ('content', '写作与小说'),
            '无需开店就能跟做的剪映剪辑课': ('media', '短视频制作'),
            'AI外贸客户开发实战课：用建站获客': ('crossborder', '外贸获客'),
            '互联网新个体创业课：商业思维、流量实战与个人成长': ('business','商业模式'),
            '闲鱼财商系统课': ('commerce', '店铺运营'),
            '负面情绪调节指南：健走写作游戏多重方法': ('life', '个人成长'),
            'Hypersnap v10.5.3便携汉化版': ('software', '影音工具'),
            'AI办公效率提升课：WPS AI全套': ('office', '表格与文档'),
            '抖音某博主图文带货实战课': ('commerce', '联盟带货'),
            '信息差零成本，日入500+，小白可学': ('',''),
        }
        for title,result in cases.items():
            with self.subTest(title=title):
                self.assertEqual(project_topic(title,(),''),result)

    def test_real_introduction_supplies_a_topic_when_title_is_vague(self):
        self.assertEqual(project_topic('保姆级实战公开课', ('以悬赏平台接推广任务，学习领取与交付的完整流程。',),''),
                         ('services','活动推广'))
        self.assertEqual(project_topic('某主任特训营：每节课都是经验分享', (), '课程内容：拼多多 […]'),
                         ('commerce','店铺运营'))
        self.assertEqual(project_topic('社媒加独立站实战课：私域网红营销教学',
                                       ('本套课程聚焦跨境出海实操，完整覆盖社媒引流与独立站运营。',),''),
                         ('crossborder','外贸获客'))

    def test_public_text_distinguishes_tasks_from_project_pitch(self):
        title='全程托管，零门槛小白可做'
        self.assertEqual(project_topic(title, (), ''), ('',''))
        self.assertEqual(project_topic(title, ('提交酒店截图任务，审核之后领取佣金。',), '',
                                       source_checked=True), ('services','技能接单'))
        self.assertEqual(project_topic(title, ('长期收益，手机版操作。',), '', source_checked=True),
                         ('reference','项目线索'))
        self.assertEqual(project_topic('付费文章：几件我遇到的事', ('有些话写给以前的朋友。',), '',
                                       source_checked=True), ('reference','经验与阅读'))
        self.assertEqual(project_topic('游戏交易市场自动掘金', (), '', source_checked=True),
                         ('gaming','游戏运营'))

    def test_news_requires_reporting_purpose(self):
        cases = {
            '2026年11月27日新闻早讯，每天60s读懂世界': 'news',
            'FDE行业趋势峰会资料合集+7天系统大课：AI企业服务': 'business',
            '小程序日活10W+，我做了哪些？实战经验分享': 'growth',
            'AI提示词高阶应用15讲｜商业行业案例全落地': 'automation',
            '用工作流自动生成每日新闻简报的脚本': 'automation',
            '经验分享：做了几件事': '',
        }
        for title, category in cases.items():
            with self.subTest(title=title):
                self.assertEqual(project_topic(title, (), '')[0], category)
        self.assertEqual(displayed_secondary_topic('automation',{'secondary_category':'news'}),'')
        self.assertEqual(project_topic('AI提示词高阶应用15讲', (), '', {'category':'news'})[0],'automation')

    def test_classifies_the_deliverable_instead_of_the_tool_or_examples(self):
        cases = {
            'WorkBuddy短视频自动化实战课：利用AI自动写文案、配音、生成字幕': ('media','短视频制作'),
            '抖音汽车解说Skill+视频教程，汽车号内容创作': ('media','影视解说'),
            '普通人如何从零开好淘宝店？商品上架及万相台推广技巧': ('commerce','店铺运营'),
            '豆包AI+实体门店获客，了解同城推广': ('local_business','门店经营'),
            '同城高效相亲匹配项目，私域沟通技巧': ('services','本地服务'),
            'AIGC漫剧案例实战教学：从剧本到成片': ('screen','短剧与漫剧'),
            '红果短剧电脑版+TV版本来了，同步追剧安装即用': ('software','影音工具'),
            '高品质素材内容！全套语音音效合集，49000个文件': ('assets','音乐与音效'),
            '短视频制作教程，附赠设计素材和封面模板': ('media','短视频制作'),
            '剪映模板拉新项目，操作简单，人人可做': ('services','活动推广'),
            'AI做PPT爆单，0基础月入2W，附万能PPT模板': ('services','技能接单'),
            '哔哩哔哩最新视频下载工具，支持合集下载和字幕下载': ('software','影音工具'),
            'Adobe Photoshop 2026 v27.11.0.26': ('software','设计工具'),
        }
        for title, result in cases.items():
            with self.subTest(title=title):
                self.assertEqual(project_topic(title, (), ''),result)

    def test_description_drops_heading_and_duplicate_title(self):
        title='掌握 AI 工具'
        self.assertEqual(intro_preview(title, ('课程内容简介', title, '课程介绍：通过多种场景练习搭建实用的智能体工作流。'), ''),
                         '通过多种场景练习搭建实用的智能体工作流。')
        self.assertEqual(intro_preview('一套关于从零开始搭建小店经营全流程的实操教学', (),
                                      '一套关于从零开始搭建小店经营全流程[…]'), '')

    def test_reviewed_topic_wins_and_empty_is_not_a_public_facet(self):
        self.assertNotIn('unsorted',CATEGORIES)
        self.assertEqual(project_topic('练习口播剪辑',(),'',{'category':'growth','subcategory':'账号起号'},manual=True),
                         ('growth','账号起号'))
        self.assertEqual(project_topic('手机屏幕搬上电脑！开源投屏神器GameScrcpy', (), '', {'category':'technical'}),
                         ('software','影音工具'))
        self.assertEqual(project_topic('每天做一点就有收益',(),'',{'category':'unsorted'}),('',''))

    def test_primary_purpose_reclassifies_older_automatic_decisions(self):
        cases = (
            ('抖音AI创作者计划实操课程', ('创作者开通发布与分成计划。',), 'reference', ('growth', '账号起号')),
            ('AI短剧工作流实战课：脚本到成片', ('课程核心是短剧制作。',), 'automation', ('screen', '短剧与漫剧')),
            ('酒店民宿线上获客系统课：抖音起号和直播', ('系统学习酒店民宿的获客和开店经营。',), 'growth', ('local_business', '门店经营')),
            ('小红书无货源0粉电商课：图文方法', (), 'content', ('commerce', '店铺运营')),
            ('微信小游戏制作付费课程', (), 'gaming', ('gaming', '游戏开发')),
            ('飞书多维表格自动化训练营', (), 'automation', ('office', '表格与文档')),
        )
        for title, paragraphs, previous, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(project_topic(title, paragraphs, '', {'category': previous}, source_checked=True), expected)

    def test_platform_and_tool_names_do_not_define_the_primary_topic(self):
        cases = (
            ('GitHub项目：开源视频压缩工具', ('可批量压缩视频并保留画质。',), ('software', '影音工具')),
            ('GitHub上的人生指南', ('汇集提高生活质量的人生经验。',), ('life', '个人成长')),
            ('AI和抖音红利来了：三天就能上手', ('项目收益由个人实际情况决定。',), ('reference', '项目线索')),
            ('抖音同城直播实训', ('本课程主要教餐饮门店获客。课程案例包括短视频制作。',), ('local_business', '门店经营')),
            ('平面设计实战：附赠电商带货教程', ('本课程主要学习平面设计和海报。',), ('design', '平面与作图')),
        )
        for title, paragraphs, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(project_topic(title, paragraphs, '', source_checked=True), expected)

    def test_identifies_software_products_and_what_the_tutorial_teaches(self):
        cases = (
            ('图文克隆工具，视频教程', ('以下是部分功能的视频展示。',), ('software', '应用软件')),
            ('Windows智能题库生成器！导入文档后可生成题目', ('软件读取PDF和Word。',), ('software', '应用软件')),
            ('实时变声软件教学', ('想在动画制作中切换说话声音？',), ('media', '录音与配音')),
            ('即梦AI全能创作课：图像和视频', ('课程覆盖软件全功能，从图像到视频生成。',), ('automation', 'AI 工具')),
            ('即梦AI短视频制作课', ('本课程重点教用同一角色制作短视频。',), ('media', '短视频制作')),
            ('AE模板高效修改课：学会改模板、装插件', ('内容包含 AE 软件下载安装和视频剪辑实操。',), ('media', '短视频制作')),
        )
        for title, paragraphs, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(project_topic(title, paragraphs, '', source_checked=True), expected)

    def test_ancillary_sections_do_not_define_topics_and_curriculum_still_counts(self):
        cases = (
            ('入门实战课', ('适合人群：', '电商直播带货和广告投放从业者。',
                         '课程目录：', '本课程主要教实时变声实操。'), ('media', '录音与配音')),
            ('9个必赚的小项目', ('给大家把一些必赚的小项目整理了一个清单。',
                            '小项目大家可以自己实操变现，也可以拿出去送人引流。'), ('reference', '项目线索')),
            ('办公入门项目', ('课程介绍：', '项目规则与任务要求。',
                         '提交酒店截图任务，审核之后领取佣金。'), ('services', '技能接单')),
        )
        for title, paragraphs, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(project_topic(title, paragraphs, '', source_checked=True), expected)

    def test_primary_outcomes_distinguish_similar_projects(self):
        cases = (
            ('亚马逊FBA物流实操课，整理商品库存', ('课程涵盖卖家仓储和库存规划。',), ('crossborder', '跨境店铺')),
            ('亚马逊KDP出版实战课，教你制作和销售书籍', ('后续学习书籍广告投放。',), ('content', '写作与小说')),
            ('朋友圈自动成交营，学写文案', ('课程重点是成交话术和销售策略。',), ('growth', '转化营销')),
            ('小红书CPA拉新实战课，包含短视频曝光技巧', ('完成拉新和拉活任务后按单计酬。',), ('services', '活动推广')),
            ('AI赋能SEM竞价培训课', ('介绍OCPC投放及组织分工。',), ('growth', '广告投放')),
            ('即梦AI精细控图课', ('训练构图、风格迁移与景深控制。',), ('design', '平面与作图')),
            ('付费文章：男女在情感中如何相处', ('文章分析两性择偶心理。',), ('life', '个人成长')),
            ('视频号分成计划：两性情感视频', (), ('growth', '账号起号')),
        )
        for title, paragraphs, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(project_topic(title, paragraphs, '', source_checked=True), expected)

    def test_routes_projects_using_the_business_outcome_and_content_form(self):
        cases = (
            ('亚马逊产品开发课程，研究市场与选品', (), '', ('crossborder', '跨境店铺')),
            ('Facebook中东商城实战课', ('课程讲解广告投放、商品选品与自发货。',), '', ('crossborder', '跨境店铺')),
            ('知识付费卖项目实操，按份销售', (), '', ('commerce', '虚拟商品')),
            ('文旅商家AI内容创作课', ('最终所有教学都指向将线上流量转化为门店营收。',), '', ('local_business', '门店经营')),
            ('抖音动画科普实战课：科普文案、字幕音效与视频发布', (), '', ('media', '短视频制作')),
            ('虚拟视频制作实战课：CC4建模、Blender渲染、Iclone动画', (), '', ('screen', '动画与特效')),
            ('宠物托运项目，介绍定价和订单执行', (), '', ('services', '本地服务')),
            ('番茄小说拉新实操，无脑发视频，每单十元', (), '', ('services', '活动推广')),
            ('国外网赚项目合集：从建站到流量', (), '汇总二十种玩法，覆盖20余个海外项目。', ('reference', '项目线索')),
        )
        for title, paragraphs, excerpt, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(project_topic(title, paragraphs, excerpt, source_checked=bool(paragraphs)), expected)
