/* Design layer. Original scoring, wallet and trading services remain unchanged. */
const names={overview:'Live',learning:'Learning',scans:'Token scans',treasury:'Treasury'};
const descriptions={scans:'Inspect each assessment, its evidence, and what happened afterward.',treasury:'Actual balances, operating reserves, and the money moving through the worm.',learning:'An open record of the calls, the mistakes, and the evidence behind each rule.',activity:'Follow the worm as it reads the chain and records its findings.'};
const wrap=document.querySelector('.wrap'),intro=document.createElement('div');intro.className='view-intro';wrap.prepend(intro);
wrap.append(document.querySelector('#disc'));
wrap.append(document.querySelector('#synapse'));
const specimenLeft=document.createElement('div');specimenLeft.className='specimen-label';specimenLeft.innerHTML='SPECIMEN / WORM<br><strong>BINARY CHAIN SCOUT</strong>';document.querySelector('.mast').append(specimenLeft);
const specimenRight=document.createElement('div');specimenRight.className='specimen-label right';specimenRight.id='specimen-block';specimenRight.innerHTML='ROBINHOOD CHAIN<br>WAITING FOR BLOCK';document.querySelector('.mast').append(specimenRight);
const guide=document.createElement('a');guide.href='/docs';guide.textContent='Docs ↗';guide.className='docs-nav';document.querySelector('.rail nav').append(guide);
const xlink=document.createElement('a');xlink.textContent='X ↗';xlink.className='docs-nav';xlink.target='_blank';xlink.rel='noopener noreferrer';xlink.hidden=true;document.querySelector('.rail nav').append(xlink);

