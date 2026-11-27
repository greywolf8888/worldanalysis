"""Browse topics derived from the scope of the projects in the library."""
import re
from unicodedata import normalize


TOPIC_REVISION = 'project-topics-2.1'
CATEGORIES = {
    'commerce': ('电商与带货', ('商品经营', '店铺运营', '虚拟商品', '联盟带货', '本地团购')),
    'crossborder': ('跨境电商与外贸', ('跨境店铺', '海外带货', '外贸获客')),
    'media': ('短视频与直播', ('短视频制作', '直播运营', '影视解说', '录音与配音')),
    'content': ('图文与写作', ('写作与小说', '图文创作', '内容 IP', '短视频', '短剧与漫剧')),
    'design': ('设计与创意', ('平面与作图', '动画与短剧', '设计素材')),
    'growth': ('流量获客与营销', ('账号起号', '获客引流', '广告投放', '私域运营', '转化营销')),
    'automation': ('AI 工具与智能体', ('AI 工具', '智能体', '自动化工作流')),
    'technical': ('软件开发与建站', ('软件开发', '建站', '网络技术', '设计', '专业制作')),
    'office': ('办公与效率', ('表格与文档', '汇报与演示', '笔记与知识管理')),
    'software': ('软件与数字工具', ('系统与文件', '影音工具', '应用软件')),
    'services': ('副业与接单', ('技能接单', '活动推广', '本地服务')),
    'gaming': ('游戏与运营', ('游戏开发', '游戏推广', '游戏运营')),
    'business': ('商业经营与管理', ('商业模式', '企业经营', '组织与团队')),
    'life': ('职场成长与生活', ('职场技能', '个人成长', '生活技能')),
    'finance': ('财经与投资', ('财商', '投资研究', '金融方法')),
    'news': ('资讯与案例', ('新闻简报', '行业观察', '经验案例')),
}
# These fields remain valid in old results, but have no reader-facing facet.
NO_TOPIC = 'unsorted'
CATEGORY_ALIASES = {'ai_media': 'media', 'tools': 'software', NO_TOPIC: ''}

