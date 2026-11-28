"""Read-only analysis advice grounded in the current catalog and textbook receipts."""

from dataclasses import dataclass
import re
from unicodedata import normalize

from catalog_model import safe_text
from catalog_relations import article_identity
from catalog_semantics import article_key


MAX_PEERS = 6
MAX_OUTLINE = 160
_OUTLINE_START = re.compile(r'^(?:[一二三四五六七八九十]+[、.．]\s*)?(?:课程目录|课程大纲|课程核心模块|教学目录|教学大纲)\s*[:：]?$')
_OUTLINE_END = re.compile(r'^(?:[一二三四五六七八九十]+[、.．]\s*)?(?:适合人群|学习收获|配套资料|下载方式|网盘下载|免责声明|课程亮点)\s*[:：]?$')
_CONDITIONS = re.compile(r'适合|适用|适配|无需.{1,12}基础|零基础|兼容|最低配置|须先|仅支持|需要自备')
_METHOD = re.compile(r'讲解|演示|实操|步骤|教程|操作|工作流|软件功能|支持将|用于.{2,16}(?:编辑|生成|制作)')
_MONETIZATION = re.compile(r'广告分成|平台分成|带货佣金|达人佣金|流量主|挂小黄车|接单|付费咨询|订阅收费|销售商品|'
                          r'变现(?:路径|模式|方式)|收费模式|靠.{2,20}(?:获客|承接订单|变现)')
_COST = re.compile(r'(?:预算|成本|费用|订阅|算力|人工|投入工时|设备).{0,18}(?:\d|免费|付费|按次|每月|每天|显存)|'
                   r'(?:每月|每年|按次|每人).{0,12}(?:\d+\s*(?:元|美元|小时)|付费)|'
                   r'(?:免费额度|显存|工作时长|人工投入|最低配置).{0,12}\d')
_MATERIAL = re.compile(r'提示词|模板|工作流文件|可编辑|素材包|工程文件|案例|配套资料|脚本')
_MEDIA = re.compile(r'\.(?:mp4|mkv|avi|mov|pdf|docx?)$', re.I)
_TREE = re.compile(r'^[│├└─\s]+')
_LEADING = re.compile(r'^(?:第\s*[零一二三四五六七八九十\d]+\s*(?:课|讲|章)\s*[:：.、-]?|\d{1,3}(?:[.．]\d{1,3}){0,2}[.．、)\]：:\s-]*)\s*')
_VIDEO_LABEL = re.compile(r'(?:_x264|\s*\(\d+\)|_)+$', re.I)
_BAD_TEXT = re.compile(r'提取码|访问密码|解压密码|推广专用|\b(?:pwd|token)\s*=|群号|加我微信|'
                       r'(?:[日月年][入赚]|躺赚|收益|成功率|回本|稳定日赚)\s*\d', re.I)
_TOOL_NAME = re.compile(
    r'(?<![A-Za-z0-9])(?:MKVToolNix|HyperSnap|ChatGPT|Claude|Gemini|DeepSeek|Midjourney|ComfyUI|'
    r'Photoshop|Premiere(?: Pro)?|After Effects|DaVinci Resolve|Suno|Grok(?: Bot)?|Muse|Dots|'
    r'剪映(?:专业版)?|即梦(?:AI)?|可灵(?:AI)?|豆包|海螺(?:AI)?|通义万相|飞书多维表格|小龙虾)(?![A-Za-z0-9])',
    re.I)
_NUMBERED_VERSION = re.compile(r'(?<![\w.])v?\d+(?:\.\d+){1,3}(?![A-Za-z0-9.])', re.I)
_AUTHOR_LINE = re.compile(r'^\s*(?:原作者|作者|主讲人|讲师)\s*[:：]\s*([^\s，。；、]{2,22})')
_AUTHOR_COURSE = re.compile(r'本(?:课程|课)由([^\s，。；、]{2,16})(?:主讲|亲授|录制|制作)|本课程为([^\s，。；、]{2,12})专属')
_QUOTE = re.compile(r'[《「“]([^》」”]{5,28})[》」”]')
_THEME_KIND = re.compile(r'(.{3,17}?)(科普|作图|剪辑|直播|运营|写作|带货|绘本|提示词|自动化)$')
_TOPIC_IGNORE = re.compile(r'全套|零基础|实战课|入门课|快速变现|一键|万能|完整版')
_FINISHED = {'completed', 'completed_received_scope', 'same_source_reuse'}


