// Open a copy of this delivery in an isolated Obsidian profile and test the real reading view.
const fs = require("node:fs/promises");
const { createHash } = require("node:crypto");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { setTimeout: delay } = require("node:timers/promises");

async function prepareNote(vault, cases, folder, preview, rel) {
  const name = path.basename(folder);
  const note = path.join(preview, rel);
  const body = await fs.readFile(note, "utf8");
  const vaultNote = path.join(vault, name, rel);
  await fs.mkdir(path.dirname(vaultNote), {recursive:true});
  await fs.writeFile(vaultNote, body);
  const images = new Set();
  const localLinks = new Map();
  const addImage = file => {
    if (!file.startsWith(path.join(preview, "课件", "原图") + path.sep)) throw Error("Unexpected image " + file);
    images.add(path.basename(file));
    localLinks.set(file, path.relative(preview, file));
  };
  for (const match of body.matchAll(/!\[\[([^|\]]+\.png)(?:\|[0-9]+)?\]\]/g)) {
    if (!match[1].startsWith(name + "/课件/原图/")) throw Error("Unexpected image " + match[1]);
    addImage(path.resolve(preview, match[1].slice(name.length + 1)));
  }
  for (const match of body.matchAll(/!\[[^\]\n]+\]\(([^)\n]+)\)/g)) {
    addImage(path.resolve(path.dirname(note), decodeURIComponent(match[1])));
  }
  const urls = new Set([...body.matchAll(/\]\((\.\.\/[^)]+)\)/g)].map(match => match[1]));
  for (const url of urls) {
    const file = path.resolve(path.dirname(path.join(folder, rel)), decodeURIComponent(url));
    if (!file.startsWith(path.join(folder, "原始资料") + path.sep)) throw Error("Source escapes course " + file);
    localLinks.set(file, path.relative(folder, file));
  }
  for (const [file, relative] of localLinks) {
    const dest = path.join(vault, name, relative);
    await fs.mkdir(path.dirname(dest), {recursive:true});
    try {
      await fs.link(file, dest);
    } catch (error) {
      if (["EXDEV", "ENOTSUP", "EPERM"].includes(error.code)) await fs.copyFile(file, dest);
      else if (error.code !== "EEXIST") throw error;
    }
  }
  const provenance = [...body.matchAll(/<!-- (段落出处|配图出处) (.+?) -->/g)];
  if (!provenance.some(match => match[1] === "段落出处") ||
      provenance.filter(match => match[1] === "配图出处").length < images.size) {
    throw Error("缺少隐藏的出处记录 " + note);
  }
  cases.set(name + "/" + rel, {name, images:[...images], urls:urls.size,
    provenance:provenance.map(match => match[2].replace(/\s+/g, " ")),
    digest:createHash("sha256").update(body).digest("hex"),
    appendices:[...body.matchAll(/^> \[!info\]- (原课与配套资料|有声原课与工具入口)$/gm)].map(match => match[1]),
    formula:/\$\$[\s\S]+?\$\$/.test(body),code:/^(?:\x60{3}|~{3})\w+/m.test(body),table:/^\|.*\|\s*$/m.test(body)});
}

async function configureVault(vault) {
  await fs.mkdir(path.join(vault,".obsidian"),{recursive:true});
  await fs.writeFile(path.join(vault,".obsidian/app.json"),JSON.stringify({defaultViewMode:"preview",readableLineLength:true}));
}

