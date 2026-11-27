import unittest

from catalog_topics import CATEGORIES, intro_preview, project_topic


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