def compact_key(value):
    return re.sub(r'[^\w]|_', '', normalize('NFKC', value).casefold())


def visible(value, size=140):
    if not isinstance(value, str):
        return ''
    clean = value.replace('\u200b', '').strip()
    return safe_text(clean, size) if not _BAD_TEXT.search(clean) and '://' not in clean else ''


def intro_lines(row):
    return [line for line in row.get('paragraphs') or () if isinstance(line, str) and line.strip()][:1000]


def published_intro(coverage):
    return {'complete':'完整简介', 'full':'完整简介', 'partial':'部分简介',
            'excerpt':'来源摘录', 'image_only':'原页图片'}.get(coverage)


def outline_key(line):
    return compact_key(_VIDEO_LABEL.sub('', _MEDIA.sub('', _LEADING.sub('', _TREE.sub('', line)))))


def teaching_outline(lines):
    result = {}
    in_outline = False
    for raw in lines:
        line = raw.strip().replace('\u200b', '')
        if _OUTLINE_START.match(line):
            in_outline = True
            continue
        if _OUTLINE_END.match(line):
            in_outline = False
            continue
        if not (_MEDIA.search(line) or (in_outline and _LEADING.match(_TREE.sub('', line)))):
            continue
        key = outline_key(line)
        label = visible(_MEDIA.sub('', line), 120).lstrip('│├└─ ').strip()
        if len(key) >= 3 and label and key not in result:
            result[key] = label
        if len(result) == MAX_OUTLINE:
            break
    return result


def chosen_lines(lines, pattern, maximum):
    result = []
    for line in lines:
        if pattern.search(line) and (entry := visible(line)) and entry not in result:
            result.append(entry)
        if len(result) == maximum:
            break
    return result


def named_author(row, lines):
    author = (row.get('analysis') or {}).get('author')
    if author and any(author in line for line in [row.get('title') or '', *lines]):
        return visible(author, 40)
    names = set()
    for line in lines:
        if match := (_AUTHOR_LINE.search(line) or _AUTHOR_COURSE.search(line)):
            name = next(value for value in match.groups() if value)
            if not re.search(r'博主|团队|网上|某|官方|\d', name):
                names.add(name)
    return visible(names.pop(), 40) if len(names) == 1 else ''


@dataclass(frozen=True)
class Entry:
    summary: dict
    outline: dict


def summary(row, progress_fields):
    lines = intro_lines(row)
    source_coverage = row.get('coverage')
    outline = teaching_outline(lines)
    tools = {}
    title = row['title']
    title_tools = list(_TOOL_NAME.finditer(title))
    versions = _NUMBERED_VERSION.findall(title)
    if len(title_tools) == 1 and len(versions) == 1:
        name = title_tools[0][0]
        tools[compact_key(name)] = f'{name} {versions[0]}'
    for line in [title, *lines]:
        if not visible(line):
            continue
        for match in _TOOL_NAME.finditer(line):
            name = match[0].strip()
            tools.setdefault(compact_key(name), name)
    analysis = row.get('analysis') or {}
    fields = {
        'id':row['id'], 'article_key':row.get('article_key'), 'title':row['title'],
        'site_label':row.get('site_label'), 'version_label':row.get('version_label'),
        'published_at':row.get('published_at'), 'introduction':published_intro(source_coverage),
        'author':named_author(row, lines), 'objective':visible(analysis.get('goal')),
        'methods':list(outline.values())[:4] or chosen_lines(lines, _METHOD, 3),
        'tools':list(tools.values())[:8], 'conditions':chosen_lines(lines, _CONDITIONS, 3),
        'monetization':chosen_lines(lines, _MONETIZATION, 3), 'costs':chosen_lines(lines, _COST, 3),
        'materials':chosen_lines(lines, _MATERIAL, 3),
    }
    fields.update({key:progress_fields[key] for key in ('analysis_state','analysis_scope','textbook_href')
                   if progress_fields.get(key)})
    return Entry({key:value for key,value in fields.items() if value}, outline)


