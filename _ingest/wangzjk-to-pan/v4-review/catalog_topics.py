"""Browse topics derived from the scope of the projects in the library."""
from functools import lru_cache
import re
from unicodedata import normalize
from catalog_topic_refinement import CONTEXT_TOPICS, REFINED_TOPICS, reference_topic


TOPIC_REVISION = 'project-topics-6'
CATEGORIES = {
    'commerce': ('电商与带货', ('商品经营', '店铺运营', '虚拟商品', '联盟带货', '本地团购')),
    'crossborder': ('跨境电商与外贸', ('跨境店铺', '海外带货', '外贸获客')),
    'local_business': ('门店与本地生活', ('门店经营', '团购与外卖', '探店与点评')),
    'media': ('短视频与直播', ('短视频制作', '直播运营', '影视解说', '录音与配音')),
    'content': ('图文与写作', ('写作与小说', '图文创作', '内容 IP', '短视频', '短剧与漫剧')),
    'screen': ('短剧与动画', ('短剧与漫剧', '动画与特效', '发行与推广')),
    'design': ('设计与创意', ('平面与作图', '动画与短剧', '设计素材', '摄影与空间设计')),
    'assets': ('素材与模板', ('图片与设计素材', '音乐与音效', '视频与模型', '文档与演示模板')),
    'growth': ('流量获客与营销', ('账号起号', '获客引流', '广告投放', '私域运营', '转化营销')),
    'automation': ('AI 工具与智能体', ('AI 工具', '智能体', '自动化工作流')),
    'technical': ('软件开发与建站', ('软件开发', '建站', '网络技术', '设计', '专业制作')),
    'office': ('办公与效率', ('表格与文档', '汇报与演示', '笔记与知识管理')),
    'software': ('软件与数字工具', ('系统与文件', '影音工具', '应用软件', '办公工具', '设计工具')),
    'services': ('副业与接单', ('技能接单', '活动推广', '本地服务', '综合副业')),
    'gaming': ('游戏与运营', ('游戏开发', '游戏推广', '游戏运营', '游戏资料')),
    'business': ('商业经营与管理', ('商业模式', '企业经营', '组织与团队')),
    'life': ('职场成长与生活', ('职场技能', '个人成长', '生活技能', '学习与兴趣')),
    'finance': ('财经与投资', ('财商', '投资研究', '金融方法')),
    'news': ('新闻与行业资讯', ('新闻简报', '行业观察', '经验案例')),
    'reference': ('综合参考', ('项目线索', '经验与阅读', '课程与资料', '操作方法', '站点说明')),
}
# These fields remain valid in old results, but have no reader-facing facet.
NO_TOPIC = 'unsorted'
CATEGORY_ALIASES = {'ai_media': 'media', 'tools': 'software', NO_TOPIC: ''}

