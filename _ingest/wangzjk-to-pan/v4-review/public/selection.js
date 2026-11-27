'use strict';

window.worldanalysisSelection = (() => {
  let draft = null;
  let picked = new Set();
  let saving = false;
  let readRequest = null;
  const subscribers = new Set();
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('worldanalysis-selection') : null;

  function emit() {
    for (const notify of subscribers) notify(draft, saving);
  }

  function adopt(value) {
    if (value && Number.isSafeInteger(value.revision) && Array.isArray(value.ids)
        && (!draft || value.revision > draft.revision)) {
      draft = {revision:value.revision, ids:value.ids, prefs:value.prefs};
      picked = new Set(value.ids);
      emit();
    }
    return draft;
  }

  async function request(body) {
    const options = {cache:'no-store', credentials:'omit'};
    if (body) Object.assign(options, {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
    });
    const response = await fetch('/api/draft' + (body ? '' : '?view=selection'), options);
    const result = await response.json();
    if (response.ok) return result;
    adopt(result.draft);
    const error = new Error(response.status === 409 ? '选择已更新，请重新核对。' :
      response.status === 422 ? '该资料暂不能加入转存清单。' : '选择未保存，请重试。');
    error.status = response.status;
    throw error;
  }

  function load() {
    if (!readRequest) {
      readRequest = request().then(adopt).finally(() => { readRequest = null; });
    }
    return readRequest;
  }

  async function mutate(action, values = {}, revision = draft?.revision) {
    if (!draft || saving) throw new Error('选择正在更新，请稍后再试。');
    if (revision !== draft.revision) {
      const error = new Error('选择已更新，请重新核对。');
      error.status = 409;
      throw error;
    }
    saving = true;
    emit();
    try {
      const saved = await request({action, ...values, compact:true, revision});
      adopt(saved);
      channel?.postMessage(saved.revision);
      return draft;
    } finally {
      saving = false;
      emit();
    }
  }

  channel?.addEventListener('message', event => {
    if (Number.isSafeInteger(event.data) && event.data > (draft?.revision ?? -1)) {
      void load().catch(() => {});
    }
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) void load().catch(() => {});
  });

  return {
    load, adopt, mutate,
    subscribe(notify) {
      subscribers.add(notify);
      notify(draft, saving);
      return () => subscribers.delete(notify);
    },
    isPicked(row) {
      return typeof row === 'string' ? picked.has(row) : !row.action_pending && picked.has(row.id);
    },
    toggle(row, selected, snapshot) {
      const values = {id:row.id, selected};
      if (selected) {
        if (row.article_key) values.article_key = row.article_key;
        if (snapshot) values.snapshot_id = snapshot;
      }
      return mutate('toggle', values);
    },
    removeMany(ids) {
      const chosen = Array.from(new Set(ids)).filter(id => picked.has(id));
      return chosen.length ? mutate('remove_many', {ids:chosen}) : Promise.resolve(draft);
    },
    clear(revision) {
      return mutate('clear', {}, revision);
    }
  };
})();