def current_context_pairs(library):
    rows = getattr(library, 'semantic_rows', {})
    for record in getattr(library, 'semantic_relations', ()):
        if record.get('relation') not in ('related', 'contains') or record.get('revoked'):
            continue
        left, right = record.get('left') or {}, record.get('right') or {}
        pair = [rows.get(article_key(ref.get('site_id'), ref.get('article_id'))) for ref in (left, right)]
        if any(not row or not ref.get('input_hash') or row.get('input_hash') != ref['input_hash']
               for row, ref in zip(pair, (left, right))):
            continue
        yield record, *pair


def refs_from_relations(library, source):
    for record, left, right in current_context_pairs(library):
        if source.get('article_key') == left.get('article_key'):
            yield right, record['relation']
        elif source.get('article_key') == right.get('article_key'):
            yield left, record['relation']


def playbook_topic(left, right):
    """Use a shared name when both introductions support it; keep unnamed pairs distinct."""
    titles = [row['title'] for row in (left, right)]
    if compact_key(titles[0]) == compact_key(titles[1]):
        return compact_key(titles[0]), safe_text(titles[0], 72)
    for title, other in (titles, titles[::-1]):
        for topic in _QUOTE.findall(title):
            if (compact_key(topic).removeprefix('ai') in topics_in_title(title)
                    and title_has_topic(other, compact_key(topic).removeprefix('ai'))):
                return compact_key(topic).removeprefix('ai'), safe_text(topic, 72)
    # A reviewed association supports this comparison alone without a shared topic name.
    key = tuple(sorted((left['article_key'], right['article_key'])))
    return key, safe_text(titles[0], 58) + ' · 同类玩法'


def playbook_recommendation(focus, lineage, formal):
    if formal:
        scope = formal.get('scope')
        if scope in ('核心待补', '配套待补'):
            return {'action':'先读教材，再核对补件', 'reason':f'当前版本已有可读教材，已验收范围为{scope}。'}
        if formal.get('reused'):
            return {'action':'打开已有教材', 'reason':'当前项目已有登记的同源教材，可先阅读其覆盖范围。'}
        return {'action':'打开已有教材', 'reason':'当前版本已有对应教材和同版验收记录。'}
    def has_book(record):
        book = record['version']
        return book.get('analysis_state') in _FINISHED and book.get('textbook_href')

    if any(r['relation'] == 'same_edition_source' and has_book(r) for r in lineage):
        return {'action':'核对同版后复用', 'reason':'另一来源已有对应教材。先核对当前来源的实际资料范围。'}
    if any(r['relation'] == 'older' and has_book(r) and r.get('comparison') for r in lineage):
        return {'action':'核对后增量分析', 'reason':'旧版已有教材，简介列出了变化。先核对新版原件和实际重合范围。'}
    if focus.get('introduction') == '完整简介' and focus.get('methods'):
        return {'action':'可安排完整分析',
                'reason':'简介列有教学方法，当前同版教材和与其他课程的实际重合范围仍待核实。'}
    return {'action':'暂缓判定', 'reason':'现有简介不足以确定教学步骤或新增价值。'}


def playbook_entry(library, source, progress_for):
    detail = library.relations.details(source)
    decision = analysis_decision(library, source, detail, progress_for, include_candidates=False)
    focus = decision.get('focus') or summary(source, progress_for(source)).summary
    analysis = source.get('analysis') or {}
    fields = {**focus,
              'platforms': [label for row in source.get('platforms', ()) if (label := visible(row.get('label'), 28))],
              'version_dates': [label for date in analysis.get('dates', ()) if date.get('meaning') == 'version'
                                and (label := visible(date.get('text'), 72))][:3]}
    covered = progress_for(source)
    lineage = [entry for entry in decision.get('comparisons', ())
               if entry['relation'] in ('same_edition_source', 'different_version', 'older', 'newer')]
    known_course = covered.get('course_completion') or {}
    formal = None
    if covered.get('textbook_href') and covered.get('analysis_state') in _FINISHED:
        formal = {'href':covered['textbook_href'], 'scope':covered.get('analysis_scope'),
                  'received':known_course.get('received_analysis') == 'accepted_for_edition',
                  'reused':covered['analysis_state'] == 'same_source_reuse'}
    return {key:value for key,value in {
        **fields, 'lineage':lineage, 'formal':formal,
        'pending': ['课程实物的重合范围'] + [label for key, label in (
            ('version_dates', '版次时间'), ('costs', '费用和投入')) if not fields.get(key)],
        'recommendation':playbook_recommendation(focus, lineage, formal),
    }.items() if value}