_RULES = (
    ('finance','投资研究',10,r'量化交[易割]|EA量化|智投|证券投资|股票投资|股市|期货|基金定投|投资组合|价值投资|证券分析|加密货币|虚拟币交易'),
    ('finance','财商',8,r'财商|个人理财|资产配置|家庭理财|投资入门'),
    ('crossborder','跨境店铺',10,r'跨境电商|跨境.{0,8}(?:店铺|运营|开店)|亚马逊.{0,14}(?:运营|卖家|店铺|选品|跨境|品牌)|阿里国际站|shopee|lazada|速卖通|temu|ebay|沃尔玛.{0,5}店'),
    ('crossborder','海外带货',11,r'(?:tiktok|tk|美区|海外).{0,30}(?:带货|小店|shop|商品|跨境|电商)|(?:带货|小店|商品|电商).{0,18}(?:tiktok|美区|海外)'),
    ('crossborder','外贸获客',10,r'外贸|出海获客|海外(?:社媒|Facebook).{0,12}(?:获客|客户)'),
    ('local_business','门店经营',13,r'实体(?:门店|商家|店)(?:.{0,14}(?:经营|获客|引流|运营|生意|开店|营销))?|餐饮(?:实体店|门店|商家|店铺|开店|经营)|(?:门店|实体店).{0,12}(?:获客|引流|经营|客流|运营|成交)|巨量本地推|同城.{0,12}门店'),
    ('local_business','团购与外卖',13,r'抖音团购|餐饮团购|外卖(?:店铺|商家|平台|运营|爆单|起店)|本地团购|本地生活(?:服务行业|.{0,10}(?:商家|团购|运营|店铺))|团购达人|到店核销'),
    ('local_business','探店与点评',11,r'大众点评|探店(?:达人|创作|视频|账号|项目|变现|运营)'),
    ('gaming','游戏开发',11,r'(?:游戏|小游戏).{0,6}(?:开发|上架|制作教程|制作付费课程)|(?:unity|godot|虚幻引擎).{0,14}(?:开发|游戏)|taptap'),
    ('gaming','游戏推广',10,r'游戏广告|手游推广|星图小游戏|试玩推广|游戏拉新|(?:抖音|微信)小游戏.{0,10}(?:玩法|广告|收益)|游戏试玩'),
    ('gaming','游戏运营',8,r'游戏.{0,9}搬砖|游戏.{0,8}(?:打金|代练|挂机)|游戏工作室|游戏代练|冒险岛|魔兽世界|梦幻西游|地下城与勇士|游戏模组|游戏变速|游戏辅助'),
    ('services','本地服务',9,r'陪伴经济|陪玩|陪聊|语音厅|家政服务|婚介|相亲服务|陪诊|代办服务'),
    ('services','活动推广',9,r'网盘拉新|网盘拉转|app拉新|任务悬赏|地推拉新|看广告项目|刷广告|推广任务|返利网|悬赏平台|拉新项目|试玩任务|快递cps'),
    ('services','技能接单',9,r'(?:接单|派单|代做|代写|代运营|外包|众包)(?:项目|实操|指南|渠道|教程|实战)?|在线兼职|配音兼职|任务平台'),
    ('services','技能接单',13,r'(?:AI|代写|代做).{0,14}(?:代改简历|简历代改|代写PPT|代做PPT|制作微课|教育动画\s*\+\s*微课)|(?:PPT|微课|设计|剪辑|配音|代写|代做).{0,8}(?:接单变现|兼职接单|代做实战)|(?:接单变现|代做).{0,8}(?:PPT|微课|简历)|(?:做|代写)PPT.{0,8}(?:接单|爆单|兼职|商单)'),
    ('commerce','联盟带货',10,r'带货|商品橱窗|分销佣金|带货佣金|小店随心推|巨量千川|商品推广|电商佣金|高佣商品|佣金选品'),
    ('commerce','虚拟商品',10,r'虚拟(?:商品|资料|产品|店铺|资源|类目|电商)|知识付费选品|自动发货|小报童.{0,12}(?:付费|变现)'),
    ('commerce','店铺运营',9,r'拼多多|淘宝|闲鱼|抖店|视频号小店|小红书店铺|开店实|网店|卖货|电商|选品|店群|供应链|商品运营|店铺运营'),
    ('media','直播运营',9,r'无人直播|无人播|半无人.{0,6}直播|直播运营|直播实战|直播间|主播培训|直播切片|直播带播|直播.{0,10}拆解'),
    ('media','影视解说',9,r'影视解说|电影解说|纪录片解说|体育解说|汽车解说|人物志解说|解说号|影视剪辑|视频解说'),
    ('media','短视频制作',8,r'短视频|视频制作|视频创作|视频剪辑|ai电影|ai视频(?:创作|课|制作)|真人出镜|数字人(?:制作|口播|实操)|剪映|premiere|capcut|达芬奇|视频拍摄|摄影剪辑|混剪|手机拍摄|爆款视频|视频搬运|视频素材二创'),
    ('media','录音与配音',8,r'音频制作|配音|字幕制作|录音(?:工具|教程|技巧)|播客制作|音频剪辑|有声书制作'),
    ('screen','发行与推广',12,r'(?:短剧|漫剧).{0,6}(?:出海|投放|推广|拉新|发行|授权分销)|微短剧(?:推广|分销)'),
    ('screen','短剧与漫剧',11,r'短剧|漫剧|转绘漫剧|微短剧|短剧制作|漫剧复刻'),
    ('screen','动画与特效',11,r'动画短片|动画电影|动画制作|动画教程|动画变现|二次元动效|漫画(?:制作|生成|变现)|3d动画|影视特效|视频特效|cg动画'),
    ('design','平面与作图',9,r'ai作图|平面设计|图像设计|工业设计|产品渲染|品牌视觉|photoshop|lightroom|illustrator|indesign|autodesk revit|topaz gigapixel|coreldraw|blender|midjourney|绘本制作|绘画|海报设计|作图|修图|插画|生图|视觉设计|美工'),
    ('design','摄影与空间设计',10,r'摄影|拍照构图|室内设计|建筑设计|空间设计|景观设计|效果图制作'),
    ('office','表格与文档',9,r'办公(?:提效|套件|效率|实操)|办公软件|表格|文档编辑|数据透视|excel|wps|acrobat|pdf专家|pdf编辑|pdf阅读|财务报表|记账软件|文档翻译|microsoft office'),
    ('office','汇报与演示',9,r'做ppt|写ppt|代写ppt|ppt|powerpoint|演示文稿|汇报材料|公文写作'),
    ('office','笔记与知识管理',9,r'知识管理|知识库管理|笔记软件|笔记管理|markdown编辑|typora|obsidian|trilium'),
    ('automation','智能体',9,r'智能体|agent|openclaw|workbuddy|skill(?:工具|体系|技能|标准化|封装)|muse全能|bot搭建|扣子(?:实战|搭建|智能)|多智能体|grok bot'),
    ('automation','自动化工作流',9,r'工作流|流程自动化|自动化工具|自动化(?:脚本|任务|办公)|n8n|dify|coze|扣子|自动化系统'),
    ('automation','AI 工具',8,r'deepseek(?:入门|教程|实战|工具|应用|提示词)|ai工具|提示词工程|ai提示词|ai指令|即梦ai.{0,8}(?:系统课|教程)|大模型(?:部署|应用|入门|教程|训练)|模型部署|模型接入|大模型聚合'),
    ('technical','软件开发',10,r'ai编程|代码开发|全栈开发|python(?:开发|编程|实战|教程)|java(?:编程|开发)|(?:vibe|claude code)\s*coding|claudecode|cursor.{0,10}(?:编程|开发)|软件开发|应用开发|小程序开发|程序设计|前端开发|后端开发|源码解析'),
    ('technical','建站',10,r'独立站.{0,10}(?:搭建|建站)|网站搭建|建站|wordpress|服务器部署|域名配置|站长工具|网页开发'),
    ('technical','网络技术',10,r'网络安全|漏洞挖掘|网络技术|linux(?:运维|命令)|服务器运维|网络工程|系统架构'),
    ('content','写作与小说',11,r'写小说|小说(?:写作|创作|投稿|推文|变现|搬运)|番茄小说|网络文学|故事创作|写作(?:技巧|课程|训练|教程)|网文|写稿|稿件投稿'),
    ('content','图文创作',8,r'图文|爆文|公众号(?:运营|投稿|创作|变现|流量主|贴图)|头条(?:号|文章)|自媒体文章|小红书笔记|笔记创作|笔记制作|图文排版'),
    ('content','内容 IP',7,r'内容ip|自媒体ip|ip孵化|内容创作|人设打造|自媒体创作'),
    ('growth','广告投放',10,r'广告投放|投流|信息流广告|付费流量|全域投放|万相台|无界.{0,12}(?:投放|关键词爆流)|(?:facebook|meta)广告|seo|geo流量|引擎优化.{0,4}geo'),
    ('growth','私域运营',9,r'私域|社群运营|企业微信|复购运营|用户运营'),
    ('growth','获客引流',8,r'精准获客|获客|精准引流|引流|商业营销|增长营销|社媒营销|品牌营销|销售转化|营销策划'),
    ('growth','账号起号',8,r'起号|小红书运营|账号运营|账号定位|新手涨粉|账号增长|养号|涨粉|流量主|账号矩阵|ip起盘|小程序.{0,8}日活'),
    ('growth','账号起号',12,r'(?:搭建|起盘|打造|重做).{0,18}(?:小红书|抖音|视频号)账号|(?:小红书|抖音|视频号)账号.{0,12}(?:从0到1|定位|运营|增长)'),
    ('growth','获客引流',12,r'全球化ip.{0,8}(?:进阶|变现|获客)|(?:内容|全自动|线上|精准).{0,5}获客|商业ip流量(?:破局|实战)'),
    ('business','商业模式',11,r'新个体创业(?:课|必修课)|从零搭建低成本小生意'),
    ('business','商业模式',9,r'商业模式|商业思维|创业(?:课|课程|训练营|经营|研修)|新个体|创业认知|商业认知|商业闭环|一人公司|从0到1做小生意'),
    ('business','企业经营',9,r'企业增长|企业经营|ai企业服务|fde行业|老板ip|经营管理|商业战略|连锁经营|创业管理'),
    ('business','组织与团队',9,r'组织管理|团队管理|员工绩效|股权激励|培训管理|领导力'),
    ('life','个人成长',10,r'负面情绪|精神内耗|个人成长|自我成长|成长研修|自我蜕变|情绪沟通|情绪调节|心力成长|心理疏导|亲密关系|内耗|个人提升|人际关系|识人的.{0,8}(?:方法|心法)|自我突破|人生忠告|洞察力提升'),
    ('life','职场技能',9,r'面试|求职|职业规划|职场(?:成长|技能|沟通|转型|晋升|潜规则|博弈)|职场.{0,18}领导|晋升案例|简历|公开演讲|工作沟通'),
    ('life','生活技能',8,r'烹饪|健身训练|家庭收纳|育儿(?:方法|指南|亲子)|亲子沟通|婚礼流程|宠物饲养|酒桌.{0,14}话术'),
    ('life','学习与兴趣',8,r'英语(?:词汇|口语|发音|学习)|中小学.{0,8}(?:学习|复习)|(?:考研|考证).{0,8}(?:备考|复习规划)|丙午年.{0,12}(?:趋势课|年运)|(?:风水|传统文化).{0,10}(?:课程|本质|讲座)'),
    ('software','系统与文件',9,r'文件管理|文件搜索|文件快速复制|文件批量重命名|重复文件|文件时间戳|解压缩|系统驱动|(?:nvidia|显卡|图形)驱动|crystaldiskmark|系统优化|windows常态化|远程桌面|系统清理|虚拟机|系统安装|启动盘|备份工具|分区工具|应用卸载|浏览器数据迁徙|q-dir|xyplorer|ccleaner|peazip|duplicates?\s+same\s+files'),
    ('software','影音工具',10,r'mkvtoolnix|hypersnap|gamescrcpy|视频封装|格式转换|音乐播放器|游戏投屏|投屏工具|mp3tag|video converter|视频下载器|字幕提取工具|音乐下载器'),
    ('software','应用软件',7,r'绿色版|便携版|绿色纯净|绿色精简|便携汉化|中文专业版|绿色便携|纯净版|精简版|google chrome|microsoft edge|ab download manager|下载管理器|电脑工具|软件合集|v?\d{1,4}\.\d+(?:\.\d+){0,3}.{0,5}(?:特别版|正式版|汉化版|高级版|专业版|单文件版)'),
)
_PURPOSE_RULES = (
    ('commerce','店铺运营',13,r'(?:开好|开个|开一家|从零开)\s*(?:淘宝|拼多多|抖音)店|(?:淘宝|淘天|拼多多).{0,8}(?:开店|店铺运营|起店|特训营)|抖店小店|无限活动流.{0,4}(?:迭代|课程)|电商开店工作台'),
    ('commerce','虚拟商品',13,r'(?:淘宝|拼多多|闲鱼|小红书|多多).{0,8}虚拟(?:项目|资料|产品|电商|类目)|(?:卖|售卖|销售).{0,5}(?:虚拟资料|考研资料|性格测试报告)|(?:性格测试|mbti|考研)资料.{0,8}(?:虚拟|售卖|成交)'),
    ('media','短视频制作',13,r'(?:短视频|ai视频)(?:自动化|制作|拍摄|剪辑|生产|创作)(?:实战|课程|教程|sop|全流程|课)?|广告剧情片|数字人(?:实操课|制作教学|口播制作)'),
    ('media','短视频制作',13,r'(?:百家号|视频号|头条)视频.{0,5}(?:搬砖|分成|广告收益|发布变现)'),
    ('media','影视解说',13,r'(?:汽车|影视|电影|纪录片|人物志|动漫|体育)解说'),
    ('media','录音与配音',11,r'配音教程|有声书配音|播客制作|古风.{0,8}(?:mv|翻唱)|音乐(?:创作|制作)'),
    ('automation','智能体',14,r'(?:workbuddy|openclaw|扣子|coze|dify|智能体)\s*(?:ai\s*)?(?:系统课|系统应用|(?:智能体)?高阶玩法|技能(?:创建|搭建)|自动化实战课)|飞书自动化实战课|^自动化实战课|ai技能.{0,6}skill'),
    ('automation','AI 工具',12,r'多模态\s*ai\s*工具站|ai提示词(?:高阶|入门|应用)|ai指令(?:训练|实战|教程)|ai从入门到精通|职场ai进阶课程|提示词.{0,5}教你'),
    ('technical','建站',13,r'ai中转站|(?:服务器|转发站|中转站).{0,12}(?:搭建|配置|部署)'),
    ('content','图文创作',12,r'公众号贴图|公众号流量主.{0,8}(?:实战|运营|起号)|(?:小红书|公众号).{0,8}图文创作'),
    ('growth','账号起号',12,r'(?:涨粉|账号增长).{0,8}(?:实战|训练营|技巧|玩法|方法)|(?:小红书|抖音|视频号)账号|(?:小红书|抖音|视频号).{0,8}吞私信|抖音.{0,8}流量.{0,8}运营'),
    ('business','商业模式',13,r'(?:ai)?一人公司.{0,10}(?:商业|全链路|实战|创业|踩坑|变现)|(?:商业认知|副业)复盘.{0,5}(?:项目|方法)'),
    ('business','企业经营',13,r'fde行业.{0,8}(?:系统|趋势峰会)|ai企业服务.{0,8}(?:变现|赛道|实战)'),
    ('life','职场技能',12,r'职场.{0,10}(?:晋升|招人|实习|领导|博弈)|(?:实习|工作).{0,12}职场潜规则|升职.{0,4}(?:方法|指南)|晋升.{0,4}(?:方法|指南)'),
    ('life','个人成长',11,r'(?:识人|辨人|可靠).{0,10}(?:方法|心法)|如何判断一个人靠谱|高情商沟通'),
    ('crossborder','海外带货',12,r'(?:跨境|亚马逊|temu|shopee|tiktok).{0,8}(?:通关训练营|跨境卖家|出单运营)|跨境出海'),
    ('services','活动推广',12,r'(?:豆包|百度|夸克|uc|迅雷|网盘|快手极速版|剪映模板).{0,4}拉(?:新|转|存)|(?:浏览|看|点击)广告.{0,10}(?:任务|挂机|项目|收益)|广告挂机|悬赏(?:评论|任务|接单)'),
    ('services','本地服务',12,r'(?:同城|单身).{0,8}(?:配对|相亲|婚介|经济项目)|陪伴经济|电影票.{0,8}(?:代购|代订|项目|玩法)'),
)
SIGNALS = tuple((category, sub, weight, re.compile(expression, re.I))
                for category, sub, weight, expression in (
                    *CONTEXT_TOPICS, *_PURPOSE_RULES, *_RULES,
                    *((category, sub, 10, expression) for category, sub, expression in REFINED_TOPICS),
                ))
