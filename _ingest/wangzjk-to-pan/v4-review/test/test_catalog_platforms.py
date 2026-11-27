import unittest

from catalog_platforms import project_platforms


class PlatformProjectionTest(unittest.TestCase):
    def test_explicit_operating_channels_and_aliases(self):
        cases = {
            '抖店淘宝拼多多小红书，店铺取数和运营全套': {'douyin','taobao','pinduoduo','xiaohongshu'},
            '不靠粉丝、不用囤货，抖音商城拉新完整实操': {'douyin'},
            '外卖全平台运营爆单课：美团饿了么京东玩法': {'meituan','eleme','jd'},
            '海外抖音电商和海外拼多多选品运营': {'tiktok','temu'},
            'Shopee虾皮高阶运营和阿里国际站外贸获客': {'shopee','alibaba_intl'},
            '企业微信承接获客咨询，视频号开播': {'wechat','wechat_video'},
            '覆盖淘系、拼多多、抖音、小红书等多平台，店铺运营实战': {'taobao','pinduoduo','douyin','xiaohongshu'},
            '抖音和视频号，春节倒计时玩法，操作方法简单，可实现快速涨粉': {'douyin','wechat_video'},
            '原创视频玩法撸平台收益，适合头条+百家+视频号+抖音，一个作品吃多份收益':
                {'toutiao','baijia','wechat_video','douyin'},
            '百度网盘拉新项目，夸克网盘推广，UC网盘佣金': {'baidu_pan','quark_pan','uc_pan'},
            '抖音最新吞私信解决办法，11月最新方法': {'douyin'},
            '拼多多特训营高阶班，独家玩法赋能，突破运营天花板': {'pinduoduo'},
            '动漫风励志类视频，视频号抖音一稿多发，收入持续': {'wechat_video','douyin'},
            '2026闲鱼数码相机手机租赁项目，从开店上架到售后管理': {'xianyu'},
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(set(project_platforms(title, (), '')), expected)

    def test_delivery_software_source_and_negative_mentions(self):
        cases = {
            '微信视频号下载助手': set(),
            '多平台直播录制工具：适配B站、抖音和小红书直播': set(),
            'TikTok跨境电商运营，百度网盘：链接，提取码见资料': {'tiktok'},
            '某公众号付费文章：关于怎样获客的一点想法': set(),
            '公众号热门赛道：知乎高赞问答搬运——公众号流量主运营': {'wechat_articles'},
            '不做小红书和抖音，只做公众号流量主运营': {'wechat_articles'},
            '亚马逊云科技开发和本地办公工具': set(),
            '无货源店铺运营，1688拿货、数据化选品': set(),
            '超100+平台视频免费下载：YouTube、B站': set(),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(set(project_platforms(title, (), '')), expected)

    def test_uses_intro_without_using_earlier_projects_or_software_compatibility(self):
        self.assertEqual(project_platforms(
            '日收益500+的微信小程序，多种收益来源',
            ('我做过淘宝客、视频号小游戏直播、小红书虚拟、拼多多虚拟等项目。',), ''),
            ('wechat_miniprograms',))
        self.assertEqual(project_platforms('B站录播BiliLive-tools v3.6.0绿色版',
                                          ('支持弹幕转换与视频压制并上传至B站。',), ''), ())
        self.assertEqual(project_platforms('小生意实战手册',
                                          ('用小红书发布种草笔记', '公众号流量主实战'), ''),
                         ('xiaohongshu','wechat_articles'))
        self.assertEqual(project_platforms('别乱做大众点评！最新合规玩法',
                                          ('大众点评玩法3.0，2分钟一条笔记。',), ''), ('dianping',))

    def test_reviewed_roles_win_and_manual_records_are_respected(self):
        review={'platforms':[{'id':'douyin','role':'excluded'}, {'id':'xiaohongshu','role':'acquire'},
                             {'id':'wechat','role':'delivery'}]}
        title='抖音起号，淘宝开店，企业微信承接'
        self.assertEqual(project_platforms(title, (), '', review), ('xiaohongshu','taobao'))
        self.assertEqual(project_platforms(title, (), '', review, manual=True), ('xiaohongshu',))