def cross_author_playbooks(library, *, progress_for=None):
    """Organize reviewed comparisons while keeping every relationship bound to its evidence."""
    model = getattr(library, 'relations', None)
    if model is None:
        return []
    groups = {}
    progress = {}

    def trusted_progress(row):
        key = row['article_key']
        if key not in progress:
            observed = progress_for(row) if progress_for else {}
            # A name match helps users find notes, but cannot decide topic coverage.
            progress[key] = {} if observed.get('analysis_state') == 'local_result_available' else observed
        return progress[key]

    for record, left, right in current_context_pairs(library):
        if record['relation'] != 'related' or any(row.get('demo') or row.get('action_pending')
                                                 for row in (left, right)):
            continue
        if model.series.anchor[article_identity(left)] == model.series.anchor[article_identity(right)]:
            continue
        authors = [named_author(row, intro_lines(row)) for row in (left, right)]
        if not all(authors) or compact_key(authors[0]) == compact_key(authors[1]):
            continue
        key, theme = playbook_topic(left, right)
        group = groups.setdefault(key, {'theme':theme, 'entries':{}, 'comparisons':[]})
        evidence = {}
        for side, row, author in zip(('left', 'right'), (left, right), authors):
            snippets = dict.fromkeys(visible(proof.get('text')) for proof in record.get('evidence', ())
                                     if proof.get('side') == side)
            token = row['article_key']
            if token not in group['entries']:
                group['entries'][token] = {**playbook_entry(library, row, trusted_progress), 'author':author}
            evidence[side] = [text for text in snippets if text][:2]
        gap = differences(summary(left, {}), summary(right, {}), 'related')
        group['comparisons'].append({'left':left['article_key'], 'right':right['article_key'],
                                     'reason':visible(record.get('reason')), 'evidence':evidence, 'comparison':gap})
    for group in groups.values():
        group['entries'] = sorted(group['entries'].values(), key=lambda row: (row.get('published_at') or '',
                                                                            row['article_key']))
    return sorted(groups.values(), key=lambda group: group['entries'][-1].get('published_at', ''), reverse=True)


def topics_in_title(title):
    words = []
    for raw in _QUOTE.findall(title):
        topic = re.sub(r'^ai', '', compact_key(raw), flags=re.I)
        if len(topic) >= 5 and not _TOPIC_IGNORE.search(topic) and _THEME_KIND.fullmatch(topic):
            words.append(topic)
    return tuple(dict.fromkeys(words))[:3]


def title_has_topic(title, topic):
    plain = compact_key(title)
    if topic in plain:
        return True
    signature = _THEME_KIND.fullmatch(topic)
    if not signature:
        return False
    subject, activity = signature.groups()
    return re.search(rf'{re.escape(subject)}.{{0,8}}{re.escape(activity)}', plain) is not None


def same_playbook_candidates(library, source, excluded):
    rows = getattr(library, 'semantic_rows', {})
    source_title = source.get('title') or ''
    topics = topics_in_title(source_title)
    if not topics:
        return []
    candidates = []
    for row in rows.values():
        token = row.get('article_key')
        if (token in excluded or row.get('demo') or row.get('action_pending') or
                row.get('category') != source.get('category')):
            continue
        if any(title_has_topic(row.get('title') or '', topic) for topic in topics):
            candidates.append(row)
    return sorted(candidates, key=lambda row: (row.get('coverage') in ('complete','full'),
                                               row.get('published_at') or '', row['article_key']), reverse=True)[:2]