async function until(fn, ms=30000) {
  const end = Date.now()+ms;
  let last;
  do {
    try { const value = await fn(); if (value) return value; } catch (e) { last=e; }
    await delay(250);
  } while (Date.now()<end);
  throw new Error("Timed out "+(last ? last.message : ""));
}
async function connect(url) {
  const ws = new WebSocket(url);
  await new Promise((ok,fail)=>{ws.addEventListener("open",ok,{once:true}); ws.addEventListener("error",fail,{once:true});});
  const pending=new Map();
  let seq=0;
  ws.addEventListener("message",({data})=>{
    const m=JSON.parse(data), p=pending.get(m.id);
    if(p){pending.delete(m.id); if(m.error)p.fail(new Error(m.error.message)); else p.ok(m.result);}
  });
  return {
    ws,
    async call(method,params={},timeout=14000){
      const id=++seq;
      return new Promise((ok,fail)=>{
        const timer=setTimeout(()=>{pending.delete(id); fail(new Error("No reply: "+method));},timeout);
        pending.set(id,{ok(v){clearTimeout(timer);ok(v);},fail(e){clearTimeout(timer);fail(e);}});
        ws.send(JSON.stringify({id,method,params}));
      });
    }
  };
}
async function evaluate(client, expression, timeout) {
  const ret=await client.call("Runtime.evaluate",{expression,awaitPromise:true,returnByValue:true},timeout);
  if(ret.exceptionDetails) throw new Error(ret.exceptionDetails.exception?.description || ret.exceptionDetails.text);
  return ret.result.value;
}
async function main(){
  if (process.argv[2] !== "--project" || process.argv.length !== 5) {
    throw Error("用法 --project 课程包 阅读预览目录");
  }
  const preview=path.resolve(process.argv[4]);
  const working=path.dirname(preview);
  const qa=await fs.mkdtemp(path.join(working,"obsidian-render-"));
  const vault=path.join(qa,"搬移后的学习库"),roam=path.join(qa,"roaming"),local=path.join(qa,"local");
  const profile=path.join(roam,"obsidian");
  const cases=new Map();
  await prepareNote(vault, cases, path.resolve(process.argv[3]), preview, "课件/00 开始学习.md");
  await configureVault(vault);
  for(const d of [profile,local]) await fs.mkdir(d,{recursive:true});
  await fs.writeFile(path.join(profile,"obsidian.json"),JSON.stringify({vaults:{a73c12e5f03b94d6:{path:vault,ts:Date.now(),open:true}},updateDisabled:true}));
  const exeCandidates = process.env.OBSIDIAN_EXE ? [process.env.OBSIDIAN_EXE] :
    [process.env.LOCALAPPDATA, process.env.ProgramFiles].filter(Boolean)
      .map(folder => path.join(folder, "Obsidian", "Obsidian.exe"));
  let exe;
  for (const candidate of exeCandidates) {
    if (await fs.stat(candidate).then(value => value.isFile(), () => false)) {
      exe = candidate;
      break;
    }
  }
  if (!exe) throw Error("找不到 Obsidian，请用 --obsidian-exe 或 OBSIDIAN_EXE 指定位置");
  const app=spawn(exe,["--user-data-dir="+profile,"--remote-debugging-port=0","--remote-debugging-address=127.0.0.1",
    "--disable-background-networking","--disable-component-update","--no-first-run","--disable-gpu",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost"],{cwd:qa,windowsHide:true,
    env:{...process.env,APPDATA:roam,LOCALAPPDATA:local,TEMP:qa,TMP:qa},stdio:"ignore"});
  let browser, client, passed=false;
  try {
    const port=await until(async()=>Number((await fs.readFile(path.join(profile,"DevToolsActivePort"),"utf8")).split(/\r?\n/)[0]),20000);
    const url="http://127.0.0.1:"+port;
    const ver=await (await fetch(url+"/json/version")).json();
    browser=await connect(ver.webSocketDebuggerUrl);
    const page=await until(async()=>(await (await fetch(url+"/json/list")).json()).find(t=>t.type==="page" && t.url.includes("index.html")),25000);
    client=await connect(page.webSocketDebuggerUrl);
    const vaultPath=await until(()=>evaluate(client,"globalThis.app?.vault?.adapter?.getBasePath?.()"),25000);
    if(path.resolve(vaultPath)!==path.resolve(vault)) throw new Error("Unexpected test profile");
    await until(async()=>{
      const state=await evaluate(client,`(()=>{
        const buttons=[...document.querySelectorAll(".modal button")];
        const allowed=buttons.find(b=>/restricted mode|限制模式|受限模式/i.test(b.textContent) && !/turn off|disable|关闭/i.test(b.textContent));
        if(allowed)allowed.click();
        return {ready:app.workspace.layoutReady && !!app.workspace.rootSplit,
          dialog:document.querySelector(".modal")?.innerText,buttons:buttons.map(b=>b.textContent)};
      })()`);
      if(!state.ready) await fs.writeFile(path.join(qa,"startup-state.json"),JSON.stringify(state));
      return state.ready;
    },22000);
    const files=[...cases.keys()];
    await until(()=>evaluate(client,JSON.stringify(files)+".every(p=>{const f=app.vault.getAbstractFileByPath(p);return f&&app.metadataCache.getFileCache(f)})"));
    const checks=[];
    for(const file of files){
      const info=await evaluate(client,`(async()=>{
        const target=app.vault.getAbstractFileByPath(${JSON.stringify(file)});
        const leaf=app.workspace.activeLeaf || app.workspace.getMostRecentLeaf() || app.workspace.getLeaf(false);
        await leaf.openFile(target,{state:{mode:"preview"}});
        const state=leaf.getViewState(); state.state.mode="preview"; await leaf.setViewState(state);
        const wait=(n)=>new Promise(r=>setTimeout(r,n));
        const rendered=()=>leaf.view.containerEl.querySelector(".markdown-reading-view .markdown-preview-view");
        let view;
        for(let attempt=0;attempt<100;attempt++){
          view=rendered();
          if(view && view.querySelector("h1"))break;
          if(view)view.scrollTop=0;
          await wait(100);
        }
        if(!view?.querySelector("h1"))throw Error("reading view did not render: "+target.path);
        const seen=new Map(), urls=new Set(), closedAppendices=new Set(), visibleCitations=new Set();
        const appendices=${JSON.stringify(cases?.get(file).appendices || [])};
        const provenance=${JSON.stringify(cases?.get(file).provenance || [])};
        const collectLinks=()=>{
          for(const a of view.querySelectorAll("a[href]")){
            const href=a.getAttribute("href");
            if(href.startsWith("../") || decodeURIComponent(href).includes("原始资料/")) urls.add(href);
          }
        };
        let expanded=0, title="", formula=false, code=false, table=false, reachedBottom=false, horizontalOverflow=false;
        for(let pos=0,step=0;step<700;step++){
          view.scrollTop=pos; await wait(85);
          for(const node of view.querySelectorAll(".callout.is-collapsed .callout-title")){
            const text=node.querySelector(".callout-title-inner")?.textContent.trim();
            if(appendices.includes(text)) closedAppendices.add(text);
            else {node.click(); expanded++;}
          }
          await wait(65);
          for(const i of view.querySelectorAll("img")){
            const src=i.getAttribute("src")||"";
            if(i.complete && i.naturalWidth>0)seen.set(src,{width:i.naturalWidth,height:i.naturalHeight,fit:i.getBoundingClientRect().width<=view.clientWidth});
          }
          title=title||view.querySelector("h1")?.textContent||"";
          horizontalOverflow=horizontalOverflow||view.scrollWidth>view.clientWidth+2;
          formula=formula||!!view.querySelector("mjx-container,.math-block,.MathJax");
          code=code||!!view.querySelector("pre code");
          table=table||!!view.querySelector("table");
          collectLinks();
          if(provenance.length){
            const visibleText=view.innerText.replace(/\\s+/g," ")
              .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
            for(const citation of provenance){
              if(visibleText.includes(citation)) visibleCitations.add(citation);
            }
            for(const label of view.querySelectorAll("em,strong")){
              if(label.innerText.trim()==="对照" && label.parentElement.innerText.trim()==="对照"){
                visibleCitations.add("对照");
              }
            }
          }
          if(view.scrollTop+view.clientHeight>=view.scrollHeight-2){reachedBottom=true;break;}
          pos=view.scrollTop+Math.max(120,view.clientHeight*0.68);
        }
        await wait(175);
        const box=view.getBoundingClientRect();
        const overflowElements=[...view.querySelectorAll("*")].filter(el=>{
          const rect=el.getBoundingClientRect();
          return rect.width>0 && rect.right>box.right+2;
        }).slice(-8).map(el=>({tag:el.tagName,cls:el.className,width:Math.round(el.getBoundingClientRect().width)}));
        const links=app.metadataCache.getFileCache(target)?.links||[];
        const internal=links.map(l=>{
          const [file,mark]=l.link.split("#",2);
          const dest=file ? app.metadataCache.getFirstLinkpathDest(file,target.path) : target;
          const cache=dest && app.metadataCache.getFileCache(dest);
          const found=!!dest && (!mark || (mark.startsWith("^") ? !!cache?.blocks?.[mark.slice(1)] :
            !!cache?.headings?.some(h=>h.heading===mark)));
          return {link:l.link,found};
        });
        return {file:target.path,mode:leaf.view.getMode(),title,expanded,reachedBottom,closedAppendices:[...closedAppendices],
          images:[...seen].map(([s,v])=>({name:decodeURIComponent(s.split("?")[0].split("/").pop()),...v})),
          internal, formula, code, table, rawLinks:urls.size,visibleCitations:[...visibleCitations],
          horizontalOverflow:horizontalOverflow||view.scrollWidth>view.clientWidth+2,
          clientWidth:view.clientWidth, scrollWidth:view.scrollWidth,overflowElements};
      })()`,120000);
      info.document_sha256=cases.get(file).digest;
      checks.push(info);
    }
    const result={version:ver.Browser,testVault:vault,checks};
    const unreadable=checks.filter(v=>v.mode!=="preview" || !v.title || v.horizontalOverflow ||
      v.internal.some(l=>!l.found) || v.images.some(i=>!i.fit) || (()=>{
        const want=cases.get(v.file);
        return want.images.some(f=>!v.images.some(i=>i.name===f)) || v.rawLinks<want.urls ||
          want.appendices.some(title=>!v.closedAppendices.includes(title)) ||
          (!v.reachedBottom||!!v.expanded||v.visibleCitations.length>0) ||
          (want.formula&&!v.formula) || (want.code&&!v.code) || (want.table&&!v.table);
      })());
    if(unreadable.length){
      await fs.writeFile(path.join(qa,"incomplete-render.json"),JSON.stringify(result,null,2));
      throw Error("Incomplete reading-view checks "+JSON.stringify(unreadable.map(v=>({
        file:v.file,links:v.internal.filter(x=>!x.found),images:v.images.map(x=>[x.name,x.fit]),raw:v.rawLinks,
        formula:v.formula,code:v.code,table:v.table,visibleCitations:v.visibleCitations,
        overflow:v.horizontalOverflow,extended:v.overflowElements})))+" "+qa);
    }
    await fs.writeFile(path.join(preview, "obsidian-render.json"), JSON.stringify({version:ver.Browser,checks},null,2)+"\n");
    passed=true;
    console.log("Rendered",checks.length,"notes and",checks.reduce((a,v)=>a+v.images.length,0),"images");
  }finally{
    if(browser){try{await browser.call("Browser.close");}catch{} browser.ws.close();}
    if(client)client.ws.close();
    if(app.exitCode===null){await delay(1200); if(app.exitCode===null) app.kill();}
    if(passed && path.dirname(path.resolve(qa)) === path.resolve(working)) {
      await fs.rm(qa,{recursive:true,force:true,maxRetries:3}).catch(() => {});
    }
  }
}
main().catch(e=>{console.error(e.message);process.exitCode=1;});
