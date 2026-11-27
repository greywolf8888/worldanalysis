import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {runInNewContext} from 'node:vm';

const script = readFileSync(new URL('../public/priority.js', import.meta.url), 'utf8');

function harness(serve) {
  class Element {
    constructor(tag) {
      this.tag = tag;
      this.children = [];
      this.attributes = {};
      this.events = {};
      this.textContent = '';
      this.parentElement = null;
      const classes = new Set();
      this.classList = {add: key => classes.add(key), remove: key => classes.delete(key),
        toggle: (key, on) => on ? classes.add(key) : classes.delete(key)};
    }
    append(...children) { for (const child of children) this.insertBefore(child, null); }
    replaceChildren(...children) { for (const child of [...this.children]) child.remove(); this.append(...children); }
    insertBefore(child, cursor) {
      if (child === cursor) return;
      child.remove();
      const index = cursor ? this.children.indexOf(cursor) : this.children.length;
      this.children.splice(index, 0, child);
      child.parentElement = this;
    }
    remove() {
      if (!this.parentElement) return;
      const siblings = this.parentElement.children;
      siblings.splice(siblings.indexOf(this), 1);
      this.parentElement = null;
    }
    contains(node) { return flatten(this).includes(node); }
    get firstElementChild() { return this.children[0] ?? null; }
    get nextElementSibling() {
      const siblings = this.parentElement?.children;
      return siblings?.[siblings.indexOf(this) + 1] ?? null;
    }
    get isConnected() { return document.body.contains(this); }
    closest(query) {
      for (let node = this; node; node = node.parentElement) if (query === '[hidden]' && node.hidden) return node;
      return null;
    }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, handler) { this.events[name] = handler; }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    click() { this.focus(); this.events.click?.({stopPropagation() {}}); }
  }

  const flatten = element => [element, ...element.children.flatMap(flatten)];
  const document = {body: new Element('body'), activeElement: null,
    getElementById: id => flatten(document.body).find(node => node.id === id),
    createElement: tag => new Element(tag)};
  const window = {};
  const calls = [];
  const fetch = async (url, options) => {
    assert.equal(url, '/api/analysis-priority');
    const update = options.body ? JSON.parse(options.body) : null;
    calls.push(update);
    const [status, value] = serve(update);
    return {ok: status === 200, status, json: async () => value};
  };
  runInNewContext(script, {document, window, fetch});
  const find = (label, tag = 'button') => flatten(document.body).find(node => node.tag === tag &&
    node.textContent === label && !node.closest('[hidden]'));
  return {document, window, calls, find};
}

const settle = () => new Promise(resolve => setImmediate(resolve));
const project = (id, title) => ({id: `rv_${id.repeat(64)}`, site_id: 'example.com', article_id: id, title});
const ranked = row => ({kind: 'article', key: `article:${row.site_id}:${row.article_id}`, resource_id: row.id,
  site_id: row.site_id, article_id: row.article_id, title: row.title, state: 'awaiting_intake'});

test('standalone calendar entry edits the draft, reuses rows, and submits only after confirmation', async () => {
  const first = ranked(project('a', '项目甲'));
  const second = ranked(project('b', '项目乙'));
  const third = project('c', '项目丙');
  let revision = 2;
  let entries = [first, second];
  let submitted = {revision, entries: entries.slice()};
  const {window, document, calls, find} = harness(update => {
    if (update) {
      assert.equal(update.revision, revision);
      revision += 1;
      if (update.action === 'add') entries.splice(update.position - 1, 0, ranked(third));
      if (update.action === 'move') {
        const old = entries.findIndex(entry => entry.key === update.key);
        entries.splice(update.position - 1, 0, ...entries.splice(old, 1));
      }
      if (update.action === 'remove') entries = entries.filter(entry => entry.key !== update.key);
      if (update.action === 'submit') submitted = {revision, entries: entries.slice()};
    }
    return [200, {revision, entries: entries.slice(), submitted}];
  });
  const api = window.worldanalysisAnalysisPriority;
  assert.equal(api.articleAction, window.analysisArticleAction);
  assert.equal(api.articleAction({...third, action_pending: true}), null);
  const trigger = api.articleAction(third, {compact: true});
  document.body.append(trigger);
  trigger.click();
  await settle();
  assert.deepEqual(calls, [null]);
  const firstRow = find('项目甲').parentElement.parentElement;
  const secondRow = find('项目乙').parentElement.parentElement;
  find('放第 2 位').click();
  await settle();
  assert.deepEqual(entries.map(row => row.title), ['项目甲', '项目丙', '项目乙']);
  assert.equal(find('项目甲').parentElement.parentElement, firstRow);

  find('项目乙').click();
  find('置顶').click();
  await settle();
  assert.equal(secondRow.parentElement.firstElementChild, secondRow);
  secondRow.children.at(-1).click();
  await settle();
  assert.deepEqual(submitted.entries.map(row => row.title), ['项目甲', '项目乙']);

  find('提交分析顺序').click();
  find('取消').click();
  assert.deepEqual(calls.filter(Boolean).map(call => call.action), ['add', 'move', 'remove']);
  find('提交分析顺序').click();
  find('确认提交').click();
  await settle();
  assert.equal(calls.at(-1).action, 'submit');
  assert.deepEqual(submitted.entries.map(row => row.title), ['项目甲', '项目丙']);
  assert.ok(find('分析顺序已提交。', 'p'));
});

test('a revision conflict rereads the draft without replaying the action', async () => {
  const row = project('d', '项目丁');
  let reads = 0;
  const {window, document, calls, find} = harness(update => {
    if (update) return [409, {}];
    return [200, {revision: reads++, entries: [], submitted: null}];
  });
  const trigger = window.analysisArticleAction(row);
  document.body.append(trigger);
  trigger.click();
  await settle();
  find('加到末尾').click();
  await settle();
  assert.deepEqual(calls.map(call => call?.action), [undefined, 'add', undefined]);
  assert.ok(find('顺序已变动。已重新读取，请核对后再操作。', 'p'));
});