_RULES = (
    ('news','新闻简报',11,r'新闻早讯|新闻早报|每日早报|每日快讯|60秒读懂世界|热点新闻|国际新闻简报'),
    ('finance','投资研究',10,r'量化交[易割]|EA量化|智投|证券投资|股票投资|股市|期货|基金定投|投资组合|价值投资|证券分析|加密货币|虚拟币交易'),
    ('finance','财商',8,r'财商|个人理财|资产配置|家庭理财|投资入门'),
    ('crossborder','跨境店铺',10,r'跨境电商|跨境.{0,8}(?:店铺|运营|开店)|亚马逊.{0,14}(?:运营|卖家|店铺|选品|跨境|品牌)|阿里国际站|shopee|lazada|速卖通|temu|ebay|沃尔玛.{0,5}店'),
    ('crossborder','海外带货',11,r'(?:tiktok|tk|美区|海外).{0,30}(?:带货|小店|shop|商品|跨境|电商)|(?:带货|小店|商品|电商).{0,18}(?:tiktok|美区|海外)'),
    ('crossborder','外贸获客',10,r'外贸|出海获客|海外(?:社媒|Facebook).{0,12}(?:获客|客户)'),
    ('commerce','本地团购',10,r'抖音团购|本地生活.{0,12}(?:团购|商家|运营|店铺)|团购达人|大众点评.{0,12}(?:团购|探店)|餐饮团购|到店核销'),
    ('gaming','游戏开发',11,r'(?:游戏|小游戏).{0,6}(?:开发|上架|制作教程)|(?:unity|godot|虚幻引擎).{0,14}(?:开发|游戏)|taptap'),
    ('gaming','游戏推广',10,r'游戏广告|手游推广|星图小游戏|试玩推广|游戏拉新'),
    ('gaming','游戏运营',8,r'游戏.{0,9}搬砖|游戏工作室|游戏代练|冒险岛|魔兽世界|梦幻西游|地下城与勇士|游戏模组|游戏变速|游戏辅助'),
    ('services','本地服务',9,r'陪伴经济|陪玩|陪聊|语音厅|家政服务|婚介|相亲服务|陪诊|代办服务'),
    ('services','活动推广',9,r'网盘拉新|网盘拉转|app拉新|任务悬赏|地推拉新|看广告项目|刷广告|推广任务|返利网|悬赏平台|拉新项目|试玩任务|快递cps'),
    ('services','技能接单',9,r'(?:接单|派单|代做|代写|代运营|外包|众包)(?:项目|实操|指南|渠道|教程|实战)?|在线兼职|配音兼职|任务平台'),
    ('commerce','联盟带货',10,r'带货|商品橱窗|分销佣金|带货佣金|小店随心推|巨量千川|商品推广|电商佣金|高佣商品|佣金选品'),
    ('commerce','虚拟商品',10,r'虚拟(?:商品|资料|产品|店铺|资源|类目|电商)|知识付费选品|自动发货|小报童.{0,12}(?:付费|变现)'),
    ('commerce','店铺运营',9,r'拼多多|淘宝|闲鱼|抖店|视频号小店|小红书店铺|开店实|网店|卖货|电商|选品|店群|供应链|商品运营|店铺运营'),
    ('media','直播运营',9,r'无人直播|无人播|半无人.{0,6}直播|直播运营|直播实战|直播间|主播培训|直播切片|直播带播'),
    ('media','影视解说',9,r'影视解说|电影解说|纪录片解说|体育解说|汽车解说|解说号|影视剪辑|视频解说'),
    ('media','短视频制作',8,r'短视频|视频制作|视频创作|视频剪辑|ai电影|数字人制作|剪映|premiere|capcut|达芬奇|视频拍摄|摄影剪辑|混剪|手机拍摄|爆款视频|视频搬运|视频素材二创'),
    ('media','录音与配音',8,r'音频制作|配音|字幕制作|录音(?:工具|教程|技巧)|播客制作|音频剪辑|有声书制作'),
    ('design','动画与短剧',10,r'短剧|漫剧|动画短片|动画制作|动画教程|动画变现|二次元动效|转绘漫剧|漫画制作|3d动画|影视特效|视频特效|cg动画'),
    ('design','设计素材',9,r'设计素材|海报模板|封面模板|ps模板|视觉素材|字体合集|免版权.{0,8}(?:bgm|音乐|素材)|配乐素材|音乐素材|画册模板'),
    ('design','平面与作图',9,r'ai作图|平面设计|图像设计|photoshop|lightroom|illustrator|indesign|autodesk revit|topaz gigapixel|coreldraw|blender|midjourney|绘本制作|绘画|海报设计|作图|修图|插画|生图|摄影|视觉设计|美工'),
    ('office','表格与文档',9,r'办公(?:提效|套件|效率|实操)|办公软件|表格|文档编辑|数据透视|excel|wps|acrobat|pdf专家|pdf编辑|pdf阅读|财务报表|记账软件|文档翻译|microsoft office'),
    ('office','汇报与演示',9,r'做ppt|写ppt|代写ppt|ppt|powerpoint|演示文稿|汇报材料|公文写作'),
    ('office','笔记与知识管理',9,r'知识管理|知识库管理|笔记软件|笔记管理|markdown编辑|typora|obsidian|飞书多维表格|trilium'),
    ('automation','智能体',9,r'智能体|agent|openclaw|workbuddy|muse全能|bot搭建|扣子(?:实战|搭建|智能)|多智能体|grok bot'),
    ('automation','自动化工作流',9,r'工作流|流程自动化|自动化工具|自动化(?:脚本|任务|办公)|n8n|dify|coze|扣子|自动化系统'),
    ('automation','AI 工具',8,r'deepseek(?:入门|教程|实战|工具|应用|提示词)|ai工具|提示词工程|大模型(?:部署|应用|入门|教程|训练)|模型部署|模型接入|大模型聚合'),
    ('technical','软件开发',10,r'ai编程|代码开发|全栈开发|python(?:开发|编程|实战|教程)|java(?:编程|开发)|(?:vibe|claude code)\s*coding|claudecode|cursor.{0,10}(?:编程|开发)|软件开发|应用开发|小程序开发|程序设计|前端开发|后端开发|源码解析'),
    ('technical','建站',10,r'独立站.{0,10}(?:搭建|建站)|网站搭建|建站|wordpress|服务器部署|域名配置|站长工具|网页开发'),
    ('technical','网络技术',10,r'网络安全|漏洞挖掘|网络技术|linux(?:运维|命令)|服务器运维|网络工程|系统架构'),
    ('content','写作与小说',11,r'写小说|小说(?:写作|创作|投稿|推文|变现|搬运)|番茄小说|网络文学|故事创作|写作(?:技巧|课程|训练|教程)|网文|写稿|稿件投稿'),
    ('content','图文创作',8,r'图文|爆文|公众号(?:运营|投稿|创作|变现|流量主)|头条(?:号|文章)|自媒体文章|小红书笔记|笔记创作|笔记制作|图文排版'),
    ('content','内容 IP',7,r'内容ip|自媒体ip|ip孵化|内容创作|人设打造|自媒体创作'),
    ('growth','广告投放',10,r'广告投放|投流|信息流广告|付费流量|全域投放|万相台|无界.{0,12}(?:投放|关键词爆流)|(?:facebook|meta)广告|seo|geo流量|引擎优化.{0,4}geo'),
    ('growth','私域运营',9,r'私域|社群运营|企业微信|复购运营|用户运营'),
    ('growth','获客引流',8,r'精准获客|获客|精准引流|引流|商业营销|增长营销|社媒营销|品牌营销|销售转化|营销策划'),
    ('growth','账号起号',8,r'起号|小红书运营|账号运营|账号定位|新手涨粉|账号增长|养号|涨粉|流量主|账号矩阵|ip起盘'),
    ('business','商业模式',11,r'新个体创业(?:课|必修课)|从零搭建低成本小生意'),
    ('business','商业模式',9,r'商业模式|商业思维|创业(?:课|课程|训练营|经营|研修)|新个体|创业认知|商业认知|商业闭环|一人公司'),
    ('business','企业经营',9,r'企业增长|企业经营|老板ip|经营管理|商业战略|连锁经营|创业管理'),
    ('business','组织与团队',9,r'组织管理|团队管理|员工绩效|股权激励|培训管理|领导力'),
    ('life','个人成长',10,r'负面情绪|精神内耗|个人成长|自我成长|成长研修|自我蜕变|情绪沟通|情绪调节|心力成长|心理疏导|亲密关系|内耗|个人提升'),
    ('life','职场技能',9,r'面试|求职|职业规划|职场(?:成长|技能|沟通|转型)|简历|公开演讲|工作沟通'),
    ('life','生活技能',8,r'烹饪|健身训练|家庭收纳|育儿|兴趣学习|婚礼流程|宠物饲养'),
    ('news','行业观察',8,r'行业观察|行业趋势|行业快讯|市场观察|行业简报|行业报告'),
    ('news','经验案例',7,r'案例复盘|经验分享|做了.{1,9}复盘|真实经营案例|行业案例|复盘实录|复盘笔记'),
    ('software','系统与文件',9,r'文件管理|文件搜索|文件快速复制|文件批量重命名|重复文件|文件时间戳|解压缩|系统驱动|(?:nvidia|显卡|图形)驱动|crystaldiskmark|系统优化|windows常态化|远程桌面|系统清理|虚拟机|系统安装|启动盘|备份工具|分区工具|应用卸载|浏览器数据迁徙|q-dir|xyplorer|ccleaner|peazip|duplicates?\s+same\s+files'),
    ('software','影音工具',10,r'mkvtoolnix|hypersnap|gamescrcpy|视频封装|格式转换|音乐播放器|游戏投屏|投屏工具|mp3tag|video converter|视频下载器|字幕提取工具|音乐下载器'),
    ('software','应用软件',7,r'绿色版|便携版|绿色纯净|绿色精简|便携汉化|中文专业版|绿色便携|纯净版|精简版|google chrome|microsoft edge|ab download manager|下载管理器|电脑工具|软件合集|v?\d{1,4}\.\d+(?:\.\d+){0,3}.{0,5}(?:特别版|正式版|汉化版|高级版|专业版|单文件版)'),
)
SIGNALS = tuple((category,sub,weight,re.compile(expression,re.I)) for category,sub,weight,expression in _RULES)
_TITLE_NOISE = re.compile(r'(?:无需|不用|不需要|不必|免去)(?:再)?(?:直播|剪辑|拍摄|拍视频|开店|囤货|投流|露脸|出镜|懂技术|写代码)')
_TAGS = re.compile(r'^(?:课程内容|课程|项目|内容|软件|公开)?(?:介绍|简介)(?:[:：\s]|$)')
_FROM_LEGACY = {
    'commerce': {'crossborder'}, 'content': {'design','media'},
    'growth': {'business','crossborder','gaming'}, 'automation': {'office','technical','design'},
    'technical': {'design','software','office','gaming'}, 'business': {'crossborder','services','gaming'},
}


def evidence_topics(text):
    candidates = ((weight, -order, category, sub) for order,(category,sub,weight,pattern) in enumerate(SIGNALS)
                  if pattern.search(text))
    return max(candidates, default=(0,0,'',''))


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


def project_topic(title, paragraphs, excerpt, reviewed=None, *, manual=False):
    reviewed=reviewed or {}
    previous=reviewed.get('category','')
    previous_sub=reviewed.get('subcategory','')
    text=_TITLE_NOISE.sub('',normalize('NFKC',title))
    top=evidence_topics(text)
    if top[0]<8:
        preview=intro_preview(title,paragraphs,excerpt)
        body=evidence_topics(_TITLE_NOISE.sub('',normalize('NFKC',preview)))
        if body[0]>top[0]:
            top=body
    weight,_,category,sub=top
    if previous in CATEGORIES:
        if manual:
            return previous,previous_sub
        if previous=='content' and previous_sub in ('短视频','短剧与漫剧'):
            return ('media','短视频制作') if previous_sub=='短视频' else ('design','动画与短剧')
        if category==previous and previous_sub:
            return previous,previous_sub
        if category not in _FROM_LEGACY.get(previous,()) or weight<8:
            return previous,previous_sub
    return (category,sub) if weight>=7 else ('','')