_TITLE_NOISE = re.compile(r'(?:无需|不用|不需要|不必|免去)(?:再)?(?:直播|剪辑|拍摄|拍视频|开店|囤货|投流|露脸|出镜|懂技术|写代码|设计|接单)')
_TAGS = re.compile(r'^(?:课程内容|课程|项目|内容|软件|公开)?(?:介绍|简介)(?:[:：\s]|$)')
_EDITION_PREFIX = re.compile(r'^\s*(?:(?:\(\d{4,7}期\)|【(?:无水印|原创双语字幕|独家)】)\s*)+')
_ENTRY_FORMAT = re.compile(r'^(?:(?:某|微信)?公众号)?(?:付费文章|课程介绍|项目简介)\s*[:：]\s*')
_INVISIBLE_FORMAT = re.compile(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]')
_LEAD_END = re.compile(r'[:|;。!?,，；：—]|\s-\s')
_TITLE_EXTRAS = re.compile(r'(?:附赠|附送|另赠|内附|赠送|附带|配套赠送|案例包括|案例包含|案例涉及|适用场景包括).*$')
_BODY_ASIDE = re.compile(r'^(?:附赠|赠品|额外赠送|随课赠送|免责声明|(?:适用|应用)场景举例|素材包附赠)')
_ANCILLARY_SECTION = re.compile(r'^(?:适合人群|适用人群|推荐人群|目标受众|附赠内容|赠品清单|学员案例|成功案例|效果展示)\s*[:：]?$')
_PRIMARY_SECTION = re.compile(r'^(?:课程|项目|软件|功能|工具|使用|学习|核心|内容|详细|操作)(?:目标|说明|特点|介绍|简介|大纲|目录|安排|亮点|内容|步骤|演示|教程|模块|实操|方式|方法)(?:\s*[:：]|$)')
_GENERIC_HEAD = re.compile(r'^(?:(?:\d{1,2}月)?(?:全网|全新|最新|独家|首发|揭秘|牛逼|爆款|必看|系列|进阶|干货|深度|保姆级|小白|项目|实战|课程|实操|入门|实用|必收)[!：: ]*)+$')
_TRAINING = re.compile(r'大课|课程|系统课|必修课|线下课|操盘课|教程|教学|特训营|训练营|实训(?:课|营)?|\d{1,3}\s*(?:讲|节课)|手把手|教你|从零.{0,7}(?:学|入门)|新手.{0,4}(?:上手|做)|实战(?:课|方法|经验|进阶)|(?:直播|操作).{0,10}拆解|玩法|实操')
_NEWS_BRIEF = re.compile(r'新闻(?:早讯|早报|晚报|简报|快讯)|每日(?:早报|晚报|快讯)|60\s*(?:秒|s)\s*读懂世界|(?:国际|热点)新闻简报',re.I)
_NEWS_REPORT = re.compile(r'行业(?:观察|快讯|简报|动态)|市场观察|政策要闻|行业报告发布|政策正式(?:实施|出台)',re.I)
_REPORTING = re.compile(r'最新发布|报告发布|数据(?:显示|揭示)|统计公报|正式发布|(?:根据|据).{0,18}(?:报告|通报|统计)|发布会|(?:政策|法规).{0,8}(?:出台|实施)')
_NEWS_AS_MATERIAL = re.compile(r'模板|脚本|工作流|智能体|自动化|起号|(?:制作|生成|采集|聚合)(?:工具|教程)?')
_SOFTWARE_INTRO = re.compile(r'^(?:软件|应用|工具)(?:介绍|简介)(?:\b|\s|[:：])', re.I)
_AI_PRODUCT = re.compile(r'(?:多模态)?ai\s*(?:工具站|生成平台|模型聚合平台)(?:开放|上线|使用|体验)?',re.I)
_TOOL_COURSE = re.compile(r'(?:即梦|可灵|midjourney|通义万相|海螺|ai.{0,8}(?:工具|平台|软件)).{0,8}(?:全能创作课|全功能|系统课|入门到精通)', re.I)
_ALL_FEATURES = re.compile(r'覆盖(?:软件|工具|平台)?全(?:部)?功能|从基础功能介绍|从基本功能到高阶(?:功能|操作)')
_SOFTWARE_RELEASE = re.compile(r'(?<![A-Za-z0-9])v\s*\d{1,4}(?:\.\d+){1,4}|绿色(?:版|便携|纯净|精简)|便携(?:版|汉化)|直装(?:版|/)|安装即用|(?:电脑|桌面|安卓|tv)版.{0,10}(?:客户端|体验丝滑)|客户端(?:增强|补丁|下载)', re.I)
_SOFTWARE_PRODUCT = re.compile(r'(?:视频|文件|素材|电子教材|字幕|音乐|磁力).{0,6}下载(?:器|工具|助手)|(?:下载|翻译|截图|投屏|录播|卸载|录屏)(?:器|小?工具|助手)|内容监控.{0,22}(?:开源|软件)|(?:多线程|磁力)下载工具',re.I)
_SOFTWARE_KIND = tuple((sub, re.compile(expr, re.I)) for sub, expr in (
    ('影音工具',r'录播|追剧|视频.{0,6}(?:下载|播放|压缩|修复|封装|转换|工具|翻译)|下载.{0,12}视频软件|影音|视频号下载|字幕(?:提取|转换|机翻)|音频|混音|录屏|播放器|格式工厂|audition|fxsound|camtasia|hypersnap|mkvtoolnix|gamescrcpy'),
    ('办公工具',r'pdf|excel|ppt|office|wps|word|文档|表格|笔记管理|知识库'),
    ('设计工具',r'photoshop|lightroom|illustrator|indesign|blender|topaz|设计软件|修图工具|照片编辑|相片大师|raw处理|adobe bridge|after effects'),
    ('系统与文件',r'系统(?:清理|优化|重装)|文件(?:处理|复制|管理|转存|批量|同步|恢复)|文件夹|对话.{0,5}导出到本地|网盘.{0,8}转存|驱动|存储管理|硬盘|解压缩|ftp|ssh|备份|虚拟机|网速聚合|download manager'),
))
_ASSET_SIGNAL = re.compile(r'音效(?:合集|音乐包|素材)|背景音乐|配乐素材|音乐素材|(?:bgm|音效|配乐|音乐|转场|片头|字幕|模型|贴图|插画|字体|参考图|素材|预设|样式|模板).{0,9}(?:素材包|素材合集|资源包|资源合集|完整版|合集|打包)|(?:海报|ppt|ps|封面|画册|剪映|视频|分镜|汇报|运营表格)模板|摄影预设|\d[\d,]*\s*(?:套|张).{0,8}(?:参[考照]图|姿势图|图片)|(?:免版权|可商用).{0,10}(?:bgm|音效|音乐|图片|图标|素材)|(?:三维|3d|cg|设计)模型素材', re.I)
_ASSET_ATTACHMENT = re.compile(r'附(?:赠|送|带|全套|万能|素材|教程|模板)|配套.{0,8}(?:素材|模板)|包含|内附|素材下载器')
_ASSET_AS_METHOD = re.compile(r'^\s*(?:拉新|拉转|分销|代理)(?:项目|推广|任务)')
_ASSET_KIND = tuple((sub, re.compile(expr, re.I)) for sub, expr in (
    ('音乐与音效',r'音乐|音效|配乐|bgm|语音包'),
    ('文档与演示模板',r'ppt|演示|汇报|表格|文档|excel|word'),
    ('视频与模型',r'转场|视频|片头|剪映模板|影视剪辑素材|模板工程|模型|建模|字幕'),
))
_SIDE_JOB = re.compile(r'(?:副业(?:项目|复盘|路线|实战|教程)|信息差(?:项目|实操|机会)|精品项目群|副业项目群|任务(?:平台|项目)|副业.{0,12}(?:落地|实收|第一笔收入))')
_EXPORT_GOAL = re.compile(r'(?:聚焦|面向|专为|专题讲解).{0,10}(?:跨境出海|跨境电商|外贸独立站)')
_EXPORT_SCOPE = re.compile(r'独立站|出海|跨境|外贸|海外社媒')
_PRODUCT_TEACHING = re.compile(r'如何使用|使用方法|安装|操作教程|实操演示|使用教程|配置教程|使用指南')
_REPO_PRODUCT = re.compile(r'github.{0,15}(?:开源|工具|神器)|开源.{0,5}(?:工具|软件|客户端|桌面应用)|(?:开源|自托管).{0,14}(?:工作台|自动化项目)', re.I)
_PRODUCT_OBJECT = re.compile(r'工具|神器|软件|客户端|桌面|自托管|浏览器(?:插件|扩展)|\bv(?:er)?\s*\d+\.\d+', re.I)
_SOFTWARE_DELIVERY = re.compile(r'(?:windows|macos|mac|安卓|桌面|电脑)(?:版|客户端)|安装包|软件下载|(?:小说|写作).{0,5}(?:创作软件|辅助软件)|免安装版|一键win(?:dows)?系统重装|download manager', re.I)
_DESKTOP_PRODUCT = re.compile(r'^(?:windows|macos|电脑端|桌面端).{0,14}(?:生成器|客户端|小工具|管理软件)', re.I)
_OFFERED_SOFTWARE = re.compile(r'(?:本(?:项目|文|课程|资源)|文章).{0,30}(?:提供|附有|分享).{0,20}(?:下载软件|客户端|安装包|浏览器扩展)|(?:纯本地运行|无需联网)[\s\S]{0,95}(?:安装|扩展程序|浏览器插件)', re.I)
_SOFTWARE_DEMO = re.compile(r'(?:软件|工具|部分功能|全部功能).{0,12}(?:视频展示|视频演示)|\d{1,3}\s*个功能.{0,28}(?:持续更新|维护更新)')
_SOFTWARE_AND_TUTORIAL = re.compile(r'(?:软件|客户端|安装包)\s*(?:\+|及|与)\s*(?:安装|使用)?教程')
_PROJECT_LIST = re.compile(r'^(?:\d{1,2}\s*个.{0,8}(?:小项目|副业项目|冷门项目)|(?:国内|国外|海外)?(?:网赚|副业|冷门|小众)项目(?:合集|清单))')
_PROJECT_LIST_BODY = re.compile(r'给大家把一些必赚的小项目整理了一个\s*清单|小项目大家可以自己实操变现|以下小项目均经过本人实操|(?:覆盖|整理|收录|汇总).{0,5}\d{1,3}(?:余|多)?个.{0,20}(?:赛道|项目)')
_PROJECT_COLLECTION = re.compile(r'^(?:(?:某收费|小众|轻量)\s*)?精品项目群.{0,38}(?:持续分享|定期更新|每周更新|持续更新|专门分享.{0,8}项目)')
_PUBLISHING_TOOL = re.compile(r'(?:视频|图文|内容).{0,10}(?:搬运工具|发布工具|跨平台分发工具)|(?:全流程|一键).{0,15}(?:搬到|分发到|发布到)')
_TEACHING_DELIVERABLE = re.compile(r'本(?:套|期)?课(?:程)?(?:主要|重点|核心|围绕|聚焦|最终|是|将|会|教|以|从)|课程目标[:：]?|本项目(?:是|核心|旨在|专门|教)|(?:主要|重点)(?:教|学习|讲解|交付|训练)|是一套(?:专注|聚焦|围绕|针对)|最终(?:所有)?教学(?:都)?指向')
_GOAL_EXAMPLES = re.compile(r'[,，](?:同时|课程案例|案例包括|涵盖的案例|例如|并附|顺带|配套|搭配|附带).*$')
_GAME_PROFILE = re.compile(r'^游戏介绍[:：\s][\s\S]{5,900}(?:游戏截图|游戏视频|游戏特色|版本介绍|游戏玩法)')


