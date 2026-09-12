/* Design layer. Original scoring, wallet and trading services remain unchanged. */
const names={overview:'Live',learning:'Learning',scans:'Token scans',treasury:'Treasury'};
const descriptions={scans:'Inspect each assessment, its evidence, and what happened afterward.',treasury:'Actual balances, operating reserves, and the money moving through the worm.',learning:'An open record of the calls, the mistakes, and the evidence behind each rule.',activity:'Follow the worm as it reads the chain and records its findings.'};
const wrap=document.querySelector('.wrap'),intro=document.createElement('div');intro.className='view-intro';wrap.prepend(intro);
wrap.append(document.querySelector('#disc'));
wrap.append(document.querySelector('#synapse'));
const specimenLeft=document.createElement('div');specimenLeft.className='specimen-label';specimenLeft.innerHTML='SPECIMEN / IRL WORM<br><strong>BINARY CHAIN SCOUT</strong>';document.querySelector('.mast').append(specimenLeft);
const specimenRight=document.createElement('div');specimenRight.className='specimen-label right';specimenRight.id='specimen-block';specimenRight.innerHTML='ROBINHOOD CHAIN<br>WAITING FOR BLOCK';document.querySelector('.mast').append(specimenRight);
const guide=document.createElement('a');guide.href='/docs';guide.textContent='Docs ↗';guide.className='docs-nav';document.querySelector('.rail nav').append(guide);

