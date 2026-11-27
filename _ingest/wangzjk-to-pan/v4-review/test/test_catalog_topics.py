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
        self.assertEqual(project_topic('每天做一点就有收益',(),'',{'category':'unsorted'}),('',''))