def title_text(title):
    text = _INVISIBLE_FORMAT.sub('',normalize('NFKC', title))
    return _ENTRY_FORMAT.sub('', _EDITION_PREFIX.sub('', text)).strip()


def introduction_text(title, paragraphs, excerpt):
    seen = set()
    prefixes = (normalize('NFKC', title), title_text(title))
    for item in (*paragraphs, excerpt):
        text = ' '.join(normalize('NFKC', item or '').split())
        for prefix in prefixes:
            if prefix and text.startswith(prefix):
                text = text[len(prefix):].lstrip(' .、,:-')
        text = re.sub(r'\s*\[\s*(?:…|\.\.\.)\s*\]$', '', text).strip()
        if not text or text in seen or text.startswith(('提示本文','免责声明','本内容仅','资料下载','课程下载')):
            continue
        if any(prefix.startswith(text) for prefix in prefixes):
            continue
        seen.add(text)
        yield text


def purpose_text(parts):
    """Ignore add-ons and boilerplate while keeping the whole readable introduction."""
    kept = []
    ancillary = False
    for part in parts:
        heading = re.sub(r'^[^\w]+', '', part)
        if _ANCILLARY_SECTION.fullmatch(heading):
            ancillary = True
            continue
        if _PRIMARY_SECTION.match(heading):
            ancillary = False
        if not ancillary and not _BODY_ASIDE.match(heading):
            kept.append(_TITLE_NOISE.sub('', _TITLE_EXTRAS.sub('', part)))
    return '\n'.join(kept)