def differences(focus, peer, relation):
    if relation not in ('different_version', 'newer', 'older', 'related'):
        return {}
    compare = {}
    if focus.outline and peer.outline:
        each = (('focus_only', focus.outline, peer.outline), ('peer_only', peer.outline, focus.outline))
        for key, primary, secondary in each:
            count = sum(label not in secondary for label in primary)
            if count:
                compare[key] = {'count':count, 'examples':[primary[label] for label in primary if label not in secondary][:5]}
        if (common := len(focus.outline.keys() & peer.outline.keys())):
            compare['shared_outline_count'] = common
    for field in ('tools', 'conditions', 'monetization', 'costs', 'materials'):
        for prefix, a, b in (('focus', focus, peer), ('peer', peer, focus)):
            current = a.summary.get(field, ())
            reference = {compact_key(item) for item in b.summary.get(field, ())}
            if current and reference and (only := [item for item in current if compact_key(item) not in reference]):
                compare[f'{prefix}_{field}'] = only
    return compare


def advice(focus, comparisons):
    state, scope = focus.get('analysis_state'), focus.get('analysis_scope')
    if state in _FINISHED and scope in ('核心待补', '配套待补'):
        return '先看已有教材，再核对未收资料。'
    if state in _FINISHED and focus.get('textbook_href'):
        return '直接打开已有同版教材。'
    if state in ('in_progress','awaiting_acceptance','awaiting_re_review'):
        return '查看现有处理进度。'
    if any(entry['relation'] == 'same_edition_source' and entry['version'].get('analysis_state') in _FINISHED
           and entry['version'].get('textbook_href') for entry in comparisons):
        return '先打开已验收的来源教材，再核对当前资料范围。'
    if focus.get('introduction') != '完整简介':
        return '先核对来源简介，再决定分析范围。'
    if any(entry['version'].get('analysis_state') in _FINISHED and entry['relation'] in ('newer','older','different_version')
           for entry in comparisons):
        return '先对照已有教材，优先核实有差异的内容。'
    if any(entry.get('comparison') for entry in comparisons):
        return '先核对各版独列的内容，再安排分析。'
    if comparisons:
        return '按学习目标选择资料，作者与内容范围随来源核实。'
    return ''


def analysis_decision(library, source, detail, progress_for, *, include_candidates=True):
    """Compare introductions first and read textbook coverage from the exact formal join."""
    if source.get('demo') or source.get('action_pending') or not source.get('article_key'):
        return {}
    rows = getattr(library, 'semantic_rows', {})
    model = getattr(library, 'relations', None)
    current = summary(source, progress_for(source))
    peers = []
    seen = {source['article_key']}
    for field, kind in (('other_versions','different_version'),('other_sources','same_edition_source')):
        for entry in detail.get(field) or ():
            token = entry.get('article_key')
            row = rows.get(token)
            if row and token not in seen and not row.get('action_pending'):
                seen.add(token)
                peers.append((row, kind))
    for row, kind in refs_from_relations(library, source):
        if row['article_key'] not in seen and not row.get('action_pending'):
            seen.add(row['article_key'])
            peers.append((row, kind))
    if include_candidates and len(peers) < MAX_PEERS:
        peers.extend((row, 'topic_candidate') for row in same_playbook_candidates(library, source, seen))

    comparisons = []
    for row, kind in peers[:MAX_PEERS]:
        peer = summary(row, progress_for(row))
        if model and kind == 'different_version':
            own = model.versions.anchor.get(article_identity(source))
            other = model.versions.anchor.get(article_identity(row))
            if other in model.newer.get(own, ()):
                kind = 'newer'
            elif other in model.older.get(own, ()):
                kind = 'older'
        record = {'relation':kind, 'version':peer.summary}
        if gap := differences(current, peer, kind):
            record['comparison'] = gap
        authors = current.summary.get('author'), peer.summary.get('author')
        if all(authors):
            record['author_relation'] = 'same' if compact_key(authors[0]) == compact_key(authors[1]) else 'different'
        comparisons.append(record)
    recommendation = advice(current.summary, comparisons)
    if not comparisons and not any(current.summary.get(key) for key in ('methods', 'tools', 'conditions','textbook_href')):
        return {}
    return {'focus':current.summary, 'comparisons':comparisons, 'advice':recommendation}
