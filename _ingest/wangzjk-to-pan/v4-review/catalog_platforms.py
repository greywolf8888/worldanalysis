"""Find operating, publication and customer channels in local catalog evidence."""

import re
from unicodedata import normalize


PLATFORM_REVISION = 'platform-purpose-1'

# Put specific forms before more general aliases when they share a prefix.
_ALIASES = {
    'xiaohongshu': ('小红书', r'小红书|(?<![A-Za-z0-9])XHS(?![A-Za-z0-9])'),
    'tiktok': ('TikTok', r'海外(?:版)?抖音|国际版抖音|Tik[\s-]?Tok|(?<![A-Za-z0-9])TK(?=\s*(?:小店|美区|跨境|shop|电商|带货))'),
    'douyin': ('抖音', r'抖音|抖店|巨量千川'),
    'wechat_miniprograms': ('微信小程序', r'微信小程序'),
    'shipinhao_shop': ('微信小店', r'微信小店|视频号小店'),
    'wechat_articles': ('公众号', r'微信公众号|微信公众平台|公众号'),
    'wechat_video': ('视频号', r'微信视频号|视频号'),
    'wechat': ('微信', r'企业微信|企微|微信'),
    'kuaishou': ('快手', r'快手'),
    'bilibili': ('B站', r'哔哩哔哩|bilibili|(?<![A-Za-z0-9])B站'),
    'youtube': ('YouTube', r'YouTube|油管'),
    'toutiao': ('头条', r'今日头条|头条号|头条(?=\s*(?:[+、/&×)|]|AI|爆文|创作|文章|托管|账号|流量|运营|发布|发文|收益|变现))'),
    'baijia': ('百家号', r'百家号|百家(?=\s*(?:[+、/&×]|投稿|运营|创作|发文|收益|矩阵|变现))'),
    'zhihu': ('知乎', r'知乎'),
    'weibo': ('微博', r'新浪微博|微博'),
    'hongguo': ('红果短剧', r'红果短剧|红果漫剧'),
    'fanqie': ('番茄小说', r'番茄小说|番茄(?=\s*(?:小说|写小说|作家助手|投稿|签约|短篇小说))'),
    'qidian': ('起点', r'起点中文网|起点(?=\s*(?:小说|网文|投稿|签约|作家|写作))'),
    'taptap': ('TapTap', r'Tap[\s-]?Tap'),
    'taobao': ('淘宝', r'淘宝|淘系'),
    'tmall': ('天猫', r'天猫(?!魔盒|精灵)'),
    'xianyu': ('闲鱼', r'闲鱼|咸鱼(?=\s*(?:卖货|开店|店群|店铺|电商|选品|虚拟|运营|无货源))'),
    'temu': ('Temu', r'海外(?:版)?拼多多|国际版拼多多|(?<![A-Za-z0-9])Temu(?![A-Za-z0-9])'),
    'pinduoduo': ('拼多多', r'拼多多|多多进宝|多多买菜|多多(?=\s*(?:虚拟(?:类目|商品|资料|产品|资源)|店群|店铺|无货源|选品|电商|运营))'),
    'jd': ('京东', r'京东|(?<![A-Za-z0-9])JD(?=\s*(?:联盟|商家|店铺|外卖|自营))'),
    'dianping': ('大众点评', r'大众点评'),
    'meituan': ('美团', r'美团'),
    'eleme': ('饿了么', r'饿了么'),
    'dewu': ('得物', r'得物'),
    'amazon': ('亚马逊', r'亚马逊(?!\s*(?:云|AWS))|Amazon(?![\w]|\s*(?:Web\s+Services|AWS|云))'),
    'alibaba1688': ('1688', r'阿里巴巴1688|(?<![0-9])1688(?![0-9])'),
    'alibaba_intl': ('阿里国际站', r'阿里巴巴国际站|阿里国际站|Alibaba\.com'),
    'aliexpress': ('速卖通', r'全球速卖通|速卖通|AliExpress'),
    'shopee': ('Shopee', r'Shopee|虾皮'),
    'walmart': ('沃尔玛', r'沃尔玛|Walmart'),
    'shopify': ('Shopify', r'Shopify'),
    'facebook': ('Facebook', r'Facebook|(?<![A-Za-z0-9])FB(?=\s*(?:广告|投放|获客|引流|社媒|主页))'),
    'ctrip': ('携程', r'携程|Trip\.com'),
    'linkedin': ('LinkedIn', r'LinkedIn|领英'),
    'pinterest': ('Pinterest', r'Pinterest'),
    'twitter': ('X / Twitter', r'Twitter|推特'),
    'reddit': ('Reddit', r'Reddit'),
    'google_ads': ('Google Ads', r'Google\s+Ads|谷歌广告'),
    'tencent_video': ('腾讯视频', r'腾讯视频'),
    'instagram': ('Instagram', r'Instagram|(?<![A-Za-z0-9])INS(?=\s*(?:运营|引流|获客|营销|账号|涨粉|广告))'),
    'baidu_pan': ('百度网盘', r'百度(?:网盘|云盘)'),
    'uc_pan': ('UC 网盘', r'UC\s*(?:网盘|云盘)|UC(?=\s*(?:拉新|拉转|拉存))'),
    'quark_pan': ('夸克网盘', r'夸克网盘|夸克(?=\s*(?:网盘|拉新|拉转|拉存))'),
    'xunlei_pan': ('迅雷网盘', r'迅雷(?:网盘|云盘)|迅雷(?=\s*(?:网盘|拉新|拉转|拉存))'),
}
PLATFORMS = {key: label for key, (label, _) in _ALIASES.items()} | {'general': '通用'}
FILTER_ROLES = frozenset({'use', 'acquire', 'receive'})