def topic_scores(title, head, intro, goal_text):
    """Score independent fields, with capped support from repeated body terms."""
    scores = {}
    for category, sub, weight, pattern in SIGNALS:
        title_match = pattern.search(title)
        body_match = pattern.search(intro) if intro else None
        goal_match = goal_text and pattern.search(goal_text)
        if not (title_match or body_match or goal_match):
            continue
        if title_match:
            score = weight + (10 if title_match.end() <= len(head) else 5)
        elif body_match:
            score = weight + (0 if body_match.start() < 320 else -4)
        else:
            score = 0
        if goal_match and weight >= 10:
            score = max(score, weight + 7)
        item = scores.setdefault(category, {})
        previous = item.get(sub, (0, False, False))
        item[sub] = (max(previous[0], score), previous[1] or bool(title_match),
                     previous[2] or bool(body_match or goal_match))
    # Corroboration changes close calls but a long list of examples cannot
    # outrank a clearer title by sheer frequency.
    return {category: max(((base + (3 if matched_title and body else 0), sub, matched_title, body)
                          for sub, (base, matched_title, body) in subtopics.items()), key=lambda item: item[0])
            for category, subtopics in scores.items()}


def direct_topic(text, intro, head, teaching):
    if not teaching and _NEWS_BRIEF.search(text) and not _NEWS_AS_MATERIAL.search(text):
        return 'news','新闻简报'
    if not teaching and _NEWS_REPORT.search(head) and _REPORTING.search(intro) and not _NEWS_AS_MATERIAL.search(text):
        return 'news','行业观察'
    if not teaching and _GAME_PROFILE.match(intro):
        return 'gaming','游戏资料'
    if _PROJECT_COLLECTION.search(text) or (_PROJECT_LIST.search(text) and _PROJECT_LIST_BODY.search(intro)):
        return 'reference','项目线索'
    if _AI_PRODUCT.search(head) or (_TOOL_COURSE.search(text) and _ALL_FEATURES.search(intro)):
        return 'automation','AI 工具'
    release = _SOFTWARE_RELEASE.search(text)
    utility = _SOFTWARE_PRODUCT.search(head) or _PUBLISHING_TOOL.search(text)
    repository = _REPO_PRODUCT.search(text) and _PRODUCT_OBJECT.search(text)
    software_intro = _SOFTWARE_INTRO.search(intro)
    offered = _OFFERED_SOFTWARE.search(intro)
    desktop_product = _DESKTOP_PRODUCT.search(head)
    companion_tutorial = _SOFTWARE_AND_TUTORIAL.search(text) or (_PRODUCT_OBJECT.search(text) and _PRODUCT_TEACHING.search(text))
    demo = (_PRODUCT_OBJECT.search(head) and _SOFTWARE_DEMO.search(intro)
            and not (_TRAINING.search(head) or _PRODUCT_TEACHING.search(head)))
    if ((not teaching and (release or software_intro or utility or repository or desktop_product or _SOFTWARE_DELIVERY.search(head)))
            or (release and (software_intro or utility or companion_tutorial))
            or demo or (offered and (companion_tutorial or not teaching))):
        software = text + ' ' + intro[:130]
        sub = next((name for name, pattern in _SOFTWARE_KIND if pattern.search(head)), '')
        if _PUBLISHING_TOOL.search(text):
            sub = '影音工具'
        if not sub and not desktop_product:
            scope = text if repository else software
            sub = next((name for name, pattern in _SOFTWARE_KIND if pattern.search(scope)), '')
        return 'software', sub or '应用软件'
    asset = _ASSET_SIGNAL.search(text)
    if (not teaching and asset and not _ASSET_ATTACHMENT.search(text[:asset.end()])
            and not _ASSET_AS_METHOD.match(text[asset.end():])):
        sub = next((name for name, pattern in _ASSET_KIND if pattern.search(text)), '图片与设计素材')
        return 'assets', sub
    if teaching and _EXPORT_SCOPE.search(head) and _EXPORT_GOAL.search(intro[:180]):
        return 'crossborder','外贸获客'
    return '', ''


