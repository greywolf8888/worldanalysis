/* Passive observer for the current page's normal subtitle request. Never sends a request. */
(() => {
  'use strict';
  window.installWAPlatformObserver = config => {
    if (window.WAPlatformObserver?.active) throw Error('OBSERVER_ALREADY_INSTALLED');
    if (location.origin !== 'https://pan.baidu.com' || new URL(location.href).searchParams.get('path') !== config.path) throw Error('SOURCE_SCOPE_MISMATCH');
    const state = {active:true, source_file_id:config.file_id, source_revision:config.source_revision, source_path:config.path, observations:[], errors:[]};
    const oldOpen = XMLHttpRequest.prototype.open, oldFetch = window.fetch;
    const info = new WeakMap();
    const kind = value => { try { const u = new URL(String(value),location.href); return u.hostname.endsWith('.baidupcs.com') && u.pathname.includes('/netdisk-subtitle/') && u.pathname.includes(config.version_token+'_') ? 'subtitle' : null; } catch { return null; } };
    const keep = (text,status,transport) => {
      if (new URL(location.href).searchParams.get('path') !== config.path) { state.errors.push('SOURCE_CHANGED'); return; }
      const bytes = new TextEncoder().encode(text).length;
      if (bytes > 2*1024*1024) { state.errors.push('SUBTITLE_BYTE_BOUND'); return; }
      if (status !== 200) { state.errors.push('SUBTITLE_HTTP_'+status); return; }
      let format = /\d{2}:\d{2}:\d{2}[,.]\d{3}/.test(text) ? 'srt_or_vtt' : null;
      if (!format) { try { const value=JSON.parse(text); if(value && typeof value==='object')format='json'; } catch {} }
      // Public URLs or words like 'cookie' may be legitimate lesson content.
      // Reject credential-shaped headers/temporary signed tickets, not those words.
      if (!format || /(?:^|[\r\n])\s*(?:Cookie|Authorization)\s*:|[?&](?:bdstoken|sign|access_token|jstoken)=[^\s&]{8,}/i.test(text)) { state.errors.push('NOT_PLAIN_SUBTITLE'); return; }
      state.observations.push({schema:'worldanalysis.native-subtitle-response.v1',source_file_id:config.file_id,source_revision:config.source_revision,source_path:config.path,observed_at:new Date().toISOString(),http_status:status,transport,bytes,content:text,acquisition:'passive_normal_page_response',temporary_url_retained:false});
    };
    function observedOpen(method,url,...args) {
      const generation = {};
      info.set(this,generation); // Invalidate an older listener even for an unrelated reopen.
      if (kind(url)) {
        this.addEventListener('loadend',async()=>{ try {
          if(info.get(this)!==generation)return;
          const type=this.responseType, status=this.status, body=this.response;
          state.response_types=(state.response_types||[]).concat(type);
          const text=type==='arraybuffer'?new TextDecoder().decode(body):type==='blob'?await body.text():type==='json'?JSON.stringify(body):type===''||type==='text'?this.responseText:null;
          if(info.get(this)!==generation)return;
          if(typeof text==='string')keep(text,status,'xhr-'+type); else state.errors.push('UNSUPPORTED_SUBTITLE_RESPONSE_TYPE');
        } catch { state.errors.push('SUBTITLE_OBSERVER_READ_FAILED'); } },{once:true});
      }
      return oldOpen.call(this,method,url,...args);
    }
    function observedFetch(input,init) {
      const match = kind(typeof input==='string'||input instanceof URL?input:input?.url);
      const answer=oldFetch.call(this,input,init);
      if(match) answer.then(r=>r.clone().text().then(t=>keep(t,r.status,'fetch'))).catch(()=>state.errors.push('SUBTITLE_OBSERVER_READ_FAILED'));
      return answer;
    }
    XMLHttpRequest.prototype.open=observedOpen; window.fetch=observedFetch;
    state.restore=()=>{if(XMLHttpRequest.prototype.open===observedOpen)XMLHttpRequest.prototype.open=oldOpen;if(window.fetch===observedFetch)window.fetch=oldFetch;state.active=false;return{restored:true};};
    window.WAPlatformObserver=state; return {installed:true,source_file_id:config.file_id};
  };
  return {available:true};
})()