_REFERENCES = re.compile(
    '|'.join(f'(?P<{key}>{pattern})' for key, (_, pattern) in _ALIASES.items()),
    re.I,
)
_CLAUSES = re.compile(r'[。；;！？!?\n\r，,]+')
_ACTIVITY = re.compile(
    r'经营|运营|营销|获客|销售|变现|起号|涨粉|引流|投流|投放|广告|私域|'
    r'发帖|发文|投稿|发布|上传|创作|内容账号|爆文|流量主|写手|签约|推文|'
    r'店铺|抖店|小店|开店|店群|带货|卖货|商品|电商|跨境|外贸|选品|上架|出单|下单|'
    r'虚拟(?:资料|产品|资源|商品|类目|项目)|佣金|团购|外卖|探店|商家|接单|支付|成交|'
    r'直播|开播|播主|视频起号|短视频|原创视频|平台收益|作品.{0,4}收益|剧场|短剧|图文|笔记|解说|达人|UP主|分成|'
    r'拉新|拉转|拉存|推广|公众号流量|平台玩法|合规玩法|联动玩法|赛道|训练营|特训营|实操课|运营课|群运营|'
    r'启动账号|矩阵|经营号|账号定位|账号运营|好物|种草|网店|建站|独立站|小程序开发|'
    r'测品|测款|承接|卡券|入驻|接咨询|收款|店长|创收|实战营|掘金|新玩法|Ads|激励|'
    r'自然流拉|流量操盘|人群资产|一稿多发|买手项目|租赁项目|吞私信|私信(?:截流|获客)|大众点评玩法|'
    r'游戏(?:开发|上架|推广|运营)|微信小程序.{0,4}(?:收益|项目)|(?:日收益|日入).{0,10}微信小程序',
    re.I,
)
_TOOL_CONTEXT = re.compile(
    r'下载(?:器|助手|工具|脚本|插件|视频|字幕)|(?:批量|免费|一键)(?:视频|素材)?下载|'
    r'录播|录屏|录制工具|录制助手|链接解析(?:工具|下载)|解析.{0,5}下载|'
    r'(?:素材|数据|视频|图片)采集工具|内容提取(?:器|助手)|浏览器插件|浏览器扩展|'
    r'抓包|视频播放器|工具支持|软件支持|兼容平台|截图工具|监控工具|视频在线下载',
    re.I,
)
_DELIVERY_ONLY = re.compile(
    r'网盘下载|网盘领取|网盘交付|网盘提取码|扫码领取|加微信领|添加微信领|'
    r'加微信.{0,6}(?:领资料|咨询课件)|(?:在|用)?(?:网盘|微信)(?:分享|发送)(?:资料|讲义|课件)',
)
_CLOUD_CHANNELS = {'baidu_pan','quark_pan','xunlei_pan','uc_pan'}
_RECEIPT_CHANNELS = _CLOUD_CHANNELS | {'wechat'}
_DELIVERY_AFTER_ALIAS = re.compile(
    r'^\s*(?:[:：/－—]\s*)?(?:链接|提取码|下载地址|下载(?:资料|课件|原件|附件)|'
    r'存放资料|(?:网盘)?领取(?:资料|课件|附件)|(?:链接)?交付|分享(?:链接|资料|课件))'
)
_CLOUD_OPERATION = re.compile(r'拉新|拉转|拉存|推广|佣金|代理|渠道|转存任务')
_WECHAT_OPERATION = re.compile(r'企微|企业微信|承接|私域|社群|营销|获客|变现|销售|成交|加粉|接咨询|收款|引流')
_SOURCE_ONLY_ARTICLE = re.compile(r'^\s*(?:某|来自|摘自|转自|来源|转载自)?(?:微信)?公众号(?:原创|付费)?文章\s*[：:《「]')
_EXPLICIT_SKIP = re.compile(
    r'(?:不做|不靠|不走|不使用|不运营|不玩|别做|无需|不用|不需要|不在|不是在|不会在|禁止|排除|除去|'
    r'不依赖|不要(?:做|用|在)?|避开)\s*(?:再|去|在|做|用|运营|使用|依赖|走|选)?\s*$'
)
_AFTER_EXCLUDED = re.compile(r'^\s*(?:(?:平台|渠道)?(?:除外|不适用|不参与|不考虑|不能用)|(?:只)?作为(?:素材|参考|对标))')
_REFERENCE_ONLY = re.compile(r'(?:来自|摘自|源自|转自|参考|对照|对标|类似|不用依赖|原素材来自)\s*(?:平台)?\s*$')
_SOURCE_AFTER_ALIAS = re.compile(r'^\s*(?:拿货|找货源|(?:高赞问答|热门问答|现成素材|原视频).{0,8}(?:搬运|采集|作为素材|收集))')
_JOIN = re.compile(r'^\s*(?:[、/+/&]|与|和|及|或|以及|或者|还有|联动)\s*$')
_LIST_PADDING = re.compile(r'(?:[\s、/+&×:()·\[\]-]|以及|或者|适用于|适合|涵盖|覆盖|包括|包含|与|和|及|或|等|多平台|跨平台|平台|渠道)*')
_HISTORICAL = re.compile(r'实操过|做过|从事过|接触过|早先运营过|曾经运营过')
_HARD_OPERATION = re.compile(r'推广|经营|运营|开店|发布|起号|变现|获客|投稿|拉新|商品上架|上架店铺')
_CLAUSE_COMPARE = re.compile(r'^\s*(?:[vV][sS]\.?\s*|对比|比较)|(?:两者)?(?:差异|区别)(?:一览|分析|对照)?$')