function navigate(){if(location.hash==='#activity')history.replaceState(null,'','#overview');const view=location.hash.slice(1);const v=names[view]?view:'overview';document.body.dataset.view=v;document.querySelector('#view-title').textContent=names[v];document.querySelectorAll('.rail nav a').forEach(a=>{a.classList.toggle('active',a.dataset.view===v);if(a.dataset.view===v)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')});intro.hidden=v==='overview';intro.innerHTML=v==='overview'?'':`<h2>${names[v]}</h2><p>${descriptions[v]}</p>`;if(v==='overview')sizeWorm();if(v==='activity')drawVision();window.scrollTo({top:0,behavior:'instant'})}
window.addEventListener('hashchange',navigate);navigate();
window.motionPaused=matchMedia('(prefers-reduced-motion: reduce)').matches;const motion=document.querySelector('#motion');function motionSync(){document.body.classList.toggle('motion-off',window.motionPaused);motion.textContent=window.motionPaused?'Resume animation':'Pause animation';motion.setAttribute('aria-pressed',String(window.motionPaused))}motion.onclick=()=>{window.motionPaused=!window.motionPaused;motionSync()};motionSync();
// Keep the animation control available on touch devices, too.
const mobileMotion=motion.cloneNode(true);mobileMotion.id='mobile-motion';mobileMotion.textContent='Motion';mobileMotion.setAttribute('aria-label','Pause or resume animation');mobileMotion.onclick=()=>{motion.click();mobileMotion.setAttribute('aria-pressed',String(window.motionPaused))};document.querySelector('.topline>div').append(mobileMotion);
// The dig indicator of the old status bar lives in the topline now, so every view shows when the worm is inside a token.
const digBadge=document.createElement('span');digBadge.id='dig-badge';digBadge.hidden=true;document.querySelector('.topline>div').prepend(digBadge);
setInterval(()=>{const src=document.querySelector('#sb-dig');digBadge.hidden=src.hidden;digBadge.textContent=src.textContent},1000);
const ms=document.createElement('style');ms.textContent='#mobile-motion{border:1px solid var(--line);background:transparent;color:var(--dim);border-radius:4px;padding:5px 8px;font-size:12px;display:none}@media(max-width:950px){#mobile-motion{display:block}}@media(max-width:420px){.topline>span{display:none}.topline>div{width:100%;justify-content:space-between}}';document.head.append(ms);
window.updateFreshness=()=>{const el=document.querySelector('#freshness');const age=window.lastReceived?(Date.now()-window.lastReceived)/1000:Infinity;const chainStalled=window.blockAdvancedAt&&Date.now()-window.blockAdvancedAt>180000;const stale=window.loadFailed||age>45||chainStalled;el.classList.toggle('error',stale);el.textContent=age===Infinity?'Connecting to data…':chainStalled?'Chain progress delayed':stale?`Updates delayed · ${Math.floor(age)}s ago`:`Updated ${Math.floor(age)}s ago`;};setInterval(window.updateFreshness,1000);
for(const [id,key] of [['token-search','scanQuery'],['verdict-filter','scanFilter'],['scan-sort','scanSort']]){document.getElementById(id).addEventListener(id==='token-search'?'input':'change',e=>{window[key]=e.target.value;renderFeed(lastFeed,true)})}
function presentMoney(v){return v==null?'—':Number(v).toLocaleString('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2})}
function enhance(s){if(s.stats?.last_block!=null&&s.stats.last_block!==window.observedBlock){window.observedBlock=s.stats.last_block;window.blockAdvancedAt=Date.now()}const st=s.stats||{},rw=s.runway||{},rd=s.readiness||{},sc=s.scout||{},ch=s.character||{};document.querySelector('#specimen-block').innerHTML='ROBINHOOD CHAIN<br><strong>BLOCK '+esc(st.last_block??'—')+'</strong>';document.querySelector('#mode-label').textContent=s.live?'Live execution':'Demo · no transactions';const metrics=[['Tokens screened',num(st.scored),'Across the indexed window'],['Warnings confirmed',sc.called??'—',`${sc.checked_warnings??0} assessed warnings checked`],['Actual treasury',presentMoney(ch.usd_real??rw.treasury_usd),'Excludes demo funds'],['Trading readiness',`${rd.score??0}<em> / 100</em>`,(s.trader&&s.trader.enabled===false)?`Trading off by policy · threshold ${rd.ready_at??80}`:rd.ready?'Evidence threshold met':`Evidence threshold: ${rd.ready_at??80}`]];document.querySelector('#overview-metrics').innerHTML=metrics.map(([l,v,d])=>`<div class="metric"><span>${l}</span><strong>${v}</strong><small>${d}</small></div>`).join('');document.querySelector('#grow').textContent=ch.demo?'Character growth is previewing demo funds. Actual funds appear below.':'The worm grows with the treasury it holds.';
if(ch.demo){const stage=document.querySelector('#stage');stage.innerHTML=`<span class="demo">DEMO</span> stage ${(ch.stage||0)+1}/${num(ch.stages||7)} · ${esc(ch.stage_name||'hatchling')} · character balance ${presentMoney(ch.usd)} <span class="meta">(simulated)</span>${ch.next_usd?` · next size at ${presentMoney(ch.next_usd)}`:' · fully grown'}`}
const titles={feed:'Recent token scans',wallet:'Wallet & fee income',trades:'Trading activity',runway:'Operating runway',brain:'Rule performance',lab:'Strategy comparisons',paper:'Paper portfolio',bad:'Creator signals',causes:'Giving',voice:'Field notes'};for(const [k,v] of Object.entries(titles)){const el=document.querySelector(`[data-panel="${k}"] .ttl`);if(el)el.textContent=v}
// Make missing values distinct from measured zero where source data is available.
const r=document.querySelector('#ready');if(r){r.setAttribute('aria-label',`Trading readiness ${rd.score??0} out of 100`)}
}
const originalRender=render;render=function(s){originalRender(s);enhance(s)};
// The first price the feed returned for this verdict is kept as the at-scan value. It is missing until the feed
// lists the token, and for verdicts scored before that value was kept it was first read well after the scan.
function fdvLag(t){const m=t.metrics||{};if(!(m.price0_ts&&t.scored_at))return '';const lag=m.price0_ts-t.scored_at;return lag>900?` · first price read ${lag>=7200?Math.round(lag/3600)+' h':Math.round(lag/60)+' min'} after the scan`:''}
function fdvLabel(t){const m=t.metrics||{};return m.fdv0_usd==null?'FDV not read yet':fdvLag(t)?'FDV first read':'FDV at scan'}
card=function(t,isNew){const m=t.metrics||{},cls=vcls(t.verdict),open=expanded.has(t.token),badOutcome=/rugged|dumped/.test(t.outcome||'');const verdict=esc(t.verdict||'Pending');const outcome=t.outcome&&t.outcome!=='pending'?`${esc(t.outcome)}${t.change_pct!=null?' · '+pct(t.change_pct):''}`:t.change_pct!=null?pct(t.change_pct):'Awaiting outcome';const image=logo(t.logo);const l=image?`<img class="logo" src="${esc(image)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`:'';const detailsId='detail-'+String(t.token).replace(/[^a-zA-Z0-9]/g,'');
let html=`<article class="card v-${cls}" data-token="${esc(t.token)}"><button type="button" class="scan-row" aria-expanded="${open}" aria-controls="${detailsId}"><span class="token-title">${l}<span><b>${esc(t.name||t.symbol||'Unnamed token')}</b><small>$${esc(t.symbol||'?')} · ${esc(t.pair_symbol||'—')} pair</small></span></span><span class="assessment ${cls}"><b>${num(t.score)}</b> / 100<small>${verdict}</small></span><span class="row-money">${usd(m.fdv0_usd)||'—'}<small>${fdvLabel(t)}</small></span><span class="outcome ${badOutcome?'neg':''}">${outcome}<small>Since assessment</small></span><span class="row-chevron" aria-hidden="true">${open?'−':'+'}</span></button>`;
if(open){const chips=[['Holders',m.holders],['Top 10 share',m.top10_pct==null?null:m.top10_pct+'%',m.top10_pct>=50,m.top10_pct<20],['Outside pool',m.outside_pool_pct==null?null:m.outside_pool_pct+'%'],['Unique buyers',m.unique_buyers],['Sniped',m.snipe_pct==null?null:m.snipe_pct+'%',m.snipe_pct>=30],['Creator bought',m.deployer_buy_pct==null?null:m.deployer_buy_pct+'%',m.deployer_buy_pct>=20],['Creator holding',m.deployer_hold_pct==null?null:m.deployer_hold_pct+'%',m.deployer_hold_pct>=10],['Creator tax',t.creator_tax_bps==null?null:(t.creator_tax_bps/100)+'%',(t.creator_tax_bps||0)>500],['Creator launches',m.creator_prev_launches,(m.creator_prev_launches||0)>10],['Creator trust',t.trust,t.trust<35,t.trust>=65],['Trades 1h',m.swaps_1h],['FDV now',usd(m.fdv_usd)],['Volume 24h',usd(m.volume_24h_usd)]];const links=[[`https://www.ponsfamily.com/launchpad/${t.token}`,'View on pons ↗'],[`https://robinhoodchain.blockscout.com/token/${t.token}`,'Token explorer ↗'],[`https://robinhoodchain.blockscout.com/address/${t.deployer}`,'Creator ↗'],[xurl(t.twitter),'X ↗'],[tgurl(t.telegram),'Telegram ↗'],[weburl(t.website),'Website ↗']];html+=`<div class="scan-details" id="${detailsId}"><div class="detail-top"><span>Assessed ${ago(t.scored_at)||'—'} · graduated ${ago(t.grad_ts)||'—'}${fdvLag(t)}</span><span class="${t.partial?'data-warning':''}">${t.partial?'Incomplete chain reads':'No incomplete-read flag'}</span></div><div class="chips">${chips.map(([label,val,warn,good])=>`<span class="chip${warn?' warn':good?' good':''}">${label} <b>${esc(val??'—')}</b></span>`).join('')}</div><ul class="reasons">${(t.reasons||[]).map(r=>`<li>${esc(r)}</li>`).join('')||'<li>No evidence details returned.</li>'}</ul><div class="meta">Token ${esc(t.token)}</div><div class="links">${links.filter(x=>x[0]).map(([url,label])=>`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`).join('')}</div></div>`}else html+=`<div id="${detailsId}" hidden></div>`;return html+'</article>'};
// The source feed handler also receives detail clicks; only the actual disclosure button toggles.
document.querySelector('#feed').addEventListener('click',e=>{if(!e.target.closest('.scan-row'))e.stopPropagation()},true);
if(lastState){enhance(lastState);renderFeed(lastFeed,true)}

// A schematic learning map, with measurements kept in readable HTML alongside it.
wrap.insertBefore(document.querySelector('#synapse'),document.querySelector('#learning'));
synapse=function(st,vd,sco,rd){
 const score=Math.max(0,Math.min(100,Number(rd.score)||0));
 const f=v=>v==null?'—':typeof v==='number'?v.toLocaleString():esc(v);
 const brain='M90,250 C90,120 230,50 480,45 C740,40 940,110 940,240 C940,330 860,380 720,395 C560,410 380,400 260,385 C150,370 90,320 90,250 Z';
 const lower='M175,368 C290,342 466,359 652,377 C690,402 633,449 474,463 C335,484 212,459 175,422 Z';
 const random=n=>{const x=Math.sin(n*127.1+17)*43758.54;return x-Math.floor(x)};
 const nodes=Array.from({length:96},(_,i)=>({x:115+random(i)*795,y:66+random(i+130)*380}));
 let network='';for(let i=0;i<nodes.length;i++){let a=nodes[i];for(let j=i+1;j<nodes.length;j++){let b=nodes[j],d=Math.hypot(a.x-b.x,a.y-b.y);if(d<95)network+=`<path d="M${a.x.toFixed(1)},${a.y.toFixed(1)} L${b.x.toFixed(1)},${b.y.toFixed(1)}" stroke="#76b78f" stroke-opacity="${(.12*(1-d/95)).toFixed(3)}"/>`}}
 const dots=nodes.map((n,i)=>`<circle cx="${n.x.toFixed(1)}" cy="${n.y.toFixed(1)}" r="${i%13===0?3.5:1.6}" fill="${i%13===0?'#caebd8':'#729982'}" opacity="${i%13===0?.9:.5}"/>`).join('');
 const metric=(label,value,note,extra='')=>`<div class="brain-reading"><span>${label}</span><strong>${value}</strong><small>${note}</small>${extra}</div>`;
 return `<div class="brain-topline"><span>LEARNING SYSTEM</span><span>RULES · EVIDENCE · OUTCOMES</span></div><div class="brain-layout"><div class="brain-specimen"><svg viewBox="45 15 940 535" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Schematic brain-shaped network representing the worm's rule-based learning; dots are decorative, not measured neurons."><defs><linearGradient id="brainwash" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#16291d"/><stop offset="1" stop-color="#080e0a"/></linearGradient><clipPath id="learningclip"><path d="${brain}"/><path d="${lower}"/></clipPath></defs><path d="M702,350 C707,415 698,469 714,524 L744,524 C736,466 751,422 753,366" fill="#101c14" stroke="#456a50" stroke-width="1.2"/><ellipse cx="841" cy="407" rx="92" ry="55" fill="#0d1911" stroke="#456a50" stroke-width="1.2"/><path d="${lower}" fill="url(#brainwash)" stroke="#456a50" stroke-width="1.2"/><path d="${brain}" fill="url(#brainwash)" stroke="#81b28e" stroke-width="1.5"/><g fill="none" stroke="#527d5f" stroke-width="1.1" opacity=".55"><path d="M500,46 C492,140 475,220 452,300 C430,340 395,350 375,389"/><path d="M760,62 C770,150 780,240 790,340"/><path d="M142,279 C217,222 307,299 372,275 C449,247 520,285 602,294 C697,307 744,355 829,334"/><path d="M155,159 C210,108 279,197 337,147 C380,108 400,136 426,173"/><path d="M490,108 C544,66 591,135 650,101 C688,78 720,105 742,148"/><path d="M242,393 C322,360 384,422 453,396 C520,374 571,421 619,403"/>${[386,404,422].map(y=>`<path d="M776,${y} Q843,${y-25} 909,${y}"/>`).join('')}</g><g clip-path="url(#learningclip)">${network}${dots}<path class="brain-flow" d="M158,248 C270,157 360,215 453,258 S664,321 819,223" fill="none" stroke="#a4dcba" stroke-width="1.5" stroke-dasharray="2 18" opacity=".6"/></g></svg><div class="brain-caption"><span>IRL WORM / LEARNING MAP</span><span>Schematic · not biological neurons</span></div></div><div class="brain-readings">${metric('Trading readiness',`${score}<em> / 100</em>`,`Evidence threshold ${f(rd.ready_at??80)}`,`<div class="brain-meter" role="progressbar" aria-label="Trading readiness" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${score}"><i style="width:${score}%"></i></div>`)}${metric('Confirmed warnings',f(sco.called),`${f(sco.checked_warnings)} warnings checked`)}${metric('Warning precision',sco.warn_precision==null?'—':f(sco.warn_precision)+'%',sco.warn_precision==null?'Awaiting resolved outcomes':'Among checked warnings')}${metric('Creators flagged',f(sco.creators_flagged),'From indexed creator history')}</div></div><div class="brain-counters">${[['Screened',st.scored],['In queue',st.queued],['Avoid assessments',vd.avoid??0],['Launches · 24h',st.launches_24h],['Graduations · 24h',st.grads_24h]].map(([l,v])=>`<div><span>${l}</span><b>${f(v)}</b></div>`).join('')}</div><div class="brain-footer"><span>Measured from the current learning records</span><span>Reading block <b>${f(st.last_block)}</b></span></div>`;
};
if(lastState)render(lastState);

function capList(el,items,headerHeight=0){
 if(!el)return;
 el.classList.add('five-scroll');
 if(items.length<=5){el.style.maxHeight='none';return}
 const first=items[0],fifth=items[4];
 const height=fifth.getBoundingClientRect().bottom-first.getBoundingClientRect().top+headerHeight+6;
 el.style.maxHeight=(height>20?Math.ceil(height):450)+'px';
}
function limitPanelLists(){
 document.querySelectorAll('.panel table').forEach(table=>{
  let box=table.parentElement;
  if(!box.classList.contains('table-scroll')){box=document.createElement('div');box.className='table-scroll';table.before(box);box.append(table);box.setAttribute('role','region');box.setAttribute('aria-label',(table.closest('.panel')?.querySelector('.ttl')?.textContent||'Data')+' table');box.tabIndex=0}
  const rows=[...table.querySelectorAll('tr')],data=rows.filter(r=>r.querySelector('td'));
  const header=rows.filter(r=>r.querySelector('th')).reduce((n,r)=>n+r.getBoundingClientRect().height,0);
  capList(box,data,header);
 });
 for(const [selector,child] of [['#feed','.card'],['#log','li'],['#lessons','.lesson'],['#voice','.post'],['#worst','.worst']]){
  const el=document.querySelector(selector);if(el){capList(el,[...el.querySelectorAll(child)]);if(el.querySelectorAll(child).length>5){el.tabIndex=0;el.setAttribute('aria-label','Scrollable list; five entries visible')}}
 }
}
const uncappedRender=render;render=function(s){uncappedRender(s);requestAnimationFrame(limitPanelLists)};
const uncappedFeed=renderFeed;renderFeed=function(feed,force){uncappedFeed(feed,force);requestAnimationFrame(limitPanelLists)};
const originalNavigate=navigate;navigate=function(){originalNavigate();requestAnimationFrame(()=>{document.querySelectorAll('.five-scroll').forEach(el=>el.scrollTop=0);limitPanelLists()})};
window.removeEventListener('hashchange',originalNavigate);window.addEventListener('hashchange',navigate);
document.addEventListener('click',e=>{const h=e.target.closest('.panel>h2');if(h)requestAnimationFrame(()=>{h.parentElement.querySelectorAll('.five-scroll').forEach(el=>el.scrollTop=0);limitPanelLists()})});
window.addEventListener('resize',()=>requestAnimationFrame(limitPanelLists));
requestAnimationFrame(limitPanelLists);

// Explore the real learning pipeline through the schematic brain.
let learningFocus='learn';
const learningAreas=[{id:'observe',name:'Observe',x:26,y:30},{id:'assess',name:'Assess',x:66,y:30},{id:'learn',name:'Learn',x:44,y:67},{id:'ready',name:'Readiness',x:83,y:75}];
function learningInspector(s,id){
 const st=s.stats||{},b=s.brain||{},sc=s.scout||{},rd=s.readiness||{},lab=s.lab||{};
 const val=v=>v==null?'—':esc(typeof v==='number'?v.toLocaleString():v);
 const pair=(a,v)=>`<div><dt>${a}</dt><dd>${val(v)}</dd></div>`;
 const rules=b.rules||[],moving=rules.filter(r=>Math.abs(r.weight-1)>.001);
 const copy={observe:['01 / OBSERVE','Read the chain','New launches and graduations become the evidence for each assessment.',val(st.launches_24h),'launches in the last 24 hours',pair('Graduations · 24h',st.grads_24h)+pair('Waiting to be screened',st.queued)+pair('Last block read',st.last_block),'scans','Explore token scans'],assess:['02 / ASSESS','Explain every call','Each token receives a rule-based assessment. Its later outcome is tracked separately.',val(st.scored),'tokens screened',pair('Avoid assessments',(st.verdicts||{}).avoid??0)+pair('Mixed assessments',(st.verdicts||{}).mixed??0)+pair('Looks healthy',(st.verdicts||{})['looks healthy']??0),'brain','Inspect the scoring rules'],learn:['03 / LEARN','Learn from the outcome','Resolved outcomes adjust rule weights. The record includes successful warnings and missed calls.',val(b.resolved),'verdicts checked',pair('Confirmed warnings',sc.called)+pair('Checked warnings',sc.checked_warnings)+pair('Healthy calls that turned bad',sc.missed)+pair('Rules changed from baseline',moving.length),'learning','Read the latest lessons'],ready:['04 / READINESS','Earn the next step','Readiness combines evidence from the strategy lab, warning performance, runway and surplus.',val(rd.score),'readiness out of 100',pair('Evidence threshold',rd.ready_at??80)+pair('Resolved strategy cases',lab.cases_resolved)+pair('Execution mode',s.live?'Live':'Demo — nothing sent'),'lab','Explore strategy results']}[id];
 let parts='';if(id==='ready')parts=`<div class="readiness-parts">${(rd.parts||[]).map(p=>`<div><span>${esc(p.label)}</span><b>${val(p.score)} / 100</b><progress max="100" value="${Math.max(0,Math.min(100,Number(p.score)||0))}" aria-label="${esc(p.label)}"></progress><small>${esc(p.detail||'')}</small></div>`).join('')}</div><div class="learning-next"><span>Next milestone</span><p>${esc(rd.next||'Waiting for enough evidence.')}</p></div>`;
 return `<div class="inspector-kicker">${copy[0]}</div><h3>${copy[1]}</h3><p class="inspector-summary">${copy[2]}</p><div class="inspector-number">${copy[3]}</div><div class="inspector-unit">${copy[4]}</div><dl class="inspector-facts">${copy[5]}</dl>${parts}<button type="button" class="learning-link" data-learning-jump="${copy[6]}">${copy[7]} <span aria-hidden="true">↗</span></button>`;
}
const staticBrain=synapse;
synapse=function(st,vd,sco,rd){
 const shell=document.createElement('div');shell.innerHTML=staticBrain(st,vd,sco,rd);
 const specimen=shell.querySelector('.brain-specimen');
 specimen.classList.add('interactive-brain');
 const stage=document.createElement('div');stage.className='brain-stage';
 stage.append(specimen.querySelector('svg'));
 const hotspots=document.createElement('div');hotspots.className='brain-hotspots';hotspots.setAttribute('role','group');hotspots.setAttribute('aria-label','Explore the learning process');
 hotspots.innerHTML=learningAreas.map((a,i)=>`<button type="button" class="brain-zone" data-learning-zone="${a.id}" aria-pressed="${a.id===learningFocus}" aria-controls="learning-inspector" style="left:${a.x}%;top:${a.y}%"><span class="zone-dot">${String(i+1).padStart(2,'0')}</span><span>${a.name}</span></button>`).join('');
 stage.append(hotspots);specimen.prepend(stage);
 stage.dataset.activeArea=learningFocus;
 const sculpture=document.createElement('div');sculpture.className='brain-sculpture';
 const brainSvg=stage.querySelector('svg');brainSvg.before(sculpture);sculpture.append(brainSvg);
 brainSvg.querySelectorAll('g[clip-path] circle').forEach((node,i)=>{
  const x=(Number(node.getAttribute('cx'))-45)/940*100,y=(Number(node.getAttribute('cy'))-15)/535*100;
  const area=learningAreas.reduce((a,b)=>Math.hypot(x-a.x,y-a.y)<Math.hypot(x-b.x,y-b.y)?a:b);
  node.classList.add('neural-node');node.dataset.area=area.id;node.style.setProperty('--node-delay',(-i*.173)+'s');node.style.setProperty('--node-duration',(2.8+(i%7)*.31)+'s');
 });
 brainSvg.querySelectorAll('g[clip-path] path:not(.brain-flow)').forEach((edge,i)=>{edge.classList.add('neural-edge');edge.style.setProperty('--node-delay',(-i*.13)+'s')});
 const svgNS='http://www.w3.org/2000/svg',signals=brainSvg.querySelector('g[clip-path]');
 ['M158,248 C270,157 360,215 453,258 S664,321 819,223','M242,393 C322,360 384,422 453,396 C520,374 571,421 619,403','M500,46 C492,140 475,220 452,300 C430,340 395,350 375,389'].forEach((d,i)=>{
  const path=document.createElementNS(svgNS,'path');path.setAttribute('d',d);path.setAttribute('fill','none');path.setAttribute('pathLength','100');path.setAttribute('class','neural-packet');path.style.setProperty('--packet-delay',(-i*2.1)+'s');signals.append(path);
 });

 shell.querySelector('.brain-topline').innerHTML='<span>INSIDE THE WORM’S MIND</span><span>Select a node to explore</span>';
 const inspector=shell.querySelector('.brain-readings');inspector.className='learning-inspector';inspector.id='learning-inspector';inspector.innerHTML=learningInspector(lastState||{stats:st,scout:sco,readiness:rd},learningFocus);
 return shell.innerHTML;
};
// Rebuild the brain only when a number it shows has changed. A new block alone is written in place, so the
// travelling signals, the breathing nodes and the selected node are not reset by every state message.
const builtBrain=synapse;let brainKey='';
synapse=function(st,vd,sco,rd){
 const s=lastState||{},b=s.brain||{},lab=s.lab||{};
 const key=JSON.stringify([st.launches_24h,st.grads_24h,st.scored,st.queued,vd,sco,rd.score,rd.ready_at,rd.ready,rd.parts,rd.next,b.resolved,(b.rules||[]).map(r=>r.weight),lab.cases_resolved,s.live,learningFocus]);
 if(key===brainKey&&document.querySelector('#synapse .brain-stage')){
  const blk=st.last_block==null?'—':typeof st.last_block==='number'?st.last_block.toLocaleString():String(st.last_block);
  const foot=document.querySelector('#synapse .brain-footer b');if(foot)foot.textContent=blk;
  document.querySelectorAll('#learning-inspector .inspector-facts dt').forEach(dt=>{if(dt.textContent==='Last block read'&&dt.nextElementSibling)dt.nextElementSibling.textContent=blk});
  return null;
 }
 brainKey=key;return builtBrain(st,vd,sco,rd);
};
function selectLearningArea(id){if(!learningAreas.some(x=>x.id===id))return;learningFocus=id;document.querySelectorAll('[data-learning-zone]').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.learningZone===id)));const el=document.querySelector('#learning-inspector');if(el){el.innerHTML=learningInspector(lastState||{},id);el.setAttribute('aria-live','polite')}}
document.querySelector('#synapse').addEventListener('click',e=>{const zone=e.target.closest('[data-learning-zone]');if(zone){selectLearningArea(zone.dataset.learningZone);return}const link=e.target.closest('[data-learning-jump]');if(!link)return;const target=link.dataset.learningJump;if(target==='scans'){location.hash='scans';return}const panel=document.querySelector(`[data-panel="${target}"]`);if(panel){delete folded[target];applyFolded();requestAnimationFrame(()=>{limitPanelLists();panel.scrollIntoView({behavior:window.motionPaused?'instant':'smooth',block:'start'})})}});
function redesignLessons(s){
 const lessons=s.lessons||[],el=document.querySelector('#lessons');
 if(el)el.innerHTML=lessons.length?lessons.map(x=>{const bad=['rugged','dumped'].includes(x.outcome),up=x.up||[],down=x.down||[];return `<details class="lesson learning-lesson" data-lesson-key="${esc(x.token||String(x.scored_at)+x.symbol)}"><summary><span class="lesson-symbol">$${esc(x.symbol||'?')}<small>${ago(x.scored_at)||'Assessment time unavailable'}</small></span><span class="lesson-result ${bad?'neg':x.outcome==='grew'?'pos':''}">${esc(x.outcome)}<small>${x.change_pct==null?'':pct(x.change_pct)}</small></span><span class="lesson-expand" aria-hidden="true">+</span></summary><div class="lesson-body"><div class="lesson-call">Assessment: <b>${esc(x.verdict)}</b> · ${num(x.score)} / 100</div><span class="lesson-tag">${esc(x.tag||'Outcome recorded')}</span><p>${up.length?'Increased weight: '+up.map(esc).join(', ')+'. ':''}${down.length?'Decreased weight: '+down.map(esc).join(', ')+'.':''}${!up.length&&!down.length?'No rule weight changed for this outcome.':''}</p></div></details>`}).join(''):'<div class="empty">Lessons appear as assessed tokens reach their outcome checks.</div>';
 const title=document.querySelector('#learning .ttl');if(title)title.textContent='What the worm learned';
 const subtitle=document.querySelector('#learning>h2 small');if(subtitle)subtitle.textContent='Outcomes, mistakes and changing rules';
}
const previousLearningRender=render;render=function(s){const focused=document.activeElement?.dataset?.learningZone;const opened=[...document.querySelectorAll('#lessons details[open]')].map(x=>x.dataset.lessonKey);previousLearningRender(s);redesignLessons(s);document.querySelectorAll('#lessons details').forEach(x=>{if(opened.includes(x.dataset.lessonKey))x.open=true});if(focused)document.querySelector(`[data-learning-zone="${focused}"]`)?.focus({preventScroll:true});requestAnimationFrame(limitPanelLists)};
document.querySelector('#lessons').addEventListener('toggle',()=>requestAnimationFrame(limitPanelLists),true);
if(lastState)render(lastState);

// Pointer motion affects only the schematic, never the data or the selection targets.
const brainSurface=document.querySelector('#synapse');
brainSurface.addEventListener('pointermove',e=>{
 const stage=e.target.closest('.brain-stage');if(!stage||window.motionPaused||e.pointerType==='touch')return;
 const r=stage.getBoundingClientRect(),x=Math.max(-1,Math.min(1,(e.clientX-r.left)/r.width*2-1)),y=Math.max(-1,Math.min(1,(e.clientY-r.top)/r.height*2-1));
 stage.style.setProperty('--brain-tilt-x',(-y*5)+'deg');stage.style.setProperty('--brain-tilt-y',(x*7)+'deg');
});
brainSurface.addEventListener('pointerout',e=>{const stage=e.target.closest('.brain-stage');if(stage&&!stage.contains(e.relatedTarget)){stage.style.setProperty('--brain-tilt-x','0deg');stage.style.setProperty('--brain-tilt-y','0deg')}});
const previousBrainSelect=selectLearningArea;selectLearningArea=function(id){previousBrainSelect(id);const stage=brainSurface.querySelector('.brain-stage');if(stage){stage.dataset.activeArea=learningFocus;stage.classList.remove('brain-response');void stage.offsetWidth;stage.classList.add('brain-response')}};

// Launch tape is a non-announcing live feed; keyboard focus pauses its motion.
const launchTape=document.querySelector('.ticker');launchTape.setAttribute('role','region');launchTape.setAttribute('aria-label','Latest token launches. Hover or focus to pause. Use Motion to pause and scroll the feed.');launchTape.tabIndex=0;
function updateLaunchTape(s){const empty=!(s.ticker||[]).length;launchTape.classList.toggle('is-empty',empty);if(empty)document.querySelector('#ticker').textContent='Waiting for new launches…'}
const preTapeRender=render;render=function(s){preTapeRender(s);updateLaunchTape(s)};
if(lastState)updateLaunchTape(lastState);
