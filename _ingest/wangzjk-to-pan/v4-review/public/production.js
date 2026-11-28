'use strict';
const progressLabels={
 analysis:{queued:'待分析',in_progress:'分析中',awaiting_originals:'待原件',same_source_reuse:'同源复用',archive_only:'留档',completed:'已分析',completed_received_scope:'已分析实收资料',awaiting_re_review:'待复验'},
 acceptance:{historical_same_edition:'已验收',scope_accepted:'实收资料已验收',same_source_reuse:'同源复用',unrecorded:'待验收'},
 textbook:{registered_edition_matches:'可阅读',registered_independent_derivative_matches:'可独立阅读'},
 preservation:{installed_verified:'已独立保存'},
 cleanup:{candidate_present:'指定副本待清理',candidate_identity_unverified:'待核对副本',candidate_missing_unverified:'副本待核实',cleanup_pending_reconciliation:'清理结果待核实',removed:'指定副本已清理'},
 reopened:{verified:'已确认可读',awaiting_reopen:'待检查'},
 receipt:{'已验收':'已验收','已验收（配套待补）':'配套待补','已分析（核心待补）':'核心待补','同源复用':'同源复用','待补原件':'待原件','待处理':'待处理','留档':'留档','处理中':'分析中'}
};
function factTable(entries){
 const dl=el('dl',null,'fact-list');
 for(const [name,value] of entries){
  if(value==null||value==='')continue;
  const row=el('div',null,'fact-row');
  row.append(el('dt',name),el('dd',typeof value==='number'?fmt(value):value));
  dl.append(row);
 }
 return dl;
}
function progressSection(title){
 const section=el('section',null,'progress-section');
 section.append(el('h3',title));
 return section;
}
const collectionClock=new Intl.DateTimeFormat('zh-CN',{
 timeZone:'Asia/Singapore',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'
});
function collectionStatus(discovery){
 const section=progressSection('资料采集');
 const run=discovery.public_checkpoint;
 if(run){
  const timestamp=Date.parse(run.observed_at);
  const time=Number.isFinite(timestamp)?collectionClock.format(timestamp):'';
  const failed=Boolean(run.stop_reason);
  section.append(factTable([
   [failed?'最近尝试':'最近采集',time],['累计新增收录',run.new_articles],
   ['公开目录已核对',run.collector==='codex'&&Number.isInteger(run.history_observed)&&Number.isInteger(run.publisher_total)?fmt(run.history_observed)+' / '+fmt(run.publisher_total)+' 项':null],
   ['本次补采图文',run.collector==='codex'?run.details_added:null],
   ['待补图文',!failed&&Number.isInteger(run.day_count)?run.pending_detail_count:null]
  ]));
  if(failed){
   const state=run.stop_reason==='official_publisher_gate'?'来源访问受限。':'本次采集未完成。';
   section.append(el('p',state,'status-note'));
  }
 }
 if(discovery.public_checkpoint_pending_sync||!run)section.append(el('p','采集记录待更新。','status-note'));
 return section;
}
function textbook(p,label='在 Obsidian 打开教材'){
 if(!p||typeof p.obsidian_href!=='string')return null;
 try{
  const u=new URL(p.obsidian_href),path=u.searchParams.get('path')??'';
  if(u.protocol!=='obsidian:'||u.hostname!=='open'||u.pathname!==''||u.hash||[...u.searchParams.keys()].join()!=='path'||!/^G:\/(?:[\s\S]+)\.md$/i.test(path)||path.includes('..'))return null;
  const a=el('a',label,'detail-link');a.href=u.href;a.rel='noreferrer noopener';return a;
 }catch{return null;}
}
const completionLabels = {
 originals: ['原始资料', {core_missing:'核心资料待补',support_missing:'配套资料待补',documented_scope_received:'已核明的资料已收到',awaiting_originals:'待原件'}],
 received_analysis: ['实收内容', {accepted_for_edition:'本版实收内容已核完',queued:'待分析',in_progress:'分析中',awaiting_acceptance:'待验收',received_coverage_missing:'已收资料有遗漏',received_unreadable:'有已收资料无法读取',received_unresolved:'实收内容待核对',received_reported_missing:'请核对已收到的资料'}],
 textbook: ['教材', {received_scope_usable:'可学习已收部分',core_usable:'核心内容可学习',documented_scope_usable:'已核范围可完整学习',historical_readable:'已有同版教材',legacy_entry_pending:'旧版入口，待复核',companions_unavailable:'配套资料无法打开'}],
 cleanup: ['本地原件', {retain_for_core_gap:'有核心资料待补，继续保留',one_copy_needs_authorization_check:'一个副本待确认清理',one_copy_removed:'一个副本已清理'}]
};
function courseCompletionRows(record){
 const facts = record?.course_completion || {};
 return Object.entries(completionLabels).flatMap(([key,[name,labels]]) => {
  const label = labels[facts[key]];
  return typeof label === 'string' ? [[name,label]] : [];
 });
}
function projectState(p,showTitle=true){
 const section=el('div',null,'project-facts');
 if(showTitle&&p.title)section.append(el('h4',p.title,'project-title'));
 const isReuse=p.analysis_state==='same_source_reuse';
 const download=p.download_complete===true?'已下载完整':p.download_complete===false?'尚未下载完整':'';
 const category=typeof p.category==='string'?p.category.replace(/^\d+_/,''):'';
 const scope=['核心待补','配套待补'].includes(p.analysis_scope)?p.analysis_scope:'';
 const acceptance=p.acceptance_state==='unrecorded'&&p.analysis_state!=='in_progress'?'':progressLabels.acceptance[p.acceptance_state];
 const completion=courseCompletionRows(p);
 const productionDay=shortDate(p.production_date?.date||p.production_date);
 const productionQualifier={estimated:'（约，已知最早）',known_earliest:'（已知最早）'}[p.production_date?.certainty]||'';
 section.append(factTable([
  ['分类',category],['本地下载',download],...completion,
  ...(!completion.length?[
   ['分析',progressLabels.analysis[p.analysis_state]],['实收范围',scope],['验收',acceptance],
   ['教材',isReuse?'复用同源教材':progressLabels.textbook[p.textbook_state]]
  ]:[]),
  ['待补资料',p.scope_gaps>0?fmt(p.scope_gaps)+' 项':''],['教材保存',progressLabels.preservation[p.preservation_state]],
  ...(!p.course_completion?.cleanup?[
   ['本地清理',progressLabels.cleanup[p.local_cleanup_state]],['清理后阅读',progressLabels.reopened[p.local_cleanup_reader_state]]
  ]:[]),
  ['分析日期',shortDate(p.analysis_date?.date||p.analysis_date)],['制作日期',productionDay?productionDay+productionQualifier:''],
  ['历史阅读日期',shortDate(p.historical_readback_date)]
 ]));
 const actions=el('div',null,'project-actions');
 const link=textbook(p);if(link)actions.append(link);
 const priority=typeof analysisLocalAction==='function'?analysisLocalAction(p):null;
 if(priority)actions.append(priority);
 if(actions.children.length)section.append(actions);
 return section;
}
function currentProject(summary){
 const section=progressSection('当前项目');
 if(summary.current_production)section.append(projectState(summary.current_production));
 else section.append(el('p',summary.sync_state==='live'?'暂无已核对的在办项目。':'项目进度待更新。','status-note'));
 return section;
}
function localProjects(summary,projects){
 const section=progressSection('本地项目');
 section.append(factTable([['已登记项目',summary.counts.registered_projects],['独立教材',summary.counts.own_textbook_entries]]));
 if(projects.count>0){
  const grouped=el('details',null,'local-projects'),list=el('div',null,'local-project-list');
  grouped.append(el('summary','浏览本地项目'),list);section.append(grouped);
  localPage(list,projects);
 }
 return section;
}
function preservationStatus(preservation,cleanup){
 const section=progressSection('保存与清理');
 if(preservation){
  const local=el('div',null,'cleanup-group');
  local.append(el('h4','本地'),factTable([
   ['独立保存的教材',preservation.installed_verified],
   ['待清理副本',preservation.cleanup_states?(preservation.cleanup_states.candidate_present??0):null],
   ['已清理副本',preservation.cleanup_states?(preservation.cleanup_states.removed??0):null],
   ['清理后已确认可读',preservation.verified_post_cleanup_reopens]
  ]));
  section.append(local);
 }
 const cloud=el('div',null,'cleanup-group');
 cloud.append(el('h4','网盘'),factTable([
  ['曾回收的副本',cleanup.historic_exact_recycled],['可确认清理的副本',cleanup.new_delete_candidates]
 ]));
 section.append(cloud);
 return section;
}
let productionSeq=0;
async function localPanel(){
 $('local-title').textContent='处理进度';
 const k=++productionSeq,box=$('local-body');box.replaceChildren(el('p','加载中…','status-note'));openDialog('local-status');
 try{
  const [summary,cleanup,discovery,projects]=await Promise.all(['/api/local-status','/api/cleanup-readiness','/api/discovery-readiness','/api/local-projects?page=1'].map(path=>api(path)));
  if(k!==productionSeq||!$('local-status').open)return;
  box.replaceChildren(
   collectionStatus(discovery),currentProject(summary),localProjects(summary,projects),
   preservationStatus(summary.local_preservation,cleanup)
  );
  if(summary.sync_state==='partial')box.prepend(el('p','部分本地进度待更新。','status-note'));
  if(summary.sync_state==='pending_sync')box.prepend(el('p','本地进度待同步。','status-note'));
 }catch{if(k===productionSeq&&$('local-status').open)box.replaceChildren(el('p','未能加载处理进度，请重试。','status-note notice'));}
}
async function textbookPanel(){
 const k=++productionSeq,box=$('local-body');
 $('local-title').textContent='我的教材';
 box.replaceChildren(el('p','加载中…','status-note'));openDialog('local-status');
 try{
  const data=await api('/api/textbooks?page=1');
  if(k!==productionSeq||!$('local-status').open)return;
  box.replaceChildren();
  if(data.sync_state==='pending_sync'){box.append(el('p','教材暂时无法读取，请稍后重试。','status-note'));return;}
  const overviews=el('div',null,'project-actions');
  for(const overview of data.overviews){
   const label=data.overviews.length===1?'在 Obsidian 打开总览':overview.title.replace(/^\d+_/,'')+'总览';
   const link=textbook(overview,label);
   if(link)overviews.append(link);
  }
  if(overviews.children.length)box.append(overviews);
  if(data.count){const list=el('div',null,'local-project-list');box.append(list);localPage(list,data,'/api/textbooks');}
  else box.append(el('p','暂无可打开的教材。','status-note'));
 }catch{if(k===productionSeq&&$('local-status').open)box.replaceChildren(el('p','未能加载教材，请重试。','status-note notice'));}
}
function localPage(area,page,endpoint='/api/local-projects'){
 const body=el('div',null),controls=el('div',null,'actions local-project-controls');
 let request=0;
 function draw(p,i){
  body.replaceChildren(...p.items.map(x=>{
   const detail=el('details',null,'local-project'),state=progressLabels.receipt[x.receipt_status]||progressLabels.analysis[x.analysis_state];
   const heading=el('summary',null);
   heading.append(el('span',x.title||'本地项目','local-project-title'));
   if(state)heading.append(el('span',state,'local-project-state'));
   detail.append(heading,projectState(x,false));return detail;
  }));
  const prev=el('button','上一页'),next=el('button','下一页');
  prev.disabled=i<=1;next.disabled=i*16>=p.count;
  async function load(j){
   const k=++request;
   try{const result=await api(endpoint+'?page='+j);if(k===request&&area.isConnected&&$('local-status').open)draw(result,j);}
   catch{if(k===request&&area.isConnected&&!body.querySelector('.notice'))body.prepend(el('p','未能加载这一页，请重试。','status-note notice'));}
  }
  prev.addEventListener('click',()=>void load(i-1));next.addEventListener('click',()=>void load(i+1));
  controls.replaceChildren(prev,el('span','第 '+i+' 页','muted'),next);
 }
 area.append(body,controls);draw(page,1);
}
let reviewSeq=0;
async function preconfirmPanel(){
 const k=++reviewSeq,box=$('preconfirm-body');box.replaceChildren(el('p','加载中…','status-note'));openDialog('preconfirmation');
 try{
  const status=await api('/api/preconfirmation');
  if(k!==reviewSeq||!$('preconfirmation').open)return;
  box.replaceChildren();
  if(!status.ordered_items.length){box.append(el('p','先选择要转存的资料。','status-note'));return;}

  const order=progressSection('转存顺序'),items=el('ol',null,'queued-review');
  for(const p of status.ordered_items){
   const item=el('li',null);
   item.append(el('span',p.title||'待核对的资料'));
   const reason=p.reasons?.includes('STALE_VERSION')?'资料已变更，请重新选择':
    p.reasons?.includes('SOURCE_SCOPE_AND_TOTAL_BYTES_UNVERIFIED')?'资料范围待核实':'';
   if(reason)item.append(el('span',reason,'queued-review-note'));
   items.append(item);
  }
  order.append(items);

  const checks=progressSection('转存条件');
  checks.append(factTable([
   ['网盘可用空间',showSize(status.actual_remaining_bytes)||'待核实'],['至少保留',showSize(status.minimum_free_bytes)]
  ]));
  if(status.platform_hold_present)checks.append(el('p','网盘转存已暂停。','status-note'));
  if(!status.execution_allowed)checks.append(el('p','核对完成后，由你确认转存。','status-note'));
  const confirm=el('button','确认转存','primary');confirm.disabled=true;checks.append(confirm);
  box.append(order,checks);
 }catch{if(k===reviewSeq&&$('preconfirmation').open)box.replaceChildren(el('p','未能加载转存信息，请重试。','status-note notice'));}
}
$('local-open').addEventListener('click',()=>void localPanel());$('local-close').addEventListener('click',()=>closeDialog($('local-status')));
$('local-library-open').addEventListener('click',()=>void textbookPanel());
$('preconfirm-open').addEventListener('click',()=>void preconfirmPanel());$('preconfirm-close').addEventListener('click',()=>closeDialog($('preconfirmation')));