function navigate(){if(location.hash==='#activity')history.replaceState(null,'','#overview');const view=location.hash.slice(1);const v=names[view]?view:'overview';document.body.dataset.view=v;document.querySelector('#view-title').textContent=names[v];document.querySelectorAll('.rail nav a').forEach(a=>{a.classList.toggle('active',a.dataset.view===v);if(a.dataset.view===v)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')});intro.hidden=v==='overview';intro.innerHTML=v==='overview'?'':`<h2>${names[v]}</h2><p>${descriptions[v]}</p>`;placeOverview(v);if(v==='overview')sizeWorm();if(v==='activity')drawVision();window.scrollTo({top:0,behavior:'instant'})}
function placeOverview(v){
 const band=document.querySelector('#liveband'),grid=document.querySelector('#grid'),feed=document.querySelector('[data-panel=feed]'),voice=document.querySelector('[data-panel=voice]');
 if(!band||!grid||!feed||!voice)return;
 let left=band.querySelector('.livecol-l'),right=band.querySelector('.livecol-r');
 if(!left){
  left=document.createElement('div');left.className='livecol livecol-l';
  right=document.createElement('div');right.className='livecol livecol-r';
  band.append(left,right);
  for(const key of ['dig','log']){const panel=band.querySelector('[data-panel="'+key+'"]');if(panel)(key==='dig'?left:right).append(panel)}
 }
 if(v==='overview'){if(feed.parentElement!==right)right.prepend(feed);if(voice.parentElement!==left)left.append(voice)}
 else{if(feed.parentElement!==grid)grid.insertBefore(feed,grid.firstElementChild);if(voice.parentElement!==grid)grid.append(voice)}
}
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
function presentMoney(v){return v==null?'…':Number(v).toLocaleString('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2})}
function renderLaunchIdentity(s){
 const ch=s.character||{},tr=s.treasury||{};
 const mode=s.live?'Payments enabled':tr.token?'Payments paused':'Pre-launch · payments off';
 document.querySelector('#stage').innerHTML=`<div class="identity-status"><span class="status-pill ${s.live?'is-on':''}">${mode}</span><span>Stage ${num((ch.stage??0)+1)} / ${num(ch.stages??7)} <b>${esc(ch.stage_name||'hatchling')}</b></span></div><div class="identity-growth"><span>${ch.demo?'Simulated character': 'Treasury value'} <b>${presentMoney(ch.usd)}</b></span><span>${ch.next_usd!=null?'Next stage at '+presentMoney(ch.next_usd):'Fully grown'}</span></div>`;
}

// Presentation only: the server remains the authority for balances, policy and execution.
function renderTreasuryPanels(s){
 const tr=s.treasury||{},rw=s.runway||{},cp=s.compute||{},td=s.trader||{},rd=s.readiness||{};
 const opened=new Set([...document.querySelectorAll('[data-treasury-detail][open]')].map(el=>el.dataset.treasuryDetail));
 const focused=document.activeElement?.closest('[data-treasury-detail]')?.dataset.treasuryDetail;
 const money=presentMoney,qty=(v,d=2)=>v==null?'—':Number(v).toLocaleString('en-US',{maximumFractionDigits:d});
 const pct=v=>v==null?'—':Math.round(Number(v)*100)+'%';
 const detail=(key,label,body)=>`<details class="treasury-detail" data-treasury-detail="${key}" ${opened.has(key)?'open':''}><summary>${label}<span aria-hidden="true">+</span></summary><div class="treasury-detail-body">${body}</div></details>`;
 const fact=(label,value,note='')=>`<div class="treasury-fact"><span>${label}</span><strong>${value}</strong>${note?`<small>${note}</small>`:''}</div>`;
 const row=(label,value)=>`<div><dt>${label}</dt><dd>${value}</dd></div>`;
 const link=(address,label,kind='address')=>/^0x[0-9a-f]{40}$/i.test(address||'')?`<a class="treasury-link" href="https://robinhoodchain.blockscout.com/${kind}/${address}" target="_blank" rel="noopener noreferrer">${label} ↗</a>`:'';
 const allocation=[['Creator',tr.share,'Paid to the creator','creator'],['Gold reserve',tr.gold_share,'Buys and holds GLD','gold'],['WORM burn',tr.burn_share,'Buys and burns WORM','burn'],['Operations',tr.ops_share,'Compute, gas & runway','ops']];
 const allocations=allocation.map(([label,share,note,tone])=>`<div class="allocation-item ${tone}"><span>${label}</span><strong>${pct(share)}</strong><small>${note}</small></div>`).join('');
 const strip=allocation.map(([label,share,,tone])=>`<span class="${tone}" style="flex:${Number.isFinite(share)?Math.max(0,share):0}" title="${label}: ${pct(share)}"></span>`).join('');
 const account=tr.wallet?`<span class="eyebrow">ROBINHOOD CHAIN WALLET</span><div class="wallet-address">${link(tr.wallet,esc(short(tr.wallet)))}<span class="status-pill ${s.live?'is-on':''}">${s.live?'Payments enabled':'Payments off'}</span></div><p>Balances below are on-chain. Reserved allocations remain owed until settlement.</p>`:`<span class="eyebrow">BEFORE THE FIRST CLAIM</span><h3>Built to fund its own work.</h3><p>The production wallet is not connected yet. Once configured, its balances and fee movements will appear here.</p><span class="status-pill">Wallet setup pending</span>`;
 const ledger=(tr.ledger||[]).length?`<table><thead><tr><th>When</th><th>Movement</th><th class="r">Amount</th></tr></thead><tbody>${tr.ledger.slice(0,30).map(l=>`<tr><td>${ago(l.ts)}</td><td><b>${esc(String(l.kind||'').replaceAll('_',' '))}</b><small class="ledger-note">${esc(l.note||'')}</small></td><td class="r">${qty(l.amount,4)}<small class="ledger-note">${esc(l.asset||'')}</small></td></tr>`).join('')}</tbody></table>`:'<p class="treasury-muted">No fee movements recorded yet. Confirmed and pending payments will appear here.</p>';
 const settlements=`<dl class="treasury-facts">${row('Claimed fees',money(tr.claimed_total))}${row('Sent to creator',money(tr.forwarded_total))}${row('Still owed to creator',money(tr.owed_to_owner))}${row('Spent on WORM burns',money(tr.burned_total))}${row('WORM burned',qty(tr.burned_qty,0))}${row('Still reserved for burns',money(tr.owed_to_burn))}${row('Spent on gold',money(tr.gold_total))}${row('Gold held',qty(tr.gold_held,6)+' GLD')}${row('Gold valuation',money(tr.gold_usd))}${row('Still reserved for gold',money(tr.owed_to_gold))}</dl><p class="treasury-muted">${esc(tr.burn_state||'Burns wait for an eligible token pool.')} · ${esc(tr.gold_state||'Gold purchases use their reserved allocation.')}</p>${tr.claim_policy?.reason?`<p class="treasury-muted">Claim check: ${esc(tr.claim_policy.reason)}${tr.claim_policy.threshold_usdg!=null?' · batch target '+money(tr.claim_policy.threshold_usdg):''}${tr.claim_policy.estimated_gas_usd!=null?' · estimated gas '+money(tr.claim_policy.estimated_gas_usd):''}</p>`:''}${tr.fee_sweep?.reason?`<p class="treasury-muted">Curve fees: ${esc(tr.fee_sweep.reason)}</p>`:''}${tr.gas_refill?.reason?`<p class="treasury-muted">Gas refill: ${esc(tr.gas_refill.reason)}</p>`:''}<div class="treasury-links">${link(tr.owner,'Creator wallet')}${/^0x[0-9a-f]{40}$/i.test(tr.token||'')?`<a class="treasury-link" href="https://www.ponsfamily.com/launchpad/${tr.token}" target="_blank" rel="noopener noreferrer">View token on pons ↗</a>`:''}</div>`;
 document.querySelector('#wallet').innerHTML=`${tr.accounting_error?'<div class="treasury-notice">Payment accounting needs operator review. Available funds cannot be verified.</div>':''}<div class="treasury-overview"><div class="wallet-intro">${account}</div><div class="wallet-balances">${fact('USDG balance',tr.wallet?qty(tr.usdg):'—','Robinhood Chain')}${fact('ETH for gas',tr.wallet?qty(tr.eth,6):'—','Native gas balance')}${fact('Claimable fees',tr.wallet?qty(tr.claimable_usdg):'—','USDG in escrow')}${fact('Prepaid AI credit',money(cp.balance_usd),cp.provider==='aisurplus'?'AI Surplus · separate from wallet':cp.provider==='venice'?'Venice · separate from wallet':'Provider balance unavailable')}</div></div><div class="allocation-heading"><h3>Where every claim goes</h3><span>Current allocation policy</span></div><div class="allocation-strip" aria-hidden="true">${strip}</div><div class="allocation-grid">${allocations}</div>${detail('settlements','Balances, reservations & settlement',settlements)}${detail('ledger','Payment history'+((tr.ledger||[]).length?' · '+tr.ledger.length:''),ledger)}`;

 const reserve=Number(rw.reserve_needed_usd),available=rw.treasury_usd;
 const coverage=available!=null&&Number.isFinite(reserve)&&reserve>0?Math.max(0,Math.min(100,100*available/reserve)):null;
 const funded=coverage!=null&&coverage>=100;
 const projected=Object.entries(rw.scenarios||{});
 const scenarios=projected.length?`<table><thead><tr><th>90-day scenario</th><th class="r">Ending balance</th><th class="r">Funds last</th></tr></thead><tbody>${projected.map(([name,v])=>`<tr><td>${esc(name==='current income'&&!rw.income_measured?'Income baseline (unmeasured)':name)}</td><td class="r ${v.end_balance_usd<0?'neg':''}">${money(v.end_balance_usd)}</td><td class="r">${v.runs_out_day!=null?'Day '+qty(v.runs_out_day):'Beyond 90 days'}</td></tr>`).join('')}</tbody></table>`:'<p class="treasury-muted">Projection data is not available yet.</p>';
 const costs=rw.cost_parts||{};
 document.querySelector('#runway').innerHTML=`<div class="runway-heading"><div><span class="eyebrow">OPERATIONS RESERVE</span><strong>${tr.accounting_error?'—':money(available)}</strong><span>of ${money(rw.reserve_needed_usd)} planned for ${qty(rw.reserve_days,0)} days</span></div><span class="status-pill">${tr.accounting_error?'Needs review':funded?'Reserve covered':available>0?'Building reserve':'Awaiting funding'}</span></div><div class="reserve-meter" role="progressbar" aria-label="Operations reserve funded" aria-valuemin="0" aria-valuemax="100" ${coverage!=null&&!tr.accounting_error?`aria-valuenow="${Math.round(coverage)}"`:''}><span style="width:${tr.accounting_error?0:coverage??0}%"></span></div><div class="runway-facts">${fact('Planned daily cost',money(rw.cost_per_day_usd),'Estimate, not billed usage')}${fact('Daily operations income',rw.income_measured?money(rw.income_per_day_usd):'Not measured',rw.income_measured?'Based on recent claims':'Waiting for claim history')}</div><p class="treasury-muted">${tr.accounting_error?'Resolve the accounting check before relying on reserve coverage.':'Only available operating funds count toward runway. Gold and money owed to other allocations are excluded.'}</p>${detail('projection','Explore the 90-day projection',scenarios+'<p class="treasury-muted">Scenarios use planned costs and the available claim history. Negative balances indicate a funding shortfall, not money already spent.</p>')} ${detail('costs','Cost assumptions & payment policy',`<dl class="treasury-facts">${row('Compute / day',money(costs.compute))}${row('Gas / day',money(costs.gas))}${row('Bridge / day',money(costs.bridge))}${row('Compute budget / day',money(rw.compute_budget_per_day_usd))}${row('Surplus after reserve',money(rw.surplus_usd))}${row('Claims / day',rw.income_measured?money(rw.claims_per_day_usd):'Not measured')}${row('Compute top-up',money(cp.topup_usd))}${row('Top-up below credit',money(cp.topup_below_usd))}${row('Pays with',esc(cp.pays_with||'Not configured'))}</dl><p class="treasury-muted">${esc(rw.rule||'')} Funding the reserve does not enable trading.</p>`)}`;

 const off=td.enabled===false,blocked=off||!s.live||!td.live_sell_ready||!rd.ready;
 const score=Math.max(0,Math.min(100,Number(rd.score)||0));
 const positions=(td.positions||[]).length?`<table><thead><tr><th>Position</th><th class="r">Size</th><th class="r">Entry</th><th>State</th></tr></thead><tbody>${td.positions.slice(0,30).map(p=>`<tr><td>$${esc(p.symbol)}<small class="ledger-note">${esc(p.mode)}</small></td><td class="r">${money(p.size_usd)}</td><td class="r">${usd(p.entry_usd)}</td><td>${p.status==='open'&&p.recovered_usd>0?'Initial cost recovered':esc(p.status)}${p.reason?' · '+esc(p.reason):''}</td></tr>`).join('')}</tbody></table>`:'<p class="treasury-muted">No positions recorded.</p>';
 const trades=(td.trades||[]).map(t=>`<li><span>${ago(t.ts)} · ${esc(t.side)} $${esc(t.symbol)} · ${money(t.usd)}</span><small>${esc(t.note||'')}</small></li>`).join('');
 document.querySelector('#trades').innerHTML=`<div class="trading-heading"><span class="status-pill ${blocked?'':'is-on'}">${off?'Trading off':blocked?'Execution gated':'Trading enabled'}</span><a class="treasury-link" href="#learning">Explore learning ↗</a></div><h3>${off?'Learning continues. Trading waits.':blocked?'Execution checks come first.':'Trading within its limits.'}</h3><p class="treasury-muted">${off?'WORM studies outcomes and paper trades while live trading stays off. Only the operator can enable it.':'The policy switch, live execution, readiness and supported exits must all permit a trade.'}</p><dl class="trading-checks">${row('Operator permission',off?'Off':td.enabled?'Enabled':'Unknown')}${row('Readiness evidence',score+' / '+qty(rd.ready_at??80,0)+' required')}${row('Operating reserve',tr.accounting_error?'Needs review':rw.can_invest?'Surplus available':'No surplus yet')}${row('Live exits',td.live_sell_ready?'Supported':'Not enabled')}</dl>${detail('trading-policy','Read the full trading policy','<p class="treasury-muted">'+esc(td.policy||'Policy unavailable')+'</p>')}${detail('positions','Positions & trade history',positions+(trades?'<ul class="treasury-history">'+trades+'</ul>':''))}`;
 if(focused)document.querySelector(`[data-treasury-detail="${focused}"] summary`)?.focus({preventScroll:true});
}
function enhance(s){if(s.links&&s.links.x){xlink.href=s.links.x;xlink.hidden=false}if(s.stats?.last_block!=null&&s.stats.last_block!==window.observedBlock){window.observedBlock=s.stats.last_block;window.blockAdvancedAt=Date.now()}const st=s.stats||{},rw=s.runway||{},rd=s.readiness||{},sc=s.scout||{},ch=s.character||{};document.querySelector('#specimen-block').innerHTML='ROBINHOOD CHAIN<br><strong>BLOCK '+esc(st.last_block??'…')+'</strong>';document.querySelector('#mode-label').textContent=s.live?'Payments enabled':'Payments off';const metrics=[['Tokens screened',num(st.scored),'Across the indexed window'],['Warnings confirmed',sc.called??'…',`${sc.checked_warnings??0} assessed warnings checked`],['Actual treasury',presentMoney(ch.usd_real??rw.treasury_usd),ch.gold_usd!=null?`Wallet assets · gold reserve ${presentMoney(ch.gold_usd)}`:'Wallet assets excluding gold'],['Trading readiness',`${rd.score??0}<em> / 100</em>`,(s.trader&&s.trader.enabled===false)?`Trading off by policy · threshold ${rd.ready_at??80}`:rd.ready?'Evidence threshold met':`Evidence threshold: ${rd.ready_at??80}`]];document.querySelector('#overview-metrics').innerHTML=metrics.map(([l,v,d])=>`<div class="metric"><span>${l}</span><strong>${v}</strong><small>${d}</small></div>`).join('');document.querySelector('#grow').textContent='The worm grows with the treasury it holds.';
renderLaunchIdentity(s);
const titles={feed:'Recent token scans',wallet:'Treasury overview',trades:'Trading controls',runway:'Operating runway',brain:'Rule performance',lab:'Strategy comparisons',paper:'Paper portfolio',advisor:'Advisor',bad:'Creator signals',voice:'Field notes'};for(const [k,v] of Object.entries(titles)){const el=document.querySelector(`[data-panel="${k}"] .ttl`);if(el)el.textContent=v}
// Make missing values distinct from measured zero where source data is available.
const r=document.querySelector('#ready');if(r){r.setAttribute('aria-label',`Trading readiness ${rd.score??0} out of 100`)}
}
const originalRender=render;render=function(s){originalRender(s);enhance(s)};
// The first price the feed returned for this verdict is kept as the at-scan value. It is missing until the feed
// lists the token, and for verdicts scored before that value was kept it was first read well after the scan.
function fdvLag(t){const m=t.metrics||{};if(!(m.price0_ts&&t.scored_at))return '';const lag=m.price0_ts-t.scored_at;return lag>900?` · first price read ${lag>=7200?Math.round(lag/3600)+' h':Math.round(lag/60)+' min'} after the scan`:''}
function fdvLabel(t){const m=t.metrics||{};return m.fdv0_usd==null?'FDV not read yet':fdvLag(t)?'FDV first read':'FDV at scan'}
card=function(t,isNew){const m=t.metrics||{},cls=vcls(t.verdict),open=expanded.has(t.token),badOutcome=/rugged|dumped/.test(t.outcome||'');const verdict=esc(t.verdict||'Pending');const outcome=t.outcome&&t.outcome!=='pending'?`${esc(t.outcome)}${t.change_pct!=null?' · '+pct(t.change_pct):''}`:t.change_pct!=null?pct(t.change_pct):'Awaiting outcome';const image=logo(t.logo);const l=image?`<img class="logo" src="${esc(image)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`:'';const detailsId='detail-'+String(t.token).replace(/[^a-zA-Z0-9]/g,'');
let html=`<article class="card v-${cls}" data-token="${esc(t.token)}"><button type="button" class="scan-row" aria-expanded="${open}" aria-controls="${detailsId}"><span class="token-title">${l}<span><b>${esc(t.name||t.symbol||'Unnamed token')}</b><small>$${esc(t.symbol||'?')} · ${esc(t.pair_symbol||'…')} pair</small></span></span><span class="assessment ${cls}"><b>${num(t.score)}</b> / 100<small>${verdict}</small></span><span class="row-money">${usd(m.fdv0_usd)||'…'}<small>${fdvLabel(t)}</small></span><span class="outcome ${badOutcome?'neg':''}">${outcome}<small>Since assessment</small></span><span class="row-chevron" aria-hidden="true">${open?'−':'+'}</span></button>`;
if(open){const chips=[['Holders',m.holders],['Top 10 share',m.top10_pct==null?null:m.top10_pct+'%',m.top10_pct>=50,m.top10_pct<20],['Outside pool',m.outside_pool_pct==null?null:m.outside_pool_pct+'%'],['Unique buyers',m.unique_buyers],['Sniped',m.snipe_pct==null?null:m.snipe_pct+'%',m.snipe_pct>=30],['Creator bought',m.deployer_buy_pct==null?null:m.deployer_buy_pct+'%',m.deployer_buy_pct>=20],['Creator holding',m.deployer_hold_pct==null?null:m.deployer_hold_pct+'%',m.deployer_hold_pct>=10],['Creator tax',t.creator_tax_bps==null?null:(t.creator_tax_bps/100)+'%',(t.creator_tax_bps||0)>500],['Creator launches',m.creator_prev_launches,(m.creator_prev_launches||0)>10],['Creator trust',t.trust,t.trust<35,t.trust>=65],['Trades 1h',m.swaps_1h],['FDV now',usd(m.fdv_usd)],['Volume 24h',usd(m.volume_24h_usd)]];const links=[[`https://www.ponsfamily.com/launchpad/${t.token}`,'View on pons ↗'],[`https://robinhoodchain.blockscout.com/token/${t.token}`,'Token explorer ↗'],[`https://robinhoodchain.blockscout.com/address/${t.deployer}`,'Creator ↗'],[xurl(t.twitter),'X ↗'],[tgurl(t.telegram),'Telegram ↗'],[weburl(t.website),'Website ↗']];html+=`<div class="scan-details" id="${detailsId}"><div class="detail-top"><span>Assessed ${ago(t.scored_at)||'…'} · graduated ${ago(t.grad_ts)||'…'}${fdvLag(t)}</span><span class="${t.partial?'data-warning':''}">${t.partial?'Incomplete chain reads':'No incomplete-read flag'}</span></div><div class="chips">${chips.map(([label,val,warn,good])=>`<span class="chip${warn?' warn':good?' good':''}">${label} <b>${esc(val??'…')}</b></span>`).join('')}</div><ul class="reasons">${(t.reasons||[]).map(r=>`<li>${esc(r)}</li>`).join('')||'<li>No evidence details returned.</li>'}</ul><div class="meta">Token ${esc(t.token)}</div><div class="links">${links.filter(x=>x[0]).map(([url,label])=>`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`).join('')}</div></div>`}else html+=`<div id="${detailsId}" hidden></div>`;return html+'</article>'};
// The source feed handler also receives detail clicks; only the actual disclosure button toggles.
document.querySelector('#feed').addEventListener('click',e=>{if(!e.target.closest('.scan-row'))e.stopPropagation()},true);
if(lastState){enhance(lastState);renderTreasuryPanels(lastState);renderFeed(lastFeed,true)}

// A schematic learning map, with measurements kept in readable HTML alongside it.
wrap.insertBefore(document.querySelector('#synapse'),document.querySelector('#learning'));
synapse=function(st,vd,sco,rd){
 const score=Math.max(0,Math.min(100,Number(rd.score)||0));
 const f=v=>v==null?'…':typeof v==='number'?v.toLocaleString():esc(v);
 const brain='M90,250 C90,120 230,50 480,45 C740,40 940,110 940,240 C940,330 860,380 720,395 C560,410 380,400 260,385 C150,370 90,320 90,250 Z';
 const lower='M175,368 C290,342 466,359 652,377 C690,402 633,449 474,463 C335,484 212,459 175,422 Z';
 const random=n=>{const x=Math.sin(n*127.1+17)*43758.54;return x-Math.floor(x)};
 const nodes=Array.from({length:96},(_,i)=>({x:115+random(i)*795,y:66+random(i+130)*380}));
 let network='';for(let i=0;i<nodes.length;i++){let a=nodes[i];for(let j=i+1;j<nodes.length;j++){let b=nodes[j],d=Math.hypot(a.x-b.x,a.y-b.y);if(d<95)network+=`<path d="M${a.x.toFixed(1)},${a.y.toFixed(1)} L${b.x.toFixed(1)},${b.y.toFixed(1)}" stroke="#76b78f" stroke-opacity="${(.12*(1-d/95)).toFixed(3)}"/>`}}
 const dots=nodes.map((n,i)=>`<circle cx="${n.x.toFixed(1)}" cy="${n.y.toFixed(1)}" r="${i%13===0?3.5:1.6}" fill="${i%13===0?'#caebd8':'#729982'}" opacity="${i%13===0?.9:.5}"/>`).join('');
 const metric=(label,value,note,extra='')=>`<div class="brain-reading"><span>${label}</span><strong>${value}</strong><small>${note}</small>${extra}</div>`;
 return `<div class="brain-topline"><span>LEARNING SYSTEM</span><span>RULES · EVIDENCE · OUTCOMES</span></div><div class="brain-layout"><div class="brain-specimen"><svg viewBox="45 15 940 535" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Schematic brain-shaped network representing the worm's rule-based learning; dots are decorative, not measured neurons."><defs><linearGradient id="brainwash" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#16291d"/><stop offset="1" stop-color="#080e0a"/></linearGradient><clipPath id="learningclip"><path d="${brain}"/><path d="${lower}"/></clipPath></defs><path d="M702,350 C707,415 698,469 714,524 L744,524 C736,466 751,422 753,366" fill="#101c14" stroke="#456a50" stroke-width="1.2"/><ellipse cx="841" cy="407" rx="92" ry="55" fill="#0d1911" stroke="#456a50" stroke-width="1.2"/><path d="${lower}" fill="url(#brainwash)" stroke="#456a50" stroke-width="1.2"/><path d="${brain}" fill="url(#brainwash)" stroke="#81b28e" stroke-width="1.5"/><g fill="none" stroke="#527d5f" stroke-width="1.1" opacity=".55"><path d="M500,46 C492,140 475,220 452,300 C430,340 395,350 375,389"/><path d="M760,62 C770,150 780,240 790,340"/><path d="M142,279 C217,222 307,299 372,275 C449,247 520,285 602,294 C697,307 744,355 829,334"/><path d="M155,159 C210,108 279,197 337,147 C380,108 400,136 426,173"/><path d="M490,108 C544,66 591,135 650,101 C688,78 720,105 742,148"/><path d="M242,393 C322,360 384,422 453,396 C520,374 571,421 619,403"/>${[386,404,422].map(y=>`<path d="M776,${y} Q843,${y-25} 909,${y}"/>`).join('')}</g><g clip-path="url(#learningclip)">${network}${dots}<path class="brain-flow" d="M158,248 C270,157 360,215 453,258 S664,321 819,223" fill="none" stroke="#a4dcba" stroke-width="1.5" stroke-dasharray="2 18" opacity=".6"/></g></svg><div class="brain-caption"><span>WORM / LEARNING MAP</span><span>Schematic · not biological neurons</span></div></div><div class="brain-readings">${metric('Trading readiness',`${score}<em> / 100</em>`,`Evidence threshold ${f(rd.ready_at??80)}`,`<div class="brain-meter" role="progressbar" aria-label="Trading readiness" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${score}"><i style="width:${score}%"></i></div>`)}${metric('Confirmed warnings',f(sco.called),`${f(sco.checked_warnings)} warnings checked`)}${metric('Warning precision',sco.warn_precision==null?'…':f(sco.warn_precision)+'%',sco.warn_precision==null?'Awaiting resolved outcomes':'Among checked warnings')}${metric('Creators flagged',f(sco.creators_flagged),'From indexed creator history')}</div></div><div class="brain-counters">${[['Screened',st.scored],['In queue',st.queued],['Avoid assessments',vd.avoid??0],['Launches · 24h',st.launches_24h],['Graduations · 24h',st.grads_24h]].map(([l,v])=>`<div><span>${l}</span><b>${f(v)}</b></div>`).join('')}</div><div class="brain-footer"><span>Measured from the current learning records</span><span>Reading block <b>${f(st.last_block)}</b></span></div>`;
};
if(lastState)render(lastState);

function capList(el,items,headerHeight=0){
 if(!el||!el.getClientRects().length)return;
 el.classList.add('five-scroll');
 if(!items.length){el.style.maxHeight='none';return}
 // Expanded scan evidence must not increase the entire feed's viewport height.
 const count=el.id==='feed'&&document.body.dataset.view==='overview'?4:5;
 const visible=items.slice(0,count),gap=parseFloat(getComputedStyle(el).rowGap)||0;
 const height=visible.reduce((sum,item)=>{
  const row=el.id==='feed'?item.querySelector('.scan-row'):item;
  const style=getComputedStyle(item);
  return sum+(row||item).getBoundingClientRect().height+(parseFloat(style.marginBottom)||0)+gap;
 },headerHeight+6);
 const ceiling=el.id==='voice'?440:el.id==='feed'?620:560;
 el.style.maxHeight=Math.ceil(Math.min(height,ceiling))+'px';
}
function limitPanelLists(){
 const activityLog=document.querySelector('#log');
 if(activityLog)activityLog.style.height='';
 document.querySelectorAll('.panel table').forEach(table=>{
  let box=table.parentElement;
  if(!box.classList.contains('table-scroll')){box=document.createElement('div');box.className='table-scroll';table.before(box);box.append(table);box.setAttribute('role','region');box.setAttribute('aria-label',(table.closest('.panel')?.querySelector('.ttl')?.textContent||'Data')+' table');box.tabIndex=0}
  const rows=[...table.querySelectorAll('tr')],data=rows.filter(r=>r.querySelector('td'));
  const header=rows.filter(r=>r.querySelector('th')).reduce((n,r)=>n+r.getBoundingClientRect().height,0);
  capList(box,data,header);
 });
 for(const [selector,child] of [['#feed','.card'],['#log','li'],['#lessons','.lesson'],['#voice','.post'],['#worst','.worst']]){
  const el=document.querySelector(selector);if(el){capList(el,[...el.querySelectorAll(child)]);if(el.querySelectorAll(child).length>5){el.tabIndex=0;el.setAttribute('aria-label','Scrollable list; scroll for more entries')}}
 }
 // Fill the remaining desktop column with readable activity, not a blank spacer.
 const notes=document.querySelector('.livecol-l [data-panel=voice]');
 const logPanel=activityLog?.closest('.panel');
 if(document.body.dataset.view==='overview'&&matchMedia('(min-width:900px)').matches&&notes&&logPanel&&!notes.classList.contains('collapsed')&&!logPanel.classList.contains('collapsed')){
  const extra=notes.getBoundingClientRect().bottom-logPanel.getBoundingClientRect().bottom;
  if(extra>1){const height=activityLog.getBoundingClientRect().height+extra;activityLog.style.height=height+'px';activityLog.style.maxHeight=height+'px';}
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
 const val=v=>v==null?'…':esc(typeof v==='number'?v.toLocaleString():v);
 const pair=(a,v)=>`<div><dt>${a}</dt><dd>${val(v)}</dd></div>`;
 const rules=b.rules||[],moving=rules.filter(r=>Math.abs(r.weight-1)>.001);
 const copy={observe:['01 / OBSERVE','Read the chain','New launches and graduations become the evidence for each assessment.',val(st.launches_24h),'launches in the last 24 hours',pair('Graduations · 24h',st.grads_24h)+pair('Waiting to be screened',st.queued)+pair('Last block read',st.last_block),'scans','Explore token scans'],assess:['02 / ASSESS','Explain every call','Each token receives a rule-based assessment. Its later outcome is tracked separately.',val(st.scored),'tokens screened',pair('Avoid assessments',(st.verdicts||{}).avoid??0)+pair('Mixed assessments',(st.verdicts||{}).mixed??0)+pair('Looks healthy',(st.verdicts||{})['looks healthy']??0),'brain','Inspect the scoring rules'],learn:['03 / LEARN','Learn from the outcome','Resolved outcomes adjust rule weights. The record includes successful warnings and missed calls.',val(b.resolved),'verdicts checked',pair('Confirmed warnings',sc.called)+pair('Checked warnings',sc.checked_warnings)+pair('Healthy calls that turned bad',sc.missed)+pair('Rules changed from baseline',moving.length)+pair('Learned rules in use',((s.advisor||{}).rules||[]).filter(r=>r.active).length),'learning','Read the latest lessons'],ready:['04 / READINESS','Earn the next step','Readiness combines evidence from the strategy lab, warning performance, runway and surplus.',val(rd.score),'readiness out of 100',pair('Evidence threshold',rd.ready_at??80)+pair('Resolved strategy cases',lab.cases_resolved)+pair('Execution mode',s.live?'Live':'Demo, nothing sent'),'lab','Explore strategy results']}[id];
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
  const blk=st.last_block==null?'…':typeof st.last_block==='number'?st.last_block.toLocaleString():String(st.last_block);
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


// Server-authoritative launch countdown. Reaching zero never sends a browser-side launch request.
(()=>{
 const panel=document.getElementById('launch-clock');
 const digits=document.getElementById('launch-clock-digits'),note=document.getElementById('launch-clock-note');
 const date=document.getElementById('launch-clock-date'),tokenLink=document.getElementById('launch-clock-token');
 let schedule=null,received=0,inflight=false,lastError=false;
 function draw(){
  if(!schedule){digits.textContent='—';note.textContent=lastError?'Launch schedule temporarily unavailable.':'Connecting to launch schedule…';return}
  const elapsed=(performance.now()-received)/1000,now=schedule.server_now+elapsed;
  const stale=elapsed>20||lastError;
  panel.dataset.state=schedule.state;
  date.textContent=schedule.at?new Date(schedule.at*1000).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'long'}):'';
  if(schedule.at)date.dateTime=new Date(schedule.at*1000).toISOString();else date.removeAttribute('datetime');
  tokenLink.hidden=true;
  if(schedule.state==='launched'){
   digits.textContent='LAUNCHED';note.textContent='WORM’s token is on-chain.';
   const allocation=schedule.allocation;
   if(allocation)note.textContent=allocation.state==='complete'?'Initial allocation complete: 1% to the creator, 1% retained by WORM (plus purchase rounding).':allocation.state==='review'?'Token launched. The initial allocation needs an operator check.':'Token launched with its initial purchase. The creator’s 1% transfer is awaiting completion.';
   if(/^0x[0-9a-f]{40}$/i.test(schedule.token||'')){tokenLink.href='https://www.ponsfamily.com/launchpad/'+schedule.token;tokenLink.hidden=false}
  }else if(schedule.state==='preparing'){
   digits.textContent='PREPARING LAUNCH';note.textContent=schedule.reason||'Checking the initial purchase and its spending limits.';
  }else if(schedule.state==='launching'||schedule.state==='pending'){
   digits.textContent=schedule.state==='pending'?'AWAITING CONFIRMATION':'LAUNCH STARTED';
   note.textContent='Follow the launch in the live screen and activity log.';
  }else if(['failed','review'].includes(schedule.state)){
   digits.textContent='NEEDS REVIEW';note.textContent='The launch needs an operator check before another attempt.';
  }else if(schedule.at&&['scheduled','due'].includes(schedule.state)){
   const remaining=Math.max(0,Math.ceil(schedule.at-now));
   const d=Math.floor(remaining/86400),h=Math.floor(remaining%86400/3600),m=Math.floor(remaining%3600/60),sec=remaining%60;
   digits.textContent=remaining?[d+'d',String(h).padStart(2,'0')+'h',String(m).padStart(2,'0')+'m',String(sec).padStart(2,'0')+'s'].join(' : '):'LAUNCH WINDOW OPEN';
   note.textContent=schedule.reason||(remaining?(schedule.live_enabled?'WORM begins its launch checks when the countdown reaches zero.':'Launch time is set. Live execution is not enabled yet.'):'Waiting for the server to confirm launch status.');
  }else{digits.textContent='TO BE ANNOUNCED';note.textContent='The launch time will appear here when it is set.'}
  if(stale)note.textContent='Live status delayed. Reconnecting to confirm the launch state…';
 }
 async function refresh(){
  if(inflight||document.hidden)return;inflight=true;
  try{const r=await fetch('/api/launch/status',{cache:'no-store',signal:AbortSignal.timeout(8000)});if(!r.ok)throw Error();const data=await r.json();if(!Number.isFinite(data.server_now))throw Error();schedule=data;received=performance.now();lastError=false}
  catch{lastError=true}finally{inflight=false;draw()}
 }
 setInterval(draw,1000);setInterval(refresh,5000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});refresh();
})();
