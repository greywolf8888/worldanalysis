"""Project a public WordPress response into visible text and bounded display blocks."""
import json
import re
import sys

from lxml import etree, html

from catalog_model import ORIGIN, SITE, safe_image, safe_text
from intro_inputs import make_intro

GATE = re.compile(r'内容需要权限查看|登录后查看全文|请(?:付费|购买|登录)后查看|无权查看|解锁全文|访问过于频繁|安全验证后继续|输入提取码')
HIDE = re.compile(r'(?:paywall|pay-download|hidden-content|hide-content|access-restricted|login-required|vip-visible|download-box|post-pay|wp-block-embed|\b(?:hide|hidden|d-none)\b)', re.I)
HIDDEN_STYLE = re.compile(r'display\s*:\s*none|visibility\s*:\s*hidden', re.I)
ACCESS_LINK = re.compile(r'pan\.baidu\.com|/pay-download/|/(?:login|wp-login)', re.I)
SKIP = {'script','style','noscript','iframe','form','input','button','video','audio','object','embed','svg','template','source','track','meta','link'}
FENCE = {'p','li','ul','ol','div','section','figure','figcaption','br','blockquote','tr','h1','h2','h3','h4','h5','h6','pre','hr'}


def visible_blocks(raw, markup_limit):
    if not isinstance(raw, str):
        return [], False, 'markup_unavailable'
    if len(raw) > markup_limit:
        return [], False, 'markup_budget'
    if not raw.strip():
        return [], False, None
    parser = html.HTMLParser(recover=True, no_network=True, remove_comments=True)
    root = html.fragment_fromstring(raw, create_parent='div', parser=parser)
    output, pending = [], []
    has_images = False
    partial_reason = None

    def flush():
        nonlocal partial_reason
        value = ''.join(pending)
        pending.clear()
        if gate := GATE.search(value):
            # Unmarked access warnings can precede an unmarked gated suffix.
            value = value[:gate.start()]
            partial_reason = 'unmarked_access_boundary'
        if paragraph := safe_text(value, 3 * len(value) + 100):
            output.append({'kind': 'text', 'text': paragraph})

    def walk(element, depth):
        nonlocal has_images, partial_reason
        if partial_reason or not isinstance(element.tag, str):
            return
        if depth > 65:
            flush()
            partial_reason = 'markup_depth'
            return
        tag, attributes = element.tag.lower(), element.attrib
        if (tag in SKIP or 'hidden' in attributes
                or attributes.get('aria-hidden', '').lower() == 'true'
                or HIDDEN_STYLE.search(attributes.get('style', ''))
                or HIDE.search(attributes.get('class', '') + ' ' + attributes.get('id', ''))):
            return
        if tag in FENCE:
            flush()
        if partial_reason:
            return
        if tag == 'img':
            has_images = True
            if link := safe_image(attributes.get('data-src')) or safe_image(attributes.get('src')):
                flush()
                if not partial_reason:
                    output.append({'kind': 'image', 'url': link, 'alt': safe_text(attributes.get('alt'), 400)})
            return
        if tag == 'a' and ACCESS_LINK.search(attributes.get('href', '')):
            return
        if element.text:
            pending.append(element.text)
        for child in element:
            walk(child, depth + 1)
            if partial_reason:
                return
            if child.tail:
                pending.append(child.tail)
        if tag in FENCE:
            flush()

    walk(root, 0)
    if not partial_reason:
        flush()
    return output, has_images, partial_reason


def display_blocks(sequence, limit):
    """Bound only the image-rich detail cache, never the separately retained text."""
    return [({**block, 'text': block['text'][:16000]} if block['kind'] == 'text' else block)
            for block in sequence[:limit]]


def blocks(raw, *, block_limit=180, markup_limit=95_000):
    sequence, _, partial_reason = visible_blocks(raw, markup_limit)
    if not sequence and partial_reason:
        return None
    return display_blocks(sequence, block_limit)


def project(sample, *, block_limit=180, markup_limit=95_000, include_intro=False):
    article_id = str(sample['article_id'])
    payload = sample.get('payload')
    base = {'observed_at': sample['observed_at']}
    if sample.get('http') in (401, 403, 429):
        return {**base, 'state': 'restricted'}
    if (sample.get('http') != 200 or not isinstance(payload, dict)
            or str(payload.get('id')) != article_id
            or payload.get('link') != ORIGIN + '/' + article_id + '.html'
            or payload.get('status') != 'publish'):
        return {**base, 'state': 'unavailable'}
    content = payload.get('content')
    if not isinstance(content, dict) or content.get('protected') is not False:
        return {**base, 'state': 'restricted'}

    sequence, has_images, partial_reason = visible_blocks(content.get('rendered'), markup_limit)
    if sample.get('truncated'):
        partial_reason = 'response_truncated'
    paragraphs = [block['text'] for block in sequence if block['kind'] == 'text']
    if include_intro:
        base['introduction'] = make_intro(
            SITE, article_id, paragraphs, publisher_modified=payload.get('modified'),
            observed_at=sample['observed_at'], has_images=has_images, partial_reason=partial_reason)
    if not sequence:
        return {**base, 'state': 'restricted' if partial_reason == 'unmarked_access_boundary' else 'unavailable'}

    excerpt = payload.get('excerpt')
    excerpt_blocks = (blocks(excerpt.get('rendered')) if isinstance(excerpt, dict)
                      and excerpt.get('protected') is False else None)
    display = display_blocks(sequence, block_limit)
    return {**base, 'state': 'public_sample', 'article_url': payload['link'], 'published_at': payload.get('date'),
            'publisher_modified': payload.get('modified'), 'public_api_url': sample['source'],
            'public_api_sha256': sample['body_sha256'],
            'excerpt': ' '.join(row['text'] for row in (excerpt_blocks or []) if row['kind'] == 'text'),
            'blocks': display, 'cover_url': next((row['url'] for row in display if row['kind'] == 'image'), None),
            'cover_basis': 'first_real_body_image_not_assumed_featured_media'}


if __name__ == '__main__':
    incoming = json.load(sys.stdin)
    articles, attempts = {}, []
    for sample in incoming:
        reason = None
        try:
            result = project(sample)
        except (ValueError, etree.LxmlError):
            result = {'state': 'unavailable', 'observed_at': sample['observed_at']}
            reason = 'public_markup_budget_or_parse_limit'
        article_id = sample['article_id']
        articles[article_id] = result
        attempts.append({'article_id':article_id, 'state':result['state'], 'reason':reason,
                         'http':sample.get('http'), 'truncated':sample['truncated'],
                         'observed_at':sample['observed_at'], 'source':sample['source']})
    print(json.dumps({'schema':'wangzjk-v4-public-article-projection.v1',
                      'site_id':SITE, 'articles':articles, 'attempts':attempts}, ensure_ascii=False))