def _signals_in_clause(clause, title_scope=False):
    has_activity = _ACTIVITY.search(clause)
    if not has_activity and not title_scope:
        return
    matches = list(_REFERENCES.finditer(clause))
    if not matches:
        return
    shared_list = False
    if title_scope and (not has_activity or len(matches) > 1):
        rest = []
        end = 0
        for match in matches:
            rest.append(clause[end:match.start()])
            end = match.end()
        rest.append(clause[end:])
        shared_list = _LIST_PADDING.fullmatch(''.join(rest))
    if not has_activity and not shared_list:
        return
    tool = _TOOL_CONTEXT.search(clause)
    source_article = _SOURCE_ONLY_ARTICLE.search(clause)
    previous = None
    negative_group = False
    for match in matches:
        pid = match.lastgroup
        before = clause[:match.start()]
        left = before[-34:]
        right = clause[match.end():match.end()+38]
        joined = previous is not None and _JOIN.fullmatch(clause[previous.end():match.start()])
        denied = bool(_EXPLICIT_SKIP.search(left) or (joined and negative_group))
        previous = match
        negative_group = denied
        if denied or _AFTER_EXCLUDED.search(right) or _REFERENCE_ONLY.search(left) or _HISTORICAL.search(left):
            continue
        if source_article and pid == 'wechat_articles' and match.start() < source_article.end():
            continue
        if _SOURCE_AFTER_ALIAS.search(right) or _CLAUSE_COMPARE.search(right):
            continue
        if pid in _RECEIPT_CHANNELS and _DELIVERY_AFTER_ALIAS.search(right):
            continue
        nearby = left + match[0] + right
        if not shared_list and not _ACTIVITY.search(nearby):
            continue
        if tool and not _HARD_OPERATION.match(right.lstrip(' 、/·-：:')):
            continue
        if pid == 'wechat':
            if not _WECHAT_OPERATION.search(nearby) or _DELIVERY_ONLY.search(nearby):
                continue
        if pid in _CLOUD_CHANNELS and not _CLOUD_OPERATION.search(nearby):
            continue
        yield pid


def _mentions(text, *, title=False):
    if not text:
        return
    normalized = normalize('NFKC', text)
    title_scope = title and bool(_ACTIVITY.search(normalized)) and not _TOOL_CONTEXT.search(normalized)
    for clause in _CLAUSES.split(normalized):
        yield from _signals_in_clause(clause, title_scope)


def project_platforms(title, paragraphs, excerpt, reviewed=None, *, manual=False):
    """Return platform IDs with evidence of the project's intended operating scope."""
    verdicts = (reviewed or {}).get('platforms', ())
    excluded = {p['id'] for p in verdicts if p['role']=='excluded'}
    accepted = dict.fromkeys(p['id'] for p in verdicts if p['id'] in PLATFORMS and p['role'] in FILTER_ROLES
                            and p['id'] not in excluded)
    if manual:
        return tuple(accepted)
    decided = {p['id'] for p in verdicts}
    if _TOOL_CONTEXT.search(title) and not _HARD_OPERATION.search(title):
        return tuple(accepted)
    clipped = re.sub(r'\s*\[\s*(?:…|\.\.\.)\s*\]$', '', excerpt or '').strip()
    texts = dict.fromkeys((title, *paragraphs, '' if title.startswith(clipped) else excerpt))
    for text in texts:
        for pid in _mentions(text, title=text==title):
            if pid not in decided:
                accepted[pid] = None
    return tuple(accepted)