def intro_preview(title, paragraphs, excerpt, limit=410):
    parts=[]
    for paragraph in (*paragraphs, excerpt):
        text=' '.join((paragraph or '').split()).strip()
        if text.startswith(title):
            text=text[len(title):].lstrip(' ·、，：:-')
        text=_TAGS.sub('',text).strip()
        shortened=re.sub(r'\s*\[\s*(?:…|\.\.\.)\s*\]$', '', text).strip()
        if len(shortened)<14 or title.startswith(shortened) or text in parts or text.startswith(('提示本文','免责声明','本内容仅','资料下载','课程下载')):
            continue
        if parts and len(' '.join(parts))>=145:
            break
        parts.append(text)
    return ' '.join(parts)[:limit]


def project_topic(title, paragraphs, excerpt, reviewed=None, *, manual=False, source_checked=False):
    reviewed=reviewed or {}
    previous=reviewed.get('category','')
    previous_sub=reviewed.get('subcategory','')
    if manual:
        return (previous, previous_sub) if previous in CATEGORIES else ('', '')
    return automatic_topic(title, tuple(paragraphs), excerpt, previous, previous_sub, source_checked)


@lru_cache(maxsize=20_000)
def automatic_topic(title, paragraphs, excerpt, previous, previous_sub, source_checked):
    title_scope = title_text(title)
    text = _TITLE_NOISE.sub('', _TITLE_EXTRAS.sub('', title_scope)).strip()
    boundary = _LEAD_END.search(text)
    head = text[:boundary.start()] if boundary else text
    if len(head.strip()) < 5 or _GENERIC_HEAD.fullmatch(head.strip()):
        head = text
    teaching = bool(_TRAINING.search(text))
    parts = tuple(introduction_text(title, paragraphs, excerpt))
    intro = purpose_text(parts)
    reference = reference_topic(_INVISIBLE_FORMAT.sub('', normalize('NFKC', title)), intro) if source_checked else ('','')
    if reference == ('reference', '站点说明'):
        return reference
    goals = '\n'.join(_GOAL_EXAMPLES.sub('', part) for part in re.split('[。！？\n]', intro)
                      if _TEACHING_DELIVERABLE.search(part[:52]))
    explicit = direct_topic(text, intro, head, teaching)
    if explicit[0]:
        return explicit

    scores = topic_scores(text, head, intro, goals)
    category, (weight, sub, in_title, _) = max(
        scores.items(), key=lambda item: (item[1][0], item[1][2], item[1][3]),
        default=('', (0, '', False, False)),
    )
    # Keep complete prior decisions when the new evidence is weak.
    retained = (previous, previous_sub)
    if retained == ('content', '短视频'):
        retained = ('media', '短视频制作')
    elif retained == ('content', '短剧与漫剧'):
        retained = ('screen', '短剧与漫剧')
    if (retained[0] in CATEGORIES and retained[0] not in {'news', 'reference'}
            and retained[1] in CATEGORIES[retained[0]][1]):
        if not sub or (category != retained[0] and weight < (23 if in_title else 19)):
            return retained
    if weight >= (12 if in_title else 7) and sub != '综合副业':
        return category, sub
    if reference[0]:
        return reference
    if previous == 'reference' and previous_sub in CATEGORIES['reference'][1]:
        return previous, previous_sub
    if _SIDE_JOB.search(text) or _SIDE_JOB.search(intro[:100]):
        return 'services','综合副业'
    return '', ''


def displayed_secondary_topic(category, reviewed, *, manual=False):
    saved = reviewed.get('secondary_category')
    if not manual and reviewed.get('category', category) != category:
        return ''
    if saved in CATEGORIES and saved != category and (saved != 'news' or manual):
        return saved
    return ''
