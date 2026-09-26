/* WORM views. One render(state) draws the header, the four views and the footer from the server's state.
   Presentation only: every figure is the server's, the page never sends a transaction. The engine (canvases,
   the live dig, the connection) is the inline script in index.html. */

const VIEWS=['overview','learning','scans','treasury'];
const SUBS={report:'report',paper:'paper',rules:'d-rules',mind:'mind',lessons:'lessons-card',burn:'burn',wallet:'wallet',notes:'voice-card'};
const WORM_SUPPLY=1e9;                        // every pons token is minted with a fixed supply of one billion
const STAGE_FLOORS=[0,20,100,500,2000,10000,50000];
/* plain names for the scoring rules, for holders; the ids stay in the Details tables */
const RULE_NAMES={activity:'trading since graduation',bot_fleet:'bot fleets',buyers:'number of buyers',creator_grads:"creator's past graduations",
 creator_history:"creator's past launches",creator_rugs:"creator's past rugs",creator_tax:'creator tax',deployer_buy:'creator buying its own token',
 deployer_hold:"creator's own holdings",fresh_buyers:'brand-new wallets',funding_cluster:'wallets funded from one source',linked_wallets:'linked wallets',
 losing_crowd:'wallets that keep losing',pace:'graduation pace',snipe:'sniping',socials:'socials',top10:'top-10 holder share',top_buyer:'biggest single buyer'};
const ruleName=id=>RULE_NAMES[id]||String(id||'').replace(/_/g,' ');
const cap=s=>String(s||'').replace(/^./,c=>c.toUpperCase());
const $$=s=>[...document.querySelectorAll(s)];
const nz=x=>Number.isFinite(Number(x))?Number(x):0;
const EXPLORER='https://robinhoodchain.blockscout.com/';
const isAddr=a=>/^0x[0-9a-f]{40}$/i.test(a||'');
const txUrl=(s,tx)=>/^0x[0-9a-f]{64}$/i.test(tx||'')?String((s.links&&s.links.explorer)||EXPLORER).replace(/\/?$/,'/')+'tx/'+tx:null;
const COPY_ICON='<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/></svg>';
const copyBtn=(text,label='Copy for X')=>`<button class="w-btn w-btn--ghost w-copy" type="button" data-copy="${esc(text)}">${COPY_ICON}<span aria-live="polite">${label}</span></button>`;
let view='overview',firstRender=true;
const keys={};
function changed(name,val){const k=JSON.stringify(val);if(keys[name]===k)return false;keys[name]=k;return true}
function safe(fn,s){try{fn(s)}catch(e){console.error('render '+(fn.name||'?'),e)}}
document.documentElement.classList.add('js');
try{localStorage.removeItem('irlworm.panels')}catch(_){}           // the old panel folding is gone

/* ---------- motion: one flag, remembered; the system setting decides until the visitor chooses ---------- */
const reduceMQ=matchMedia('(prefers-reduced-motion: reduce)');
function storedMotion(){try{return localStorage.getItem('worm.motion')}catch(_){return null}}
function applyMotion(){
 const st=storedMotion();window.motionPaused=st==='off'||(st!=='on'&&reduceMQ.matches);
 document.documentElement.classList.toggle('motion-off',window.motionPaused);
 const b=$('#motion'),label=window.motionPaused?'Resume animation':'Pause animation';b.setAttribute('aria-pressed',String(window.motionPaused));b.setAttribute('aria-label',label);b.title=label;
 if(window.motionPaused){drawHood(true);scoutTick(performance.now(),true)}
 wakeLoops();
}
$('#motion').addEventListener('click',()=>{try{localStorage.setItem('worm.motion',window.motionPaused?'on':'off')}catch(_){}applyMotion()});
if(reduceMQ.addEventListener)reduceMQ.addEventListener('change',()=>{if(!storedMotion())applyMotion()});

/* ---------- freshness, health and the status popover ---------- */
window.updateFreshness=()=>{
 const age=window.lastReceived?(Date.now()-window.lastReceived)/1000:Infinity;
 const health=window.operationalHealth,healthOld=window.healthCheckedAt&&Date.now()-window.healthCheckedAt>90000;
 const chainStalled=window.blockAdvancedAt&&Date.now()-window.blockAdvancedAt>180000;
 const unhealthy=healthOld||(health&&health.ok===false),stale=window.loadFailed||age>45||chainStalled||unhealthy;
 const secs=Number.isFinite(age)?Math.floor(age):0,ageText=secs<120?secs+'s':Math.floor(secs/60)+'m';
 const [word,long]=unhealthy?['Delayed','Data collection delayed']:age===Infinity?['Connecting…','Connecting to data…']:chainStalled?['Delayed','Chain progress delayed']:
  health&&health.catching_up?['Catching up','Catching up with the chain']:stale?['Delayed',`Updates delayed · ${ageText} ago`]:['Live',`Updated ${ageText} ago`];
 const el=$('#freshness');el.innerHTML=`<span class="status__word">${word}</span>${age===Infinity?'':`<span class="status__age"> · ${ageText}</span>`}`;
 $('#status').classList.toggle('is-warn',Boolean(stale)&&age!==Infinity);$('#status').classList.toggle('is-wait',age===Infinity);
 $('#status').setAttribute('aria-label','Status: '+long+'. Show details');$('#pop-data').textContent=long;
};
setInterval(()=>window.updateFreshness(),1000);
async function checkOperationalHealth(){
 const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
 try{const response=await fetch('/healthz',{cache:'no-store',signal:controller.signal});const status=await response.json();
  window.operationalHealth={ok:response.ok&&status.ok===true,catching_up:status.catching_up===true};
 }catch(_){window.operationalHealth={ok:false}}finally{clearTimeout(timeout);window.healthCheckedAt=Date.now();window.updateFreshness()}
}
checkOperationalHealth();setInterval(checkOperationalHealth,30000);
const statusBtn=$('#status'),statusPop=$('#status-pop');
function setPop(open){statusPop.hidden=!open;statusBtn.setAttribute('aria-expanded',String(open))}
statusBtn.addEventListener('click',()=>setPop(statusPop.hidden));
document.addEventListener('click',e=>{if(!statusPop.hidden&&!e.target.closest('.status-wrap'))setPop(false)});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!statusPop.hidden){setPop(false);statusBtn.focus()}});

/* ---------- count-ups, meters and section reveals: only when seen, never on an unchanged value ---------- */
const seenIO=new IntersectionObserver(es=>{for(const e of es)if(e.isIntersecting){seenIO.unobserve(e.target);e.target._seen=true;const p=e.target._pend;e.target._pend=null;if(p)p()}},{threshold:.3});
function tween(el,from,to,dur,f){cancelAnimationFrame(el._raf);if(window.motionPaused||from===to||document.activeElement===el){el.textContent=f(to);return}
 const t0=performance.now(),step=now=>{const k=Math.min(1,(now-t0)/dur),e=1-Math.pow(1-k,3);el.textContent=f(from+(to-from)*e);if(k<1)el._raf=requestAnimationFrame(step)};el._raf=requestAnimationFrame(step)}
function setNum(el,v,f){if(!el)return;if(!isNum(v)){cancelAnimationFrame(el._raf);el.textContent='—';el.dataset.v='';return}
 v=Number(v);const had=el.dataset.v!=null&&el.dataset.v!=='',prev=had?Number(el.dataset.v):null;el.dataset.v=v;
 if(had&&prev===v)return;
 if(window.motionPaused){el.textContent=f(v);return}
 if(!el._seen){el.textContent=f(0);el._pend=()=>tween(el,0,Number(el.dataset.v),1400,f);seenIO.observe(el);return}
 tween(el,prev??0,v,prev==null?1400:900,f)}
function setMeter(el,p){if(!el)return;const w=Math.max(0,Math.min(100,nz(p))).toFixed(2)+'%';if(el._seen||window.motionPaused){el.style.setProperty('--w',w);return}
 el._pend=()=>el.style.setProperty('--w',w);seenIO.observe(el)}
const revealIO=new IntersectionObserver(es=>{for(const e of es)if(e.isIntersecting){revealIO.unobserve(e.target);e.target.classList.add('is-in')}},{threshold:.06});
function revealVisible(){for(const el of $$('.view:not([hidden]) .reveal:not(.is-in)')){const r=el.getBoundingClientRect();if(r.top<innerHeight)el.classList.add('is-in','no-trans');else revealIO.observe(el)}
 requestAnimationFrame(()=>requestAnimationFrame(()=>$$('.no-trans').forEach(el=>el.classList.remove('no-trans'))))}

/* ---------- reading position: a refresh never moves the row under the reader ---------- */
function headerH(){return ($('.app-header')||{}).offsetHeight||64}
function preserveReadingPosition(update){
 const probe=headerH()+12;let anchor=null,key=null,top=0;
 if(scrollY>0){anchor=$$('.view:not([hidden]) [data-anchor],.view:not([hidden]) [data-key]').filter(el=>{const r=el.getBoundingClientRect();return r.height&&r.top<=probe&&r.bottom>probe}).pop()||null;
  if(anchor){top=anchor.getBoundingClientRect().top;key=anchor.dataset.key||null}}
 const result=update();
 if(anchor){const el=anchor.isConnected?anchor:key?document.querySelector(`.view:not([hidden]) [data-key="${CSS.escape(key)}"]`):null;
  if(el){const d=el.getBoundingClientRect().top-top;if(Math.abs(d)>1)window.scrollTo({top:scrollY+d,behavior:'instant'})}}
 return result}

/* ---------- views: containers switched by the hash; #learning/report style sub-anchors scroll to a section ---------- */
function parseHash(){const [v,sub]=location.hash.replace(/^#/,'').split('/');if(v==='activity')return {v:'overview'};if(VIEWS.includes(v))return {v,sub};if(v&&document.getElementById(v)&&document.getElementById(v).closest('.view'))return {v:document.getElementById(v).closest('.view').dataset.view,sub:v};return {v:'overview'}}
function navigate(initial){
 const {v,sub}=parseHash(),changedView=v!==view;view=v;document.body.dataset.view=v;
 for(const s of $$('.view'))s.hidden=s.dataset.view!==v;
 for(const a of $$('.tabs a')){const on=a.dataset.view===v;a.classList.toggle('is-active',on);if(on)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')}
 document.title=(v==='overview'?'WORM · knows the dirt on every launch':{learning:'Learning',scans:'Token scans',treasury:'Treasury'}[v]+' · WORM');
 moveBrain(v);
 if(lastState)render(lastState);
 if(v==='overview'){sizeWorm();sizeRain();if(window.motionPaused){drawHood(true);scoutTick(performance.now(),true)}drawVision()}
 revealVisible();wakeLoops();
 const target=sub&&document.getElementById(SUBS[sub]||sub);
 if(target){if(target.tagName==='DETAILS')target.open=true;target.classList.add('is-in');requestAnimationFrame(()=>target.scrollIntoView({block:'start',behavior:initial||window.motionPaused?'instant':'smooth'}))}
 else if(changedView&&!initial)window.scrollTo({top:0,behavior:'instant'});
 if(!initial&&changedView&&!target)$(`#view-${v} h1`)?.focus({preventScroll:true});
}
addEventListener('hashchange',()=>navigate(false));
document.addEventListener('click',e=>{const a=e.target.closest('a[href^="#"]');if(!a)return;if(a.getAttribute('href')===location.hash||(a.getAttribute('href')==='#overview'&&!location.hash)){e.preventDefault();navigate(false);if(!location.hash.includes('/'))window.scrollTo({top:0,behavior:window.motionPaused?'instant':'smooth'})}});
let resizeTimer=null;addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(view==='overview'){sizeWorm();sizeRain()}for(const k in keys)if(k.startsWith('week'))delete keys[k];if(lastState)render(lastState)},200)});

/* ---------- copy for X ---------- */
document.addEventListener('click',async e=>{const b=e.target.closest('[data-copy]');if(!b)return;const label=b.querySelector('span')||b,was=label.dataset.was||label.textContent;label.dataset.was=was;
 try{await navigator.clipboard.writeText(b.dataset.copy||'');label.textContent='Copied ✓'}catch(_){label.textContent='Copy failed'}clearTimeout(b._t);b._t=setTimeout(()=>{label.textContent=was},1500)});
/* show-more buttons */
const more={lessons:6,voice:3,feed:20,serial:5};
function moreButton(btn,total,shown,step,labelMore,key){btn.hidden=total<=step;btn.textContent=shown<total?labelMore:'Show fewer';btn.dataset.key=key}
document.addEventListener('click',e=>{const b=e.target.closest('.more-btn');if(!b||!b.dataset.key)return;const k=b.dataset.key,step={lessons:6,voice:3,feed:20,serial:5}[k],total=Number(b.dataset.total||0);
 more[k]=more[k]>=total?step:k==='feed'?more[k]+20:total;delete keys[k];if(lastState)render(lastState)});

/* ================= the one render ================= */
function render(s){
 lastState=s;
 preserveReadingPosition(()=>{
  safe(renderHeader,s);safe(syncDig,s);safe(detectEvents,s);
  for(const fn of [renderHero,renderPulse,renderAtWork,renderMindLive,renderReceiptsLive])safe(fn,s);
  for(const fn of [renderReport,renderLanes,renderInspector,renderLessons,renderMoving,renderPaper,renderVoice,renderLearningDetails])safe(fn,s);
  for(const fn of [renderScans,renderCreators])safe(fn,s);
  for(const fn of [renderMoney,renderBurnCard,renderGold,renderRunway,renderWallet,renderTrading,renderTreasuryDetails])safe(fn,s);
  safe(renderFooter,s);
 });
 firstRender=false;
}

/* ---------- header and footer ---------- */
function renderHeader(s){
 const st=s.stats||{},tr=s.treasury||{},td=s.trader||{};
 if(s.links&&s.links.x){for(const id of ['#xlink','#pop-x','#foot-x']){const a=$(id);a.href=s.links.x;a.hidden=false}}
 if(st.last_block!=null&&st.last_block!==window.observedBlock){window.observedBlock=st.last_block;window.blockAdvancedAt=Date.now()}
 $('#pop-block').textContent=st.last_block==null?'…':fmt.int(st.last_block);
 $('#pop-pay').textContent=s.live?'Claims, burns and gold buys run on-chain':tr.token?'Payments paused':'Payments off';
 const paper=td.enabled!==true,t=paper?'Paper trading only':'Trading on, within its limits';
 $('#pop-trade').textContent=t;$('#paper-pill').textContent=t;$('#paper-pill').classList.toggle('is-on',!paper);
}
function renderFooter(s){const dc=s.disclosure||{};if(dc.real)$('#disc-real').textContent=dc.real;if(dc.not_real)$('#disc-not').textContent=dc.not_real}

/* ---------- events: a burn lands, a warning comes true, a lesson is learned ---------- */
const ev={qty:null,receipts:null,misses:null,lesson:null};
function allBurns(s){const tr=s.treasury||{},bp=tr.burn_program||{},m=new Map();
 const add=b=>{if(!b||!b.ts)return;const k=b.tx||('t'+b.ts);if(!m.has(k))m.set(k,{ts:Number(b.ts),usd:nz(b.usd??b.amount),qty:b.qty==null?null:Number(b.qty),tx:b.tx||null})};
 (tr.burn_history||[]).forEach(add);(bp.burns||[]).forEach(add);(tr.ledger||[]).filter(e=>e.kind==='burn').forEach(add);
 return [...m.values()].sort((a,b)=>a.ts-b.ts)}
function detectEvents(s){
 const tr=s.treasury||{},qty=nz(tr.burned_qty);
 if(ev.qty!=null&&qty>ev.qty+1)burnLanded(s,qty-ev.qty);
 ev.qty=qty;
 const rec=(s.receipts||[]).map(r=>r.token);ev.newReceipts=ev.receipts?rec.filter(t=>!ev.receipts.includes(t)):[];ev.receipts=rec;
 const ms=misses(s).map(r=>r.token);ev.newMisses=ev.misses?ms.filter(t=>!ev.misses.includes(t)):[];ev.misses=ms;
 const l0=(s.lessons||[])[0],lk=l0?l0.token+':'+l0.scored_at:null;if(ev.lesson&&lk&&lk!==ev.lesson)brainBurst();ev.lesson=lk;
}
function toast(html,where){const host=where||$('#toasts'),t=document.createElement('div');t.className='toast';t.innerHTML=html;host.append(t);setTimeout(()=>{t.classList.add('is-out');setTimeout(()=>t.remove(),400)},6000)}
function burnLanded(s,delta){
 const last=allBurns(s).pop(),url=last&&txUrl(s,last.tx);
 const msg=`<span class="toast__fire" aria-hidden="true"></span><span>+${fmt.qty(delta)} $WORM burned${last&&last.usd?' for '+fmt.usd(last.usd,{cents:true}):''}</span>${url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">tx</a>`:''}`;
 const band=$('#pulse');
 if(view==='overview'&&band&&band.dataset.built)toast(msg,band.querySelector('.burn__toasts'));else toast(msg);
 hood.ember=performance.now();
 for(const card of [band,$('#burn')]){if(!card)continue;card.classList.remove('is-flare');void card.offsetWidth;card.classList.add('is-flare');clearTimeout(card._flare);card._flare=setTimeout(()=>card.classList.remove('is-flare'),6000)}
 if(!window.motionPaused&&band){const burst=band.querySelector('.orb__burst');if(burst){const a=(Math.max(0,Math.min(1,nz(s.treasury.burned_qty)/WORM_SUPPLY))*360-90)*Math.PI/180;
  burst.style.setProperty('--bx',(50+44*Math.cos(a)).toFixed(1)+'%');burst.style.setProperty('--by',(50+44*Math.sin(a)).toFixed(1)+'%');
  burst.innerHTML=Array.from({length:12},(_,k)=>`<i style="--dx:${Math.round(Math.cos(k/12*6.283)*18)}px;--dy:${-40-Math.round((k*37)%31)}px;--d:${(k%4)*40}ms"></i>`).join('');
  burst.classList.remove('is-on');void burst.offsetWidth;burst.classList.add('is-on');setTimeout(()=>{burst.classList.remove('is-on');burst.innerHTML=''},1200)}}
}

/* ---------- Live: the hero ---------- */
function renderHero(s){
 const ch=s.character||{},stage=nz(ch.stage),stages=nz(ch.stages)||7;
 if(stage!==hood.stage)hood.stage=stage;
 const lo=isNum(ch.stage_floor_usd)?Number(ch.stage_floor_usd):STAGE_FLOORS[stage]||0,hi=ch.next_usd;
 $('#stage').textContent=`Stage ${stage+1}/${stages} · ${cap(ch.stage_name||'hatchling')}`;
 setMeter($('#stagebar'),hi?100*(nz(ch.usd)-lo)/Math.max(1,hi-lo):100);
 $('#grow').textContent=ch.demo?'a simulated character, not real money':hi?`grows as its treasury grows: ${fmt.usd(ch.usd)} held of ${fmt.usd(hi)}`:`fully grown: ${fmt.usd(ch.usd)} held`;
}

/* ---------- Live: the burn band ---------- */
function burnProgram(tr){const bp=tr.burn_program;if(!bp||!bp.started_ts||!bp.ends_ts)return null;const now=Date.now()/1000;return bp.active||now-bp.ends_ts<7*86400?bp:null}
const FLAME='<svg class="orb__flame" viewBox="0 0 40 56" aria-hidden="true"><path class="f1" d="M20 2C24 14 36 20 36 34a16 16 0 0 1-32 0C4 24 12 20 14 10c3 6 6 8 6 8s2-8 0-16Z"/><path class="f2" d="M20 22c3 6 9 9 9 17a9 9 0 0 1-18 0c0-6 5-8 6-14 1 3 3 4 3 4s1-4 0-7Z"/></svg>';
function renderPulse(s){
 const el=$('#pulse'),tr=s.treasury||{},rw=s.runway||{},rd=s.readiness||{},trader=s.trader||{};
 if(!tr.token){el.hidden=true;return}
 el.hidden=false;el.classList.remove('is-loading');
 if(!el.dataset.built){
  el.dataset.built='1';
  const embers=Array.from({length:10},(_,k)=>`<i style="--a:${k*36+8}deg;--r:${46+(k%3)*3}%;--d:${(k*.17).toFixed(2)}s"></i>`).join('');
  el.innerHTML=`<div class="burn__main">
 <div class="burn-orb" aria-hidden="true">
  <svg viewBox="0 0 200 200"><defs><linearGradient id="fire" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#ff6a2b"/><stop offset=".6" stop-color="#ffb25b"/><stop offset="1" stop-color="#ffe29a"/></linearGradient></defs>
   <circle cx="100" cy="100" r="88" class="orb__track"/><circle cx="100" cy="100" r="88" class="orb__arc" transform="rotate(-90 100 100)"/></svg>
  <div class="orb__embers">${embers}</div><div class="orb__burst"></div>
  <div class="orb__mid">${FLAME}<b data-n="pct">0%</b><span>of supply</span></div>
 </div>
 <div class="burn__copy">
  <span class="label">Burned forever</span>
  <h2 class="burn__num" id="pulse-title"><b data-n="qty">0</b> <em>$WORM</em></h2>
  <p class="burn__lede">bought back with <b data-n="usd">$0</b> of the fees it earned, and sent where no one can ever touch it.<span class="burn__owed" data-t="owed"></span></p>
  <div class="burn__slot"></div>
  <p class="burn__last" hidden><span></span> <a class="hit" target="_blank" rel="noopener noreferrer">see the transaction</a></p>
  <div class="burn__toasts" role="status" aria-live="polite"></div>
 </div>
</div>
<div class="burn__side">
 <div class="w-stat"><span class="label">Fees earned</span><span class="w-stat__value" data-n="earned">$0</span><span class="w-stat__note">all time, from $WORM trading</span></div>
 <div class="w-stat"><span class="label">Gold reserve</span><span class="w-stat__value" data-n="gold">$0</span><span class="w-stat__note">10% of every claim, held</span></div>
 <div class="w-stat"><span class="label">Runway</span><span class="w-stat__value"><span data-n="days">0</span><small>days</small></span><span class="w-stat__note" data-t="runway"></span></div>
 <div class="w-stat"><span class="label">Trading</span><span class="w-stat__value w-stat__value--word" data-t="trade">Paper only</span><span class="w-meter w-meter--quiet"><i data-m="ready"></i></span><span class="w-stat__note" data-t="tradeNote"></span></div>
 <a class="burn__more" href="#treasury">Where every dollar goes</a>
</div>`;
 }
 const q=k=>el.querySelector(`[data-n="${k}"]`),t=k=>el.querySelector(`[data-t="${k}"]`);
 const qty=nz(tr.burned_qty),share=Math.max(0,Math.min(1,qty/WORM_SUPPLY)),ready=nz(rd.score),readyAt=nz(rd.ready_at)||80;
 setNum(q('pct'),100*share,v=>v.toFixed(2)+'%');setNum(q('qty'),qty,fmt.qty);setNum(q('usd'),nz(tr.burned_total),v=>fmt.usd(v));
 setNum(q('earned'),nz(tr.claimed_total),v=>fmt.usd(v));setNum(q('gold'),tr.gold_usd==null?null:nz(tr.gold_usd),v=>fmt.usd(v));
 setNum(q('days'),rw.runway_days_no_income==null?null:nz(rw.runway_days_no_income),fmt.int);
 t('owed').textContent=nz(tr.owed_to_burn)>0?` ${fmt.usd(tr.owed_to_burn,{cents:true})} more is waiting for the next burn.`:'';
 t('runway').textContent=`of costs covered with no new income · ${fmt.usd(rw.treasury_usd)} treasury`;
 t('trade').textContent=trader.enabled?'Trading live':'Paper only';
 t('tradeNote').textContent=trader.enabled?`readiness ${ready} of ${readyAt} needed`:`real trades wait for proof · ${ready} of ${readyAt} ready`;
 setMeter(el.querySelector('[data-m="ready"]'),100*ready/readyAt);
 /* the ring: its arc is the share of the supply burned */
 const arc=el.querySelector('.orb__arc'),c=2*Math.PI*88,len=Math.max(share*c,share>0?4:0),apply=()=>{arc.style.strokeDasharray=len.toFixed(2)+' '+(c-len+1).toFixed(2)};
 if(arc._seen||window.motionPaused)apply();else{arc._pend=apply;seenIO.observe(arc)}
 /* the slot: the surplus-burn week while it runs, otherwise the next regular burn filling up */
 const bp=burnProgram(tr),slot=el.querySelector('.burn__slot');
 if(bp){if(changed('weekLive',[bp,tr.burn_max_usd,Math.floor(Date.now()/60000),innerWidth<600]))slot.innerHTML=weekTrack('wk-live',s,bp,{})}
 else if(changed('nextLive',[tr.owed_to_burn,tr.burn_min_usd])){const min=nz(tr.burn_min_usd)||5,p=Math.min(100,100*nz(tr.owed_to_burn)/min);
  slot.innerHTML=`<div class="next-burn"><div class="next-burn__head"><span>Next burn</span><span>${savedLine(tr.owed_to_burn,min,'in the next small buy')}</span></div><span class="w-meter w-meter--ember${p>=80?' is-close':''}"><i data-m="next"></i></span><p class="next-burn__foot">a small buy-and-burn every few hours, whenever enough fees are saved</p></div>`;
  setMeter(slot.querySelector('[data-m="next"]'),p)}
 const lastBurn=allBurns(s).pop(),last=el.querySelector('.burn__last');
 if(lastBurn){last.hidden=false;last.querySelector('span').textContent='last burn '+ago(lastBurn.ts)+' ·';const a=last.querySelector('a'),u=txUrl(s,lastBurn.tx);a.hidden=!u;if(u)a.href=u}else last.hidden=true;
}

/* ---------- the surplus-burn week: sparks where burns landed, the released share, an even unlabelled schedule ---------- */
const trackData={};
function sparkH(usd,max,big){return Math.round((big?12:10)+(big?30:22)*Math.sqrt(Math.max(0,Math.min(1,nz(usd)/Math.max(.01,max)))))}
function weekTrack(id,s,bp,o){
 const tr=s.treasury||{},start=Number(bp.started_ts),end=Number(bp.ends_ts),span=Math.max(1,end-start),now=Date.now()/1000;
 const days=Math.max(1,Math.round(nz(bp.days)||span/86400)),active=!!bp.active&&now<end,big=!!o.big;
 const burns=(bp.burns||[]).filter(b=>b&&b.ts).map(b=>({ts:Number(b.ts),usd:nz(b.usd),qty:b.qty==null?null:Number(b.qty),tx:b.tx||null})).sort((a,b)=>a.ts-b.ts);
 const max=nz(tr.burn_max_usd)||Math.max(1,...burns.map(b=>b.usd)),x=ts=>Math.max(0,Math.min(100,100*(ts-start)/span));
 const rel=nz(bp.total_usd)>0?Math.min(100,100*nz(bp.released_usd)/nz(bp.total_usd)):0,day=Math.min(days,Math.max(1,Math.ceil((Math.min(now,end)-start)/86400)));
 const seen=trackData[id]?trackData[id].seen:null,fresh=seen?burns.filter(b=>!seen.has(b.ts)):[];
 trackData[id]={burns,start,span,seen:new Set(burns.map(b=>b.ts))};
 const burnedQty=bp.burned_qty!=null?nz(bp.burned_qty):burns.reduce((a,b)=>a+nz(b.qty),0);
 const narrow=innerWidth<600;
 let marks='';
 for(let d=0;d<days;d++)marks+=`<span class="week__day" style="left:${x(start+d*86400).toFixed(2)}%"><span>${narrow&&!big&&d%2?'':'D'+(d+1)}</span></span>`;
 if(active&&nz(bp.interval_s)>0){const iv=nz(bp.interval_s);for(let t=end-iv/2;t>now+iv*.25&&t>start;t-=iv)marks+=`<span class="spark spark--planned" style="left:${x(t).toFixed(2)}%"></span>`}
 marks+=burns.map((b,i)=>`<span class="spark${fresh.includes(b)?' is-fresh':''}" data-i="${i}" style="left:${x(b.ts).toFixed(2)}%;--h:${sparkH(b.usd,max,big)}px"></span>`).join('');
 if(active)marks+=`<span class="week__now" style="left:${x(now).toFixed(2)}%"><span>now</span></span>`;
 const perDay=big?Array.from({length:days},(_,d)=>burns.filter(b=>b.ts>=start+d*86400&&b.ts<start+(d+1)*86400).reduce((a,b)=>a+b.usd,0)):null;
 const summary=`Surplus burn week: ${burns.length} small burns so far, ${fmt.usd(bp.burned_usd)} of ${fmt.usd(bp.total_usd)} burned, ${active?'ends':'ended'} ${fmt.utc(end)}. Use the arrow keys to read each burn.`;
 const head=active?`<span class="week__title">Surplus burn week</span><span><b>${fmt.usd(bp.burned_usd)}</b> of ${fmt.usd(bp.total_usd)} burned · day ${day} of ${days}</span>`
  :`<span class="week__title">Surplus burn week complete</span><span><b>${fmt.usd(bp.burned_usd||bp.total_usd)}</b> burned ${fmt.qty(burnedQty)} $WORM</span>`;
 return `<div class="week${big?' week--big':''}">
 ${o.noHead?'':`<div class="week__head">${head}</div>`}
 <div class="week__wrap"><div class="week__track" id="${id}" tabindex="0" role="group" aria-roledescription="burn timeline" aria-label="${esc(summary)}">
  <span class="week__released" style="--rel:${rel.toFixed(1)}%"></span>${marks}</div><div class="spark-tip" role="tooltip" hidden></div></div>
 ${perDay?`<div class="week__days">${perDay.map((u,d)=>`<span style="left:${(100*(d+.5)/days).toFixed(2)}%">${u?fmt.usd(u):'·'}</span>`).join('')}</div>`:''}
 <div class="week__foot"><span>a small buy-and-burn every few hours</span><span>${active?'ends':'ended'} ${fmt.utc(end)}</span></div>
 <ol class="sr-only">${burns.map(b=>`<li>${fmt.utc(b.ts)}: ${fmt.usd(b.usd,{cents:true})} bought ${fmt.qty(b.qty)} $WORM</li>`).join('')}</ol>
</div>`}
/* burn history since launch, with the same sparks */
function historyTrack(id,s,burns){
 const first=burns.length?burns[0].ts:Date.now()/1000,launch=nz((s.launch||{}).at)||first,start=Math.min(launch,first),end=Date.now()/1000,span=Math.max(1,end-start);
 const max=Math.max(1,...burns.map(b=>b.usd)),x=ts=>Math.max(0,Math.min(100,100*(ts-start)/span));
 const seen=trackData[id]?trackData[id].seen:null,fresh=seen?burns.filter(b=>!seen.has(b.ts)):[];
 trackData[id]={burns,start,span,seen:new Set(burns.map(b=>b.ts))};
 return `<div class="week week--hist"><div class="week__wrap"><div class="week__track" id="${id}" tabindex="0" role="group" aria-roledescription="burn timeline" aria-label="${esc(`${burns.length} burns since ${fmt.utc(start)}. Use the arrow keys to read each burn.`)}">
 ${burns.map((b,i)=>`<span class="spark${fresh.includes(b)?' is-fresh':''}" data-i="${i}" style="left:${x(b.ts).toFixed(2)}%;--h:${sparkH(b.usd,max,true)}px"></span>`).join('')}</div><div class="spark-tip" role="tooltip" hidden></div></div>
 <div class="week__foot"><span>${fmt.utc(start)}</span><span>now</span></div>
 <ol class="sr-only">${burns.map(b=>`<li>${fmt.utc(b.ts)}: ${fmt.usd(b.usd,{cents:true})}${b.qty?` bought ${fmt.qty(b.qty)} $WORM`:''}</li>`).join('')}</ol></div>`}
/* the tooltip: hover, tap or arrow keys pick the nearest burn */
function tipShow(track,i){const d=trackData[track.id];if(!d||!d.burns.length)return;i=Math.max(0,Math.min(d.burns.length-1,i));track._i=i;const b=d.burns[i],tip=track.parentElement.querySelector('.spark-tip'),u=txUrl(lastState||{},b.tx);
 track.querySelectorAll('.spark.is-on').forEach(x=>x.classList.remove('is-on'));const sp=track.querySelector(`.spark[data-i="${i}"]`);if(sp)sp.classList.add('is-on');
 tip.innerHTML=`<b>${fmt.utc(b.ts)}</b><span>${fmt.usd(b.usd,{cents:true})}${b.qty?` for ${fmt.qty(b.qty)} $WORM`:''}</span>${u?`<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">tx</a>`:''}`;
 tip.hidden=false;const W=track.clientWidth,left=Math.max(0,Math.min(W-tip.offsetWidth,W*(b.ts-d.start)/d.span-tip.offsetWidth/2));tip.style.left=left+'px'}
function tipHide(track){const tip=track.parentElement.querySelector('.spark-tip');if(tip&&!tip.matches(':hover'))tip.hidden=true;track.querySelectorAll('.spark.is-on').forEach(x=>x.classList.remove('is-on'))}
function nearest(track,clientX){const d=trackData[track.id];if(!d||!d.burns.length)return -1;const r=track.getBoundingClientRect(),ts=d.start+d.span*(clientX-r.left)/r.width;let best=0;d.burns.forEach((b,i)=>{if(Math.abs(b.ts-ts)<Math.abs(d.burns[best].ts-ts))best=i});
 return Math.abs(r.width*(d.burns[best].ts-ts)/d.span)<28?best:-1}
document.addEventListener('pointermove',e=>{const t=e.target.closest('.week__track');if(!t||e.pointerType==='touch')return;const i=nearest(t,e.clientX);if(i>=0)tipShow(t,i)});
document.addEventListener('pointerout',e=>{const t=e.target.closest('.week__track');if(t&&!t.contains(e.relatedTarget)&&!(e.relatedTarget&&e.relatedTarget.closest&&e.relatedTarget.closest('.spark-tip'))){clearTimeout(t._h);t._h=setTimeout(()=>tipHide(t),1200)}});
document.addEventListener('click',e=>{const t=e.target.closest('.week__track');if(t){const i=nearest(t,e.clientX);if(i>=0){clearTimeout(t._h);tipShow(t,i)}else tipHide(t);return}if(!e.target.closest('.spark-tip'))$$('.spark-tip').forEach(x=>x.hidden=true)});
document.addEventListener('keydown',e=>{const t=e.target.closest&&e.target.closest('.week__track');if(!t)return;const d=trackData[t.id];if(!d||!d.burns.length)return;
 if(['ArrowRight','ArrowLeft','Home','End'].includes(e.key)){e.preventDefault();const i=t._i==null?(e.key==='ArrowLeft'||e.key==='End'?d.burns.length-1:0):e.key==='Home'?0:e.key==='End'?d.burns.length-1:t._i+(e.key==='ArrowRight'?1:-1);tipShow(t,i)}else if(e.key==='Escape')tipHide(t)});
document.addEventListener('focusout',e=>{const t=e.target.closest&&e.target.closest('.week__track');if(t&&!(e.relatedTarget&&e.relatedTarget.closest&&e.relatedTarget.closest('.spark-tip')))tipHide(t)});

/* ---------- Live: the worm at work ---------- */
function syncDig(s){if(s.dig&&s.dig.length&&(!dig.token||s.dig[0].token!==dig.token||Object.keys(dig.steps).length<s.dig.length)){dig={token:s.dig[0].token,steps:{}};s.dig.forEach(e=>dig.steps[e.step]=e);renderDig();updateLive()}}
function renderAtWork(s){
 $('#liveband').classList.toggle('noscreen',s.screen_on===false);$('#screen-card').hidden=s.screen_on===false;
 const tk=s.ticker||[],run=$('#ticker'),bucket=Math.floor(Date.now()/30000);
 if(!changed('tape',[tk.map(t=>t.token),bucket]))return;
 if(!tk.length){run.textContent='waiting for new launches…';run.classList.remove('is-loop');return}
 const items=tk.map(t=>`<span class="tape__item"><b>$${esc(t.symbol||String(t.token).slice(0,8))}</b> ${esc(t.name||'')} <span>· ${esc(t.pair_symbol||'')} · ${ago(t.ts)}</span></span>`).join('');
 run.innerHTML=`<span class="tape__set">${items}</span><span class="tape__set" aria-hidden="true">${items}</span>`;run.classList.add('is-loop');
}

/* ---------- the mind: one brain, moved into whichever view is showing ---------- */
const AREAS=[{id:'observe',name:'Observe',x:26,y:30},{id:'assess',name:'Assess',x:66,y:30},{id:'learn',name:'Learn',x:44,y:67},{id:'ready',name:'Readiness',x:83,y:75}];
const LEARN_ROUTE='M158,248 C270,157 360,215 453,258 S664,321 819,223';
let learningFocus='learn';
function buildBrain(){
 const brain='M90,250 C90,120 230,50 480,45 C740,40 940,110 940,240 C940,330 860,380 720,395 C560,410 380,400 260,385 C150,370 90,320 90,250 Z';
 const lower='M175,368 C290,342 466,359 652,377 C690,402 633,449 474,463 C335,484 212,459 175,422 Z';
 const random=n=>{const x=Math.sin(n*127.1+17)*43758.54;return x-Math.floor(x)};
 const nodes=Array.from({length:96},(_,i)=>({x:115+random(i)*795,y:66+random(i+130)*380}));
 const area=(x,y)=>{const px=(x-45)/940*100,py=(y-15)/535*100;return AREAS.reduce((a,b)=>Math.hypot(px-a.x,py-a.y)<Math.hypot(px-b.x,py-b.y)?a:b).id};
 let net='',k=0;for(let i=0;i<nodes.length;i++){const a=nodes[i];for(let j=i+1;j<nodes.length;j++){const b=nodes[j],d=Math.hypot(a.x-b.x,a.y-b.y);if(d<95)net+=`<path class="neural-edge" d="M${a.x.toFixed(1)},${a.y.toFixed(1)} L${b.x.toFixed(1)},${b.y.toFixed(1)}" style="--o:${(.12*(1-d/95)).toFixed(3)};--node-delay:${(-(k++)*.13).toFixed(2)}s"/>`}}
 const dots=nodes.map((n,i)=>`<circle class="neural-node${i%13===0?' hub':''}" data-area="${area(n.x,n.y)}" cx="${n.x.toFixed(1)}" cy="${n.y.toFixed(1)}" r="${i%13===0?3.5:1.6}" style="--node-delay:${(-i*.173).toFixed(2)}s;--node-duration:${(2.8+(i%7)*.31).toFixed(2)}s"/>`).join('');
 const packets=[LEARN_ROUTE,'M242,393 C322,360 384,422 453,396 C520,374 571,421 619,403','M500,46 C492,140 475,220 452,300 C430,340 395,350 375,389'].map((d,i)=>`<path class="neural-packet" d="${d}" pathLength="100" style="--packet-delay:${-i*2.1}s"/>`).join('');
 const stage=document.createElement('div');stage.className='brain-stage';stage.dataset.activeArea=learningFocus;
 stage.innerHTML=`<div class="brain-sculpture"><svg viewBox="45 15 940 535" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="A schematic brain-shaped network for the worm's learning loop. The dots are decorative, not measured neurons.">
 <defs><linearGradient id="brainwash" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#15261c"/><stop offset="1" stop-color="#080e0a"/></linearGradient><clipPath id="learningclip"><path d="${brain}"/><path d="${lower}"/></clipPath></defs>
 <path class="brain-part" d="M702,350 C707,415 698,469 714,524 L744,524 C736,466 751,422 753,366"/><ellipse class="brain-part" cx="841" cy="407" rx="92" ry="55"/><path class="brain-part" d="${lower}" fill="url(#brainwash)"/><path class="brain-outline" d="${brain}" fill="url(#brainwash)"/>
 <g class="brain-folds"><path d="M500,46 C492,140 475,220 452,300 C430,340 395,350 375,389"/><path d="M760,62 C770,150 780,240 790,340"/><path d="M142,279 C217,222 307,299 372,275 C449,247 520,285 602,294 C697,307 744,355 829,334"/><path d="M155,159 C210,108 279,197 337,147 C380,108 400,136 426,173"/><path d="M490,108 C544,66 591,135 650,101 C688,78 720,105 742,148"/><path d="M242,393 C322,360 384,422 453,396 C520,374 571,421 619,403"/>${[386,404,422].map(y=>`<path d="M776,${y} Q843,${y-25} 909,${y}"/>`).join('')}</g>
 <g clip-path="url(#learningclip)" class="brain-signals">${net}${dots}<path class="brain-flow" d="${LEARN_ROUTE}"/>${packets}</g></svg></div>
 <div class="brain-hotspots" role="group" aria-label="Explore how the worm learns">${AREAS.map((a,i)=>`<button type="button" class="brain-zone" data-learning-zone="${a.id}" aria-pressed="${a.id===learningFocus}" style="left:${a.x}%;top:${a.y}%"><span class="zone-dot">${String(i+1).padStart(2,'0')}</span><span>${a.name}</span></button>`).join('')}</div>`;
 return stage}
const brainStage=buildBrain();
function moveBrain(v){const slot=$(v==='learning'?'#mind-learning':'#mind-live');if(slot&&brainStage.parentElement!==slot)slot.append(brainStage)}
function brainBurst(){if(window.motionPaused)return;const g=brainStage.querySelector('.brain-signals');if(!g)return;const p=document.createElementNS('http://www.w3.org/2000/svg','path');p.setAttribute('d',LEARN_ROUTE);p.setAttribute('pathLength','100');p.setAttribute('class','neural-packet neural-packet--burst');g.append(p);setTimeout(()=>p.remove(),1700)}
const AREA_COPY={observe:['01','Observe','It reads every launch and graduation on Robinhood Chain.'],assess:['02','Assess','It scores each graduated token with rules it can explain.'],
 learn:['03','Learn','A day later it checks what happened and re-weights its rules.'],ready:['04','Readiness','Real trades stay off until the evidence says otherwise.']};
function selectArea(id){if(!AREAS.some(a=>a.id===id))return;learningFocus=id;brainStage.dataset.activeArea=id;
 brainStage.querySelectorAll('[data-learning-zone]').forEach(z=>z.setAttribute('aria-pressed',String(z.dataset.learningZone===id)));
 brainStage.classList.remove('brain-response');void brainStage.offsetWidth;brainStage.classList.add('brain-response');
 delete keys.inspector;delete keys.caption;if(lastState){safe(renderInspector,lastState);safe(renderMindLive,lastState)}}
brainStage.addEventListener('click',e=>{const z=e.target.closest('[data-learning-zone]');if(z)selectArea(z.dataset.learningZone)});
const finePointer=matchMedia('(pointer:fine)');
brainStage.addEventListener('pointermove',e=>{if(window.motionPaused||e.pointerType!=='mouse'||!finePointer.matches)return;const r=brainStage.getBoundingClientRect(),x=Math.max(-1,Math.min(1,(e.clientX-r.left)/r.width*2-1)),y=Math.max(-1,Math.min(1,(e.clientY-r.top)/r.height*2-1));
 brainStage.style.setProperty('--brain-tilt-x',(-y*5)+'deg');brainStage.style.setProperty('--brain-tilt-y',(x*7)+'deg')});
brainStage.addEventListener('pointerleave',()=>{brainStage.style.setProperty('--brain-tilt-x','0deg');brainStage.style.setProperty('--brain-tilt-y','0deg')});

/* the honest numbers every view shares */
function baseRate(s){const acc=((s.readiness||{}).parts||[]).find(p=>p.id==='accuracy');if(acc&&isNum(acc.base_rate_pct))return Number(acc.base_rate_pct);
 const c=(s.brain||{}).counts||{},bad=nz(c.rugged)+nz(c.dumped),all=bad+nz(c.flat)+nz(c.grew)+nz(c.unknown);return all?Math.round(100*bad/all):null}
function healthyChecked(s){const row=((s.brain||{}).scorecard||{})['looks healthy'];if(row){const n=Object.values(row).reduce((a,c)=>a+nz(c),0);if(n)return n}return nz(((s.stats||{}).verdicts||{})['looks healthy'])||null}
function renderMindLive(s){
 const sc=s.scout||{},rd=s.readiness||{},td=s.trader||{},base=baseRate(s),hc=healthyChecked(s),ready=nz(rd.score),readyAt=nz(rd.ready_at)||80;
 const el=$('#mind-stats');
 if(!el.dataset.built){el.dataset.built='1';el.innerHTML=`
 <div class="w-stat w-stat--l"><span class="label">Warnings that came true</span><span class="w-stat__value"><span data-n="called">0</span><small data-t="checked"></small></span><span class="w-stat__note" data-t="calledNote"></span></div>
 <div class="w-stat w-stat--l"><span class="label">Healthy calls that went wrong</span><span class="w-stat__value neg"><span data-n="missed">0</span><small data-t="hc"></small></span><span class="w-stat__note">shown on purpose: the misses count as much as the hits</span></div>
 <div class="w-stat w-stat--l"><span class="label">Trading</span><span class="w-stat__value w-stat__value--word" data-t="trade">Paper only</span><span class="w-meter"><i data-m="ready"></i></span><span class="w-stat__note" data-t="readyNote"></span></div>
 <a class="w-btn w-btn--secondary" href="#learning">See how it learns</a>`}
 setNum(el.querySelector('[data-n="called"]'),sc.called,fmt.int);setNum(el.querySelector('[data-n="missed"]'),sc.missed,fmt.int);
 el.querySelector('[data-t="checked"]').textContent=sc.checked_warnings?'of '+fmt.int(sc.checked_warnings):'';
 el.querySelector('[data-t="hc"]').textContent=hc?`of ${fmt.int(hc)} checked`:'';
 el.querySelector('[data-t="calledNote"]').textContent=`${sc.warn_precision!=null?sc.warn_precision+'% · ':''}${base!=null?`most launches go bad anyway: ${base}% of all it checked went bad`:'checked 24 hours after each warning'}`;
 el.querySelector('[data-t="trade"]').textContent=td.enabled?'Trading live':'Paper only';
 el.querySelector('[data-t="readyNote"]').textContent=`readiness ${ready} of ${readyAt} · real trades wait for proof`;
 setMeter(el.querySelector('[data-m="ready"]'),100*ready/readyAt);
 const a=AREA_COPY[learningFocus];$('#mind-caption').innerHTML=`<b>${a[0]} · ${a[1]}</b> ${a[2]}`;
}
function learningInspector(s,id){
 const st=s.stats||{},b=s.brain||{},sc=s.scout||{},rd=s.readiness||{},lab=s.lab||{},vd=st.verdicts||{};
 const pair=(a,v)=>`<div><dt>${a}</dt><dd>${esc(typeof v==='number'?fmt.int(v):v??'…')}</dd></div>`;
 const moving=(b.rules||[]).filter(r=>Math.abs(r.weight-1)>.001);
 const c={observe:['01 / Observe','Read the chain','New launches and graduations become the evidence for each assessment.',st.launches_24h,'launches in the last 24 hours',pair('Graduations · 24h',st.grads_24h)+pair('Waiting to be screened',st.queued)+pair('Last block read',st.last_block==null?'…':fmt.int(st.last_block)),'#scans','Look up any token'],
  assess:['02 / Assess','Explain every call','Each token gets a rule-based score with its reasons. What happens next is tracked separately.',st.scored,'tokens screened',pair('Avoid',vd.avoid??0)+pair('Mixed',vd.mixed??0)+pair('Looks healthy',vd['looks healthy']??0),'#learning/rules','See every rule'],
  learn:['03 / Learn','Learn from the outcome','Each checked outcome nudges the rules. The record keeps the misses next to the hits.',b.resolved,'verdicts checked',pair('Warnings that came true',sc.called)+pair('Warnings checked',sc.checked_warnings)+pair('Healthy calls that went wrong',sc.missed)+pair('Rules moved from where they started',moving.length),'#learning/lessons','Read the latest lessons'],
  ready:['04 / Readiness','Earn the next step','Readiness combines the strategy lab, how often warnings came true, runway and surplus. Trading stays paper-only until it passes.',rd.score,`readiness out of 100 · ${nz(rd.ready_at)||80} needed`,pair('Resolved strategy cases',lab.cases_resolved)+pair('Trading',(s.trader||{}).enabled?'On, within limits':'Paper only'),'#learning/paper','See paper trading']}[id];
 const parts=id==='ready'?`<div class="inspector__parts">${(rd.parts||[]).map(p=>`<div><span>${esc(cap(p.label))}</span><b>${num(p.score)}</b><span class="w-meter"><i style="--w:${Math.max(0,Math.min(100,nz(p.score)))}%"></i></span></div>`).join('')}</div><p class="w-note">Next: ${esc(rd.next||'waiting for enough evidence')}</p>`:'';
 return `<span class="label">${c[0]}</span><h3 class="inspector__title">${c[1]}</h3><p class="inspector__lede">${c[2]}</p><div class="w-stat w-stat--l"><span class="w-stat__value">${isNum(c[3])?fmt.int(c[3]):'…'}</span><span class="w-stat__note">${c[4]}</span></div><dl class="facts">${c[5]}</dl>${parts}<a class="w-btn w-btn--secondary" href="${c[6]}">${c[7]}</a>`}
function renderInspector(s){
 if(changed('inspector',[learningFocus,s.stats&&[s.stats.launches_24h,s.stats.grads_24h,s.stats.queued,s.stats.scored,s.stats.verdicts,s.stats.last_block],s.scout,s.readiness,(s.brain||{}).resolved,(s.lab||{}).cases_resolved,((s.brain||{}).rules||[]).map(r=>r.weight)])){
  const focused=document.activeElement&&$('#learning-inspector').contains(document.activeElement);$('#learning-inspector').innerHTML=learningInspector(s,learningFocus);if(focused)$('#learning-inspector a')?.focus({preventScroll:true})}
 const st=s.stats||{},vd=st.verdicts||{},el=$('#brain-counters');
 if(!el.dataset.built){el.dataset.built='1';el.innerHTML=[['scored','Screened'],['queued','In queue'],['avoid','Avoid calls'],['launches','Launches · 24h'],['grads','Graduations · 24h']].map(([k,l])=>`<div class="w-stat w-stat--s"><span class="label">${l}</span><span class="w-stat__value" data-n="${k}">0</span></div>`).join('')}
 for(const [k,v] of [['scored',st.scored],['queued',st.queued],['avoid',vd.avoid??0],['launches',st.launches_24h],['grads',st.grads_24h]])setNum(el.querySelector(`[data-n="${k}"]`),v,fmt.int);
}

/* ---------- receipts and misses ---------- */
function receiptAfter(sec){if(sec==null)return 'within a day';if(sec<3600)return Math.max(1,Math.round(sec/60))+' min later';return (sec/3600).toFixed(sec<36000?1:0).replace(/\.0$/,'')+' h later'}
function misses(s){const out=[],seen=new Set(),feed=new Map((s.feed||[]).map(t=>[t.token,t]));
 const add=o=>{if(!o||!o.token||seen.has(o.token)||!o.symbol)return;seen.add(o.token);const f=feed.get(o.token);
  if(!o.reason&&f){const good=(f.reasons||[]).map(splitReason).filter(r=>r.pts>0).sort((a,b)=>b.pts-a.pts)[0];if(good)o.reason=good.text}out.push(o)};
 (s.misses||[]).forEach(o=>add({...o}));
 const bad=o=>o&&o.verdict==='looks healthy'&&/rugged|dumped/.test(o.outcome||'');
 ((s.brain||{}).outcomes||[]).filter(bad).forEach(o=>add({token:o.token,symbol:o.symbol,name:o.name,score:o.score,outcome:o.outcome,change_pct:o.change_pct,scored_at:o.scored_at}));
 (s.lessons||[]).filter(bad).forEach(o=>add({token:o.token,symbol:o.symbol,name:o.name,score:o.score,outcome:o.outcome,change_pct:o.change_pct,scored_at:o.scored_at}));
 (s.feed||[]).filter(bad).forEach(o=>add({token:o.token,symbol:o.symbol,name:o.name,score:o.score,outcome:o.outcome,change_pct:o.change_pct,scored_at:o.scored_at}));
 return out.sort((a,b)=>nz(b.scored_at)-nz(a.scored_at))}
function receiptCard(r,kind,isNew){
 const miss=kind==='miss',fall=r.change_pct==null?'':pct(r.change_pct),when=receiptAfter(r.after_s),why=String(r.reason||'').replace(/\s+/g,' ').trim();
 const post=miss?`Got it wrong: $${r.symbol}. WORM said looks healthy (${r.score}/100) and it ${r.outcome}${fall?' '+fall:''}. The misses are shown too. wormdig.io`
  :`Called it: $${r.symbol}. WORM said avoid (${r.score}/100) when it graduated${why?`: "${why}"`:''}. ${when==='within a day'?'Within a day':cap(when)}: ${fall||r.outcome}. wormdig.io`;
 return `<article class="receipt${miss?' receipt--miss':''}${isNew?' is-new':''}" data-key="${esc(r.token)}"><span class="receipt__stamp" aria-hidden="true">${miss?'Got it wrong':'Called it'}</span>
 <header class="receipt__head"><b>$${esc(r.symbol)}</b><strong class="neg">${esc(fall||r.outcome)}</strong></header>
 <p class="receipt__call"><span class="w-verdict ${miss?'healthy':'avoid'}"><b>${num(r.score)}</b>${miss?'looks healthy':'avoid'}</span><span>${esc(r.outcome||'')} ${miss?'':esc(when)}</span></p>
 ${why?`<p class="receipt__why">${miss?'it liked: ':''}“${esc(why)}”</p>`:''}${copyBtn(post)}</article>`}
function renderReceiptsLive(s){
 const list=(s.receipts||[]).filter(r=>r&&r.symbol).slice(0,3),sect=$('#receipts-live');sect.hidden=!list.length;if(!list.length)return;
 if(!changed('receiptsLive',list.map(r=>[r.token,Math.round(nz(r.change_pct))])))return;
 $('#receipts-live-list').innerHTML=list.map(r=>receiptCard(r,'call',(ev.newReceipts||[]).includes(r.token))).join('');
}

/* ================= Learning ================= */
function renderReport(s){
 const sc=s.scout||{},b=s.brain||{},base=baseRate(s),hc=healthyChecked(s),el=$('#report-stats');
 if(!el.dataset.built){el.dataset.built='1';el.innerHTML=`
 <div class="w-stat w-stat--l"><span class="label">Warnings that came true</span><span class="w-stat__value"><span data-n="called">0</span><small data-t="of"></small></span><span class="w-stat__note" data-t="prec"></span></div>
 <div class="w-stat w-stat--l"><span class="label">Healthy calls that went wrong</span><span class="w-stat__value neg"><span data-n="missed">0</span><small data-t="hc"></small></span><span class="w-stat__note">shown on purpose</span></div>
 <div class="w-stat w-stat--l"><span class="label">Verdicts checked</span><span class="w-stat__value" data-n="resolved">0</span><span class="w-stat__note">each one 24 hours after the call</span></div>
 <div class="w-stat w-stat--l"><span class="label">Base rate</span><span class="w-stat__value"><span data-n="base">0</span><small>%</small></span><span class="w-stat__note">of all launches it checked went bad anyway: the bar a warning has to beat</span></div>`}
 setNum(el.querySelector('[data-n="called"]'),sc.called,fmt.int);setNum(el.querySelector('[data-n="missed"]'),sc.missed,fmt.int);
 setNum(el.querySelector('[data-n="resolved"]'),b.resolved,fmt.int);setNum(el.querySelector('[data-n="base"]'),base,v=>String(Math.round(v)));
 el.querySelector('[data-t="of"]').textContent=sc.checked_warnings?'of '+fmt.int(sc.checked_warnings):'';el.querySelector('[data-t="hc"]').textContent=hc?'of '+fmt.int(hc):'';
 el.querySelector('[data-t="prec"]').textContent=sc.warn_precision!=null?`${sc.warn_precision}% of checked warnings: the token rugged or dumped`:'checked 24 hours after each warning';
 const card=b.scorecard||{};if(!changed('scorecard',card))return;
 const segs=[['rugged','Rugged'],['dumped','Dumped'],['flat','Flat'],['grew','Grew'],['unknown','Unknown']];
 const rows=['avoid','mixed','looks healthy'].filter(v=>card[v]).map(v=>{const r=card[v],tot=segs.reduce((a,[k])=>a+nz(r[k]),0)||1,bad=nz(r.rugged)+nz(r.dumped);
  const right=v==='avoid'?`${Math.round(100*bad/tot)}% went bad`:v==='looks healthy'?`${Math.round(100*nz(r.grew)/tot)}% grew`:`${Math.round(100*bad/tot)}% went bad`;
  return `<div class="sc-row"><div class="sc-row__name"><span class="w-verdict ${vcls(v)}">${esc(v)}</span><small>${fmt.int(tot)} checked</small></div><div class="sc-bar" role="img" aria-label="${esc(`${v}: ${segs.map(([k,l])=>`${nz(r[k])} ${l.toLowerCase()}`).join(', ')}`)}">${segs.map(([k,l])=>{const w=100*nz(r[k])/tot;return w>0?`<span class="sc-seg sc-seg--${k}" style="width:${w.toFixed(2)}%" title="${l}: ${nz(r[k])}">${w>=7?fmt.int(r[k]):''}</span>`:''}).join('')}</div><span class="sc-row__right${v==='looks healthy'?' neg':''}">${right}</span></div>`}).join('');
 $('#scorecard').innerHTML=rows?`<div class="sc-head"><span class="label">What happened after each kind of call</span><span class="sc-legend">${segs.map(([k,l])=>`<span><i class="sc-seg--${k}"></i>${l}</span>`).join('')}</span></div>${rows}<p class="w-note">A warning “comes true” when the token rugs or dumps within a day. Most launches go bad anyway, so compare every rate with the base rate.</p>`:'';
}
function renderLanes(s){
 const rec=(s.receipts||[]).filter(r=>r&&r.symbol),ms=misses(s),sc=s.scout||{};
 if(!changed('lanes',[rec.map(r=>r.token+Math.round(nz(r.change_pct))),ms.map(r=>r.token),sc.called,sc.missed,innerWidth<768]))return;
 $('#lane-called-n').textContent=sc.called?fmt.int(sc.called)+' in all':'';$('#lane-missed-n').textContent=sc.missed?fmt.int(sc.missed)+' in all':'';
 $('#lane-called').innerHTML=rec.length?rec.slice(0,3).map(r=>receiptCard(r,'call',(ev.newReceipts||[]).includes(r.token))).join(''):'<p class="w-empty">The first warnings are checked 24 hours after they are made.</p>';
 $('#lane-missed').innerHTML=ms.length?ms.slice(0,3).map(r=>receiptCard(r,'miss',(ev.newMisses||[]).includes(r.token))).join(''):`<p class="w-empty">${sc.missed?`${fmt.int(sc.missed)} healthy calls went wrong in all. None of them is in the recent records the page receives right now; they show here as new ones are checked.`:'No healthy call has gone wrong yet.'}</p>`;
}
function renderLessons(s){
 const ls=s.lessons||[],n=more.lessons;if(!changed('lessons',[ls,n,Math.floor(Date.now()/60000)]))return;
 const tagCls=t=>t==='called it'?'good':t==='missed it'?'warn':'';
 $('#lessons').innerHTML=ls.length?ls.slice(0,n).map(x=>{const bad=/rugged|dumped/.test(x.outcome||''),up=x.up||[],down=x.down||[];
  return `<li class="w-row w-row--static w-row--text" data-key="${esc((x.token||'')+(x.scored_at||''))}"><span class="w-row__main"><span class="w-row__title">$${esc(x.symbol||'?')} · <span class="${bad?'neg':x.outcome==='grew'?'pos':''}">${esc(x.outcome)} ${x.change_pct!=null?pct(x.change_pct):''}</span> <span class="w-row__muted">· was ${esc(x.verdict)} ${num(x.score)}</span></span>
  <span class="w-row__text">${up.length?`trusts more: ${esc(up.map(ruleName).join(', '))}`:''}${up.length&&down.length?' · ':''}${down.length?`trusts less: ${esc(down.map(ruleName).join(', '))}`:''}${!up.length&&!down.length?'no rule moved':''}</span></span>
  <span class="w-row__value"><span class="w-chip ${tagCls(x.tag)}">${esc(x.tag||'recorded')}</span><span class="w-row__time">${ago(x.scored_at)}</span></span></li>`}).join(''):'<li class="w-empty">The first lessons arrive 24 hours after the first verdicts.</li>';
 const b=$('#lessons-more');b.dataset.total=ls.length;moreButton(b,ls.length,n,6,'Show more','lessons');
}
function renderMoving(s){
 const rules=(s.brain||{}).rules||[];if(!changed('moving',rules.map(r=>[r.id,r.weight,r.hits,r.misses])))return;
 const mv=rules.filter(r=>Math.abs(r.weight-1)>0.001).sort((a,b)=>Math.abs(b.weight-1)-Math.abs(a.weight-1)).slice(0,6);
 $('#moving').innerHTML=mv.length?mv.map(r=>`<li class="w-row w-row--static w-row--rule" title="${esc(r.about||'')}"><span class="w-row__main"><span class="w-row__title">${esc(cap(ruleName(r.id)))}</span><span class="w-row__sub">right ${fmt.int(r.hits)} · wrong ${fmt.int(r.misses)}</span></span><span class="w-row__value"><span class="w-row__delta ${r.weight>1?'pos':'neg'}">${r.weight>1?'trusts more':'trusts less'} ${Number(r.weight).toFixed(2)}×</span></span></li>`).join(''):'<li class="w-empty">No rule has moved yet.</li>';
 const a=$('#moving-card .w-card__action');if(a)a.textContent=`All ${rules.length} rules`;
}
function renderPaper(s){
 const p=s.paper||{},lab=s.lab||{},val=lab.validation||{},rd=s.readiness||{};
 const lc=p.live_comparable,all=p.all_pools||{realized_usd:p.realized_usd,win_rate:p.win_rate,closed_count:p.closed_count},kept=(p.filtered||[])[0];
 if(!changed('paper',[p.realized_usd,p.unrealized_usd,p.win_rate,p.closed_count,lc,p.all_pools,kept,p.skipped,(p.open||[]).map(x=>[x.id,x.change_pct,x.valuation_stale,x.pair]),(p.closed||[]).slice(0,5).map(x=>[x.id,x.pair]),lab.in_use,val.n,val.required,rd.score,rd.next]))return;
 const ready=nz(rd.score),readyAt=nz(rd.ready_at)||80,arm=String(lab.in_use||'…').split('@')[0];
 // Where a trade could have happened for real: live buys from USDG pools only. ETH-pool trades are learning data.
 const where=x=>x.pair==='USDG'?(x.live_comparable===false?'USDG, in a loss pause':'USDG'):x.pair==='ETH'?'ETH, learning only':'';
 const row=(x,closed)=>`<li class="w-row w-row--static"><span class="w-row__main"><span class="w-row__title">$${esc(x.symbol)}${where(x)?` <span class="w-row__muted">· ${esc(where(x))}</span>`:""}</span><span class="w-row__sub">${closed?`${esc(x.reason||'closed')} · ${ago(x.closed_ts)}`:`opened ${ago(x.opened_ts)}${x.hedged?` · took profit, ${num(x.bag_pct)}% left`:''}`}</span></span><span class="w-row__value"><span class="w-row__delta ${x.valuation_stale?'':nz(closed?x.pnl_usd:x.change_pct)>=0?'pos':'neg'}">${x.valuation_stale?'price stale':pct(x.change_pct)}</span>${closed&&x.pnl_usd!=null?`<span class="w-row__time">${fmt.usd(x.pnl_usd,{cents:true})}</span>`:''}</span></li>`;
 const usd=v=>v==null?'—':fmt.usd(v,{cents:true}),sign=v=>v==null?'':nz(v)>=0?'pos':'neg',rate=b=>b&&b.win_rate!=null?num(b.win_rate)+'%':'—';
 const skipped=(p.skipped||[]).reduce((a,x)=>a+nz(x.n),0);
 const rules=((all.by_rule)||[]).map(r=>({rule:r.rule,all:r,lc:((lc||{}).by_rule||[]).find(x=>x.rule===r.rule)}));
 if(kept)rules.push({rule:kept.rule,all:kept.all_pools||{},lc:kept.live_comparable||{},filtered:true});
 const byRule=rules.length?tableWrap('Paper results by rule',`<table><thead><tr><th>rule</th><th class="r">could be real: closed</th><th class="r">result</th><th class="r">won</th><th class="r">all pools: closed</th><th class="r">result</th></tr></thead><tbody>${rules.map(r=>`<tr><td class="mono">${esc(r.rule)}${r.filtered?' <span class="w-chip">filter, buys nothing itself</span>':''}</td><td class="r">${fmt.int((r.lc||{}).closed_count??0)}</td><td class="r ${(r.lc||{}).closed_count?sign(r.lc.realized_usd):''}">${usd((r.lc||{}).closed_count?r.lc.realized_usd:null)}</td><td class="r">${rate(r.lc)}</td><td class="r">${fmt.int(r.all.closed_count??0)}</td><td class="r ${r.all.closed_count?sign(r.all.realized_usd):''}">${usd(r.all.closed_count?r.all.realized_usd:null)}</td></tr>`).join('')}</tbody></table>`):'';
 $('#paper-body').innerHTML=`<h3 class="list-title">Could be traded for real · USDG pools</h3>
 <div class="w-stats w-stats--4">
  <div class="w-stat"><span class="label">Realized, paper</span><span class="w-stat__value ${lc?sign(lc.realized_usd):''}">${lc?usd(lc.realized_usd):'—'}</span><span class="w-stat__note">${lc?`${fmt.int(lc.closed_count)} closed · $${num(p.size_usd)} of pretend money each`:'not reported yet'}</span></div>
  <div class="w-stat"><span class="label">Win rate</span><span class="w-stat__value">${rate(lc)}</span><span class="w-stat__note">${lc?`${fmt.int(lc.wins)} of ${fmt.int(lc.closed_count)} won`:'not reported yet'}</span></div>
  <div class="w-stat"><span class="label">Open</span><span class="w-stat__value">${lc?fmt.int(lc.open_count):'—'}</span><span class="w-stat__note">${skipped?`${fmt.int(skipped)} skipped while the loss breaker paused`:'none skipped by the loss breaker'}</span></div>
  <div class="w-stat"><span class="label">Exit rule in use</span><span class="w-stat__value w-stat__value--sm">${esc(arm)}</span><span class="w-stat__note">proven on paper: ${fmt.int(val.n??0)} of ${fmt.int(val.required??50)}</span></div></div>
 <p class="w-note">Real trading would buy only from USDG pools, so only these count toward the proof: ${esc(p.live_comparable_means||'a USDG pool, bought by a second-look rule, not while the loss breaker was paused')}.</p>
 <h3 class="list-title paper-all">All pools, ETH included · learning only</h3>
 <div class="w-stats w-stats--4">
  <div class="w-stat w-stat--s"><span class="label">Realized, paper</span><span class="w-stat__value ${sign(all.realized_usd)}">${usd(all.realized_usd)}</span><span class="w-stat__note">${fmt.int(all.closed_count)} closed</span></div>
  <div class="w-stat w-stat--s"><span class="label">Win rate</span><span class="w-stat__value">${rate(all)}</span><span class="w-stat__note">of ${fmt.int(all.closed_count)} closed</span></div>
  <div class="w-stat w-stat--s"><span class="label">Open result</span><span class="w-stat__value ${p.unrealized_usd==null?'':sign(p.unrealized_usd)}">${p.unrealized_usd==null?'—':usd(p.unrealized_usd)}</span><span class="w-stat__note">${(p.open||[]).length} open${p.unpriced_count?` · ${num(p.unpriced_count)} awaiting a quote`:''}</span></div>
  <div class="w-stat w-stat--s"><span class="label">Left out of the proof</span><span class="w-stat__value">${lc?fmt.int(nz(all.closed_count)-nz(lc.closed_count)):"—"}</span><span class="w-stat__note">ETH pools, verdict-time buys, entries in a loss pause</span></div></div>
 ${byRule}
 <div class="paper-ready"><div class="paper-ready__head"><span class="label">Readiness for real trades</span><span><b>${ready}</b> / ${readyAt}</span></div><span class="w-meter"><i data-m="pready"></i></span><p class="w-note">Next: ${esc(rd.next||'waiting for enough evidence')}</p></div>
 <div class="grid-2 grid-2--tight"><div><h3 class="list-title">Open positions</h3><ul class="w-list">${(p.open||[]).length?p.open.map(x=>row(x,false)).join(''):'<li class="w-empty">No open paper positions.</li>'}</ul></div>
 <div><h3 class="list-title">Last 5 closed</h3><ul class="w-list">${(p.closed||[]).length?p.closed.slice(0,5).map(x=>row(x,true)).join(''):'<li class="w-empty">Nothing closed yet.</li>'}</ul></div></div>`;
 setMeter($('#paper-body [data-m="pready"]'),100*ready/readyAt);
 $('#paper-more').innerHTML=`<p>Every loss here is a lesson for the brain and the lab, not a cost. Entries: ${esc(p.entry||'')}.</p><p>Exits: ${esc(p.rules||'')}.</p><p>When the day's paper losses reach the loss limit, new entries pause for a day, as real trading would; a rule's pick during a pause is recorded, not bought, and never counts toward its proof.${kept?` The ${esc(kept.rule)} rule buys nothing itself: it follows the entries a fixed filter would have kept, and must prove itself on 50 new USDG trades like every other rule.`:''}</p><p>${esc(rd.gate||'')}</p>`;
}
function renderVoice(s){
 const vo=s.voice||{},list=(vo.entries||[]).filter(e=>e.ok),n=more.voice;if(!changed('voice',[list.map(e=>e.id),n,Math.floor(Date.now()/60000)]))return;
 $('#voice-sub').textContent=vo.every_min?`a new note about every ${vo.every_min} min · numbers checked against the records`:'';
 $('#voice').innerHTML=list.length?list.slice(0,n).map(e=>`<li class="note" data-key="v${esc(e.id)}"><p class="note__text">${esc(e.text)}</p><div class="note__meta"><span>${esc(e.mood||'')}${e.mood?' · ':''}${ago(e.ts)}</span>${copyBtn(e.text)}</div></li>`).join(''):'<li class="w-empty">No notes yet.</li>';
 const b=$('#voice-more');b.dataset.total=list.length;moreButton(b,list.length,n,3,'Show more','voice');
}
const tableWrap=(label,html)=>`<div class="table-scroll" role="region" aria-label="${esc(label)}" tabindex="0">${html}</div>`;
function btText(b){b=b||{};if(b.n_fired==null)return '';return `${num(b.n_fired)} fired · median ${b.median_fired!=null?pct(b.median_fired):'…'} vs ${b.median_other!=null?pct(b.median_other):'…'} for the rest${b.p!=null?` · chance ${Math.round(b.p*100)} in 100`:''}`}
function armText(p){p=p||{};const tp=(p.tp||[]).map(t=>`${t[0]}x sell ${Math.round(t[1]*100)}%`).join(', ');return [tp&&`take profit ${tp}`,p.trail!=null&&`trail ${Math.round(p.trail*100)}%`,p.stop!=null&&`stop ${Math.round(p.stop*100)}%`,p.max_age&&`≤ ${Math.round(p.max_age/3600)}h`,p.delay_min!=null&&`enter +${p.delay_min}m`].filter(Boolean).join(' · ')}
function specText(x){const sp=x.spec||{};if(x.kind==='rule'){const c=(sp.conditions||[]).map(c=>`${c.metric} ${c.op} ${c.value}`).join(' and ');return `${c||'?'}: ${sp.points>0?'+':''}${num(sp.points)}`}return `${sp.name||'?'}: ${armText(sp)}`}
function renderLearningDetails(s){
 const rd=s.readiness||{},b=s.brain||{},rules=b.rules||[],cnt=b.counts||{},lb=s.lab||{},arms=lb.arms||[],ad=s.advisor||{},lr=ad.last_run;
 if(changed('dReady',rd))$('#ready').innerHTML=`<div class="parts">${(rd.parts||[]).map(pt=>`<div class="part"><div class="part__head"><span>${esc(cap(pt.label))}</span><b>${num(pt.score||0)} / 100</b></div><span class="w-meter"><i style="--w:${Math.max(0,Math.min(100,nz(pt.score)))}%"></i></span><p class="w-note">${Math.round(nz((rd.weights||{})[pt.id])*100)}% of the score · ${esc(pt.detail||'')}</p></div>`).join('')}</div><p class="w-note">Readiness ${num(rd.score??0)} of ${num(rd.ready_at||80)} needed. ${esc(rd.gate||'')}</p>`;
 if(changed('dRules',[rules,cnt,b.tracked,b.resolved]))$('#brain').innerHTML=`<p>Tracking ${fmt.int(b.tracked)} verdicts, ${fmt.int(b.resolved)} checked: rugged ${fmt.int(cnt.rugged)}, dumped ${fmt.int(cnt.dumped)}, flat ${fmt.int(cnt.flat)}, grew ${fmt.int(cnt.grew)}. A rule moves by how surprising the outcome was against the base rate: warned before a token that went bad, it gains; reassured, it loses; up to 2% a lesson, bounded 0.5× to 1.5×.</p>`
  +tableWrap('All rules and weights',`<table><thead><tr><th>rule</th><th>id</th><th class="r">weight</th><th class="r">lift</th><th class="r">right / wrong</th></tr></thead><tbody>${rules.map(r=>`<tr><td>${esc(cap(ruleName(r.id)))}${r.learned?' <span class="w-chip good">learned</span>':''}<div class="w-note">${esc(r.about||'')}</div></td><td class="mono">${esc(r.id)}</td><td class="r">${Number(r.weight).toFixed(2)}×</td><td class="r ${r.lift>1?'pos':r.lift!=null&&r.lift<1?'neg':''}">${r.lift==null?'…':Number(r.lift).toFixed(2)+'×'}</td><td class="r">${fmt.int(r.hits)} / ${fmt.int(r.misses)}${r.hit_rate!=null?` (${num(r.hit_rate)}%)`:''}</td></tr>`).join('')}</tbody></table>`);
 const armsUsdg=lb.arms_usdg;
 const armTable=(label,list)=>list.some(a=>a.n>0)?tableWrap(label,`<table><thead><tr><th>arm</th><th class="r">cases</th><th class="r">avg return</th><th class="r">lower bound</th><th class="r">win rate</th></tr></thead><tbody>${list.filter(a=>a.n>0).map(a=>`<tr><td class="mono">${esc(a.arm)}${a.arm===lb.in_use?' <span class="w-chip good">in use</span>':''}</td><td class="r">${num(a.n)}</td><td class="r ${a.mean_ret>=0?'pos':'neg'}">${a.mean_ret==null?'…':(a.mean_ret*100).toFixed(0)+'%'}</td><td class="r ${a.lcb==null?'':a.lcb>0?'pos':'neg'}">${a.lcb==null?'…':(a.lcb*100).toFixed(0)+'%'}</td><td class="r">${a.win_rate==null?'…':a.win_rate+'%'}</td></tr>`).join('')}</tbody></table>`):'';
 if(changed('dLab',[lb.in_use,lb.why,arms,armsUsdg,lb.method,lb.cases_active,lb.cases_resolved,lb.cases_ranked,lb.cases_mixed,lb.cases_legacy,lb.cases_capped]))$('#lab').innerHTML=`<p><span class="w-chip warn">simulated, not traded</span></p><p>In use: <b>${esc(lb.in_use||'')}</b> · ${esc(lb.why||'')}. Every candidate is simulated by ${fmt.int(arms.length)} arms at once (${fmt.int(Object.keys(lb.policies||{}).length)} exit rules × entry delays of ${(lb.delays_min||[]).join('/')} min), with ${Math.round(nz(lb.cost_per_side)*200)}% round-trip costs${lb.gas_per_side!=null?` and gas of about ${(nz(lb.gas_per_side)*100).toFixed(1)}% a leg`:''}. ${lb.method?esc(cap(lb.method))+'. ':''}An arm is ranked by the lower bound of its net return once it has ${num(lb.min_n)} cases; the ranking informs, and the rule in use only changes in the open, in the code, after a paper cohort. Cases: ${fmt.int(lb.cases_active)} active · ${fmt.int(lb.cases_resolved)} resolved${lb.cases_ranked!=null?` · ${fmt.int(lb.cases_ranked)} ranked · ${fmt.int(lb.cases_mixed)} with mixed price sources and ${fmt.int(lb.cases_legacy)} from the old simulator left out · ${fmt.int(lb.cases_capped)} with a gain capped across a gap in the data`:''}.</p>`
  +(armsUsdg?`<h3 class="list-title">USDG pools · could be traded for real</h3>${armTable('Exit strategy lab arms, USDG pools',armsUsdg)||'<p>No ranked USDG case yet.</p>'}<h3 class="list-title">All pools, ETH included</h3>`:'')
  +(armTable('Exit strategy lab arms, all pools',arms)||'<p>No resolved cases yet.</p>');
 if(changed('dAdvisor',ad))$('#advisor').innerHTML=`<p class="advisor-line">AI proposals: <b>${fmt.int(lr?lr.proposed:0)}</b> last run, <b>${fmt.int(lr?lr.adopted:0)}</b> adopted. Nothing it writes runs as code.</p><p>Writer <b>${esc(ad.model||'stub')}</b> · every ${num(ad.every_min)} min once ${num(ad.min_new)} more verdicts have resolved · ${esc(ad.state||'')}${lr?` · last run ${ago(lr.ts)}${(lr.tokens_in||lr.tokens_out)?` · ${fmt.int(lr.tokens_in)} tokens in, ${fmt.int(lr.tokens_out)} out`:''}${lr.note?` · ${esc(lr.note)}`:''}`:' · no run yet'}. New scoring rules first run in shadow on ${num((ad.shadow||{}).cohort_size||80)} future launches, one per creator, and must show a difference on at least ${num((ad.bar||{}).min_n)} cases each side. ${num((ad.shadow||{}).pending||0)} rules are waiting. This never enables trading.</p>`
  +((ad.rules||[]).length?tableWrap('Learned rules',`<table><thead><tr><th>learned rule</th><th>when</th><th class="r">points</th><th class="r">weight</th><th>backtest</th></tr></thead><tbody>${ad.rules.map(r=>`<tr><td class="mono">${esc(r.id)}${r.active?'':' (retired)'}</td><td>${esc(r.text)}</td><td class="r ${r.points<0?'neg':'pos'}">${num(r.points)}</td><td class="r">${Number(r.weight||1).toFixed(2)}×</td><td>${btText(r.backtest)}</td></tr>`).join('')}</tbody></table>`):'')
  +((ad.arms||[]).length?tableWrap('Learned exit arms',`<table><thead><tr><th>learned arm</th><th>policy</th><th>backtest</th></tr></thead><tbody>${ad.arms.map(a=>`<tr><td class="mono">${esc(a.name)}</td><td>${esc(armText(a.policy))}</td><td>${esc((a.backtest||{}).why||'')}</td></tr>`).join('')}</tbody></table>`):'')
  +(((ad.shadow||{}).rules||[]).length?`<p class="label">Future-launch evaluation</p><ul class="plain">${ad.shadow.rules.map(r=>`<li>Rule ${num(r.id)}: ${esc(r.status)}${r.result&&r.result.n_fired!==undefined?` · ${num(r.result.n_fired)} matched / ${num(r.result.n_other)} comparison`:''}</li>`).join('')}</ul>`:'')
  +((ad.suggestions||[]).length?`<p class="label">Proposals</p>`+tableWrap('Advisor proposals',`<table><thead><tr><th>when</th><th>kind</th><th>proposal</th><th>fate</th></tr></thead><tbody>${ad.suggestions.map(x=>`<tr><td>${ago(x.ts)}</td><td>${esc(x.kind)}</td><td class="mono">${esc(specText(x))}${x.why?`<div class="w-note">${esc(x.why)}</div>`:''}</td><td class="${x.status==='adopted'?'pos':''}">${esc(x.status)}<div class="w-note">${esc(x.reason||'')}</div></td></tr>`).join('')}</tbody></table>`):'');
 const evs=[];for(const e of [...(s.events||[])].sort((a,b)=>(b.ts||0)-(a.ts||0))){const l=evs[evs.length-1];if(l&&l.kind===e.kind&&l.text===e.text){l.n++;continue}if(evs.length>=60)break;evs.push({id:e.id,kind:e.kind,text:e.text,ts:e.ts,n:1})}
 if(changed('dLog',[evs.map(e=>[e.id,e.n]),Math.floor(Date.now()/60000)]))$('#log').innerHTML=evs.map(e=>`<li data-key="e${esc(e.id??e.kind+e.text)}"><span class="log__k ${esc(e.kind)}">${esc(e.kind)}</span><span class="log__t">${esc(e.text)}${e.n>1?` <span class="w-chip">×${e.n}</span>`:''}</span><span class="log__ago">${ago(e.ts)}</span></li>`).join('');
}

/* ================= Token scans ================= */
let scanQuery='',scanFilter='all',scanSort='newest',scanFirst=true;const expanded=new Set(),seenScans=new Set();
const FILTERS=[['all','All'],['avoid','Avoid'],['mixed','Mixed'],['looks healthy','Looks healthy'],['partial','Incomplete']];
function fdvLag(t){const m=t.metrics||{};if(!(m.price0_ts&&t.scored_at))return '';const lag=m.price0_ts-t.scored_at;return lag>900?` · first price read ${lag>=7200?Math.round(lag/3600)+' h':Math.round(lag/60)+' min'} after the scan`:''}
function scanRow(t,isNew){
 const m=t.metrics||{},cls=vcls(t.verdict),open=expanded.has(t.token),id='sd-'+String(t.token).replace(/[^a-zA-Z0-9]/g,''),img=logo(t.logo),bad=/rugged|dumped/.test(t.outcome||'');
 const delta=t.outcome&&t.outcome!=='pending'?`<span class="w-row__delta ${bad?'neg':t.outcome==='grew'?'pos':''}">${esc(t.outcome)} ${pct(t.change_pct)}</span>`:t.change_pct!=null?`<span class="w-row__delta ${t.change_pct<0?'neg':'pos'}">${pct(t.change_pct)} since</span>`:'<span class="w-row__delta">awaiting outcome</span>';
 const fdv=fmt.compact(m.fdv0_usd);
 let html=`<li class="scan" data-key="${esc(t.token)}"><button type="button" class="w-row ${cls}${isNew?' is-new':''}" aria-expanded="${open}" aria-controls="${id}">
 <span class="w-row__lead">${img?`<img src="${esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer" data-src="${esc(t.logo||'')}" onerror="badLogos.add(this.dataset.src);this.replaceWith(document.createTextNode('${initials(t.name||t.symbol)}'))">`:initials(t.name||t.symbol)}</span>
 <span class="w-row__main"><span class="w-row__title">${esc(t.name||t.symbol||'Unnamed token')}</span><span class="w-row__sub">$${esc(t.symbol||'?')} · ${esc(t.pair_symbol||'…')} · graduated ${ago(t.grad_ts)||'…'}${fdv?`<span class="hide-sm"> · FDV at scan ${fdv}</span>`:''}${t.partial?' · incomplete reads':''}</span></span>
 <span class="w-row__value"><span class="w-verdict ${cls}"><b>${num(t.score)}</b>${esc(t.verdict||'pending')}</span>${delta}</span><span class="w-row__chev" aria-hidden="true">${open?'−':'+'}</span></button>`;
 if(open){
  const pctv=v=>v==null?null:v+'%';
  const chips=[['Holders',m.holders],['Top 10 share',pctv(m.top10_pct),m.top10_pct>=50,m.top10_pct<20],['Sniped',pctv(m.snipe_pct),m.snipe_pct>=30],['Creator trust',t.trust,t.trust<35,t.trust>=65],['FDV at scan',fmt.compact(m.fdv0_usd)],['FDV now',fmt.compact(m.fdv_usd)],
   ['Outside pool',pctv(m.outside_pool_pct)],['Unique buyers',m.unique_buyers],['Bought via bots',pctv(m.routed_pct)],['Held by linked wallets',m.linked_group_pct>=10&&m.senders_on_record>=150?m.linked_group_pct+'%':null,true],['Bought by losing wallets',m.crowd_history>=150?pctv(m.losing_pct):null,m.losing_pct>=30,m.losing_pct<5],
   ['Creator bought',pctv(m.deployer_buy_pct),m.deployer_buy_pct>=20],['Creator holding',pctv(m.deployer_hold_pct),m.deployer_hold_pct>=10],['Creator tax',t.creator_tax_bps==null?null:(t.creator_tax_bps/100)+'%',(t.creator_tax_bps||0)>500],['Creator launches',m.creator_prev_launches,(m.creator_prev_launches||0)>10],['Trades 1h',m.swaps_1h],['Volume 24h',fmt.compact(m.volume_24h_usd)]].filter(c=>c[1]!=null&&c[1]!=='');
  const chip=c=>`<span class="w-chip${c[2]?' warn':c[3]?' good':''}">${c[0]} <b>${esc(c[1])}</b></span>`;
  const rs=(t.reasons||[]).map(splitReason),scored=rs.filter(r=>r.pts).sort((a,b)=>(a.pts<0?0:1)-(b.pts<0?0:1)||Math.abs(b.pts)-Math.abs(a.pts)),plain=rs.filter(r=>!r.pts);
  const links=[[`https://www.ponsfamily.com/launchpad/${t.token}`,'pons'],[`${EXPLORER}token/${t.token}`,'Explorer'],[isAddr(t.deployer)?`${EXPLORER}address/${t.deployer}`:null,'Creator'],[xurl(t.twitter),'X'],[tgurl(t.telegram),'Telegram'],[weburl(t.website),'Website']].filter(l=>l[0]);
  html+=`<div class="w-row__more" id="${id}"><p class="w-note">Scored ${ago(t.scored_at)||'…'} · graduated ${ago(t.grad_ts)||'…'}${fdvLag(t)}${t.partial?' · <span class="neg">some chain reads were incomplete</span>':''}</p>
  ${scored.length?`<p class="label">Why</p>${reasonList(scored,'reasons reasons--pts')}`:''}
  <div class="w-chips">${chips.slice(0,6).map(chip).join('')}</div>
  <details class="w-details w-details--inline"><summary>All measurements (${chips.length+plain.length})</summary><div class="w-details__body"><div class="w-chips">${chips.map(chip).join('')}</div>${plain.length?`<ul class="reasons">${plain.map(r=>`<li>${esc(r.text)}</li>`).join('')}</ul>`:''}</div></details>
  <div class="scan-links">${links.map(([u,l])=>`<a class="w-btn w-btn--ghost w-btn--sm" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${l}</a>`).join('')}</div>
  <div class="scan-addr"><code>${esc(t.token)}</code>${copyBtn(t.token,'Copy address')}</div></div>`}
 return html+'</li>'}
function renderScans(s,force){
 const feed=s.feed||[],counts={all:feed.length,partial:feed.filter(t=>t.partial).length};for(const t of feed)counts[t.verdict]=(counts[t.verdict]||0)+1;
 if(changed('chips',[counts,scanFilter]))$('#verdict-chips').innerHTML=FILTERS.map(([k,l])=>`<button type="button" class="w-filter" data-filter="${esc(k)}" aria-pressed="${scanFilter===k}">${l}<small>${counts[k]||0}</small></button>`).join('');
 if(!changed('feed',[feed.map(t=>[t.token,t.score,t.outcome,Math.round(nz(t.change_pct)),t.logo]),[...expanded],scanQuery,scanFilter,scanSort,more.feed,Math.floor(Date.now()/60000)])&&!force)return;
 const q=scanQuery.toLowerCase();let list=feed.filter(t=>(!q||[t.name,t.symbol,t.token].some(x=>String(x||'').toLowerCase().includes(q)))&&(scanFilter==='all'||(scanFilter==='partial'?t.partial:t.verdict===scanFilter)));
 list=[...list].sort((a,b)=>scanSort==='score'?b.score-a.score:scanSort==='risk'?a.score-b.score:b.grad_ts-a.grad_ts);
 $('#scan-count').textContent=feed.length?`${list.length===feed.length?`${feed.length} most recent scans`:`${list.length} of ${feed.length} recent scans`} · scores are the call at scan time`:'';
 const focusToken=document.activeElement&&document.activeElement.closest&&document.activeElement.closest('#feed .scan')?.dataset.key;
 const shown=list.slice(0,more.feed);
 $('#feed').innerHTML=shown.length?shown.map(t=>scanRow(t,!scanFirst&&!seenScans.has(t.token))).join(''):feed.length?'<li class="w-empty">No matching scans. Try another search or filter.</li>':'<li class="w-empty">No scans yet. The first ones land within a minute or two.</li>';
 if(feed.length){feed.forEach(t=>seenScans.add(t.token));scanFirst=false}
 if(focusToken){const b=document.querySelector(`#feed .scan[data-key="${CSS.escape(focusToken)}"] .w-row`);if(b)b.focus({preventScroll:true})}
 const b=$('#feed-more');b.dataset.total=list.length;b.hidden=list.length<=more.feed;b.textContent=`Show ${Math.min(20,list.length-more.feed)} more`;b.dataset.key='feed';
}
$('#token-search').addEventListener('input',e=>{scanQuery=e.target.value;more.feed=20;if(lastState)renderScans(lastState,true)});
$('#verdict-chips').addEventListener('click',e=>{const b=e.target.closest('[data-filter]');if(!b)return;scanFilter=b.dataset.filter;more.feed=20;if(lastState)renderScans(lastState,true)});
$('#sort-chips').addEventListener('click',e=>{const b=e.target.closest('[data-sort]');if(!b)return;scanSort=b.dataset.sort;$$('#sort-chips [data-sort]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));if(lastState)renderScans(lastState,true)});
$('#feed').addEventListener('click',e=>{const b=e.target.closest('.w-row');if(!b||!$('#feed').contains(b))return;const t=b.closest('.scan').dataset.key;expanded.has(t)?expanded.delete(t):expanded.add(t);if(lastState)preserveReadingPosition(()=>renderScans(lastState,true))});
function renderCreators(s){
 const ser=(s.bad_actors||{}).serial||[],n=more.serial;if(!changed('serial',[ser,n,Math.floor(Date.now()/60000)]))return;
 $('#serial').innerHTML=ser.length?ser.slice(0,n).map(x=>`<li data-key="c${esc(x.deployer)}"><a class="w-row ${x.trust<35?'avoid':''}" href="${EXPLORER}address/${esc(x.deployer)}" target="_blank" rel="noopener noreferrer"><span class="w-row__lead w-row__lead--num">${num(x.trust)}</span><span class="w-row__main"><span class="w-row__title mono">${short(x.deployer)}</span><span class="w-row__sub">${fmt.int(x.launches)} launches · ${fmt.int(x.grads||0)} graduated · ${fmt.int(x.rugged||0)} rugged · last seen ${ago(x.last_ts)}</span></span><span class="w-row__value"><span class="w-chip ${x.trust<35?'warn':x.trust>=65?'good':''}">trust ${num(x.trust)}</span></span></a></li>`).join(''):'<li class="w-empty">None in the window.</li>';
 const b=$('#serial-more');b.dataset.total=ser.length;moreButton(b,ser.length,n,5,'Show all','serial');
}

/* ================= Treasury ================= */
function splitOf(tr){const earned=nz(tr.claimed_total),creator=nz(tr.forwarded_total),burn=nz(tr.burned_total),gold=nz(tr.gold_total),owed={creator:nz(tr.owed_to_owner),burn:nz(tr.owed_to_burn),gold:nz(tr.owed_to_gold)};
 const ops=Math.max(0,earned-creator-burn-gold-owed.creator-owed.burn-owed.gold);return {earned,creator,burn,gold,ops,owed}}
function renderMoney(s){
 const tr=s.treasury||{},el=$('#money');
 if(!tr.token&&!nz(tr.claimed_total)){el.innerHTML=`<div class="w-card__head"><div><span class="label">Follow the money</span><h2 class="w-card__title" id="money-title">Nothing earned yet</h2></div></div><p>Once the first fees are claimed, every dollar and where it went appears here.</p>`;return}
 const x=splitOf(tr),e=x.earned||1;
 if(!el.dataset.built){el.dataset.built='1';el.innerHTML=`<div class="w-card__head"><div><span class="label">Follow the money</span><h2 class="w-card__title money__title" id="money-title"><span data-n="earned">$0</span> earned in fees so far</h2></div></div>
 <div class="flow" role="img" data-t="flowLabel"><span class="flow__seg flow__seg--creator"></span><span class="flow__seg flow__seg--burn"></span><span class="flow__seg flow__seg--gold"></span><span class="flow__seg flow__seg--ops"></span></div>
 <div class="w-stats w-stats--4 money__legend">${[['creator','To the creator'],['burn','Bought and burned'],['gold','Gold reserve'],['ops','Kept to run itself']].map(([k,l])=>`<div class="w-stat w-stat--s legend--${k}"><span class="label"><i class="swatch"></i>${l}</span><span class="w-stat__value" data-n="${k}">$0</span><span class="w-stat__note" data-t="${k}"></span></div>`).join('')}</div>`}
 const q=k=>el.querySelector(`[data-n="${k}"]`),t=k=>el.querySelector(`[data-t="${k}"]`);
 setNum(q('earned'),x.earned,v=>fmt.usd(v));
 const share={creator:tr.share,burn:tr.burn_share,gold:tr.gold_share,ops:tr.ops_share};
 for(const k of ['creator','burn','gold','ops']){setNum(q(k),x[k],v=>fmt.usd(v));const ow=x.owed[k];t(k).textContent=`policy ${Math.round(nz(share[k])*100)}%${ow>0?` · + ${fmt.usd(ow,{cents:true})} waiting`:''}`}
 const segs=el.querySelectorAll('.flow__seg'),w=[x.creator+x.owed.creator,x.burn+x.owed.burn,x.gold+x.owed.gold,x.ops].map(v=>100*v/e);
 segs.forEach((sg,i)=>{sg._w=w[i];setMeter(sg,w[i])});
 t('flowLabel').setAttribute('aria-label',`Of ${fmt.usd(x.earned)} earned: ${fmt.usd(x.creator)} to the creator, ${fmt.usd(x.burn)} burned, ${fmt.usd(x.gold)} in gold, ${fmt.usd(x.ops)} kept to run itself`);
}
// The ETH burns paid in gas, with its dollar value at the wallet's own ETH price (booked since gas was recorded).
function gasNote(s){const tr=s.treasury||{},g=nz(tr.burn_gas_eth);if(!(g>0))return '';const eth=nz(tr.eth),usd=nz(s.character?.parts?.eth_rh),px=eth>0&&usd>0?usd/eth:0;
 return ` · gas ${g<0.001?g.toFixed(6):g.toFixed(4)} ETH${px?` (${fmt.usd(g*px,{cents:true})})`:''}`}
function renderBurnCard(s){
 const tr=s.treasury||{},el=$('#burn');if(!tr.token){el.hidden=true;return}el.hidden=false;
 const bp=burnProgram(tr),burns=allBurns(s),qty=nz(tr.burned_qty),share=100*qty/WORM_SUPPLY,min=nz(tr.burn_min_usd)||5,p=Math.min(100,100*nz(tr.owed_to_burn)/min);
 if(!changed('burnCard',[tr.burned_qty,tr.burned_total,tr.owed_to_burn,bp,burns.length,burns.length?burns[burns.length-1].ts:0,Math.floor(Date.now()/60000),innerWidth<600]))return;
 const prog=bp?`<div class="burn-prog"><p class="burn-prog__reason">${esc(bp.reason||'The treasury holds more than its 90-day reserve needs, so the surplus is burned over a week in small buys that do not move the price.')}</p>
  <div class="w-stats w-stats--3"><div class="w-stat w-stat--s"><span class="label">${bp.auto?'This round':'Program'}</span><span class="w-stat__value">${fmt.usd(bp.total_usd)}</span></div><div class="w-stat w-stat--s"><span class="label">Burned so far</span><span class="w-stat__value">${fmt.usd(bp.burned_usd)}</span></div><div class="w-stat w-stat--s"><span class="label">$WORM burned this week</span><span class="w-stat__value">${fmt.qty(bp.burned_qty!=null?bp.burned_qty:(bp.burns||[]).reduce((a,b)=>a+nz(b.qty),0))}</span></div></div>
  ${weekTrack('wk-treasury',s,bp,{big:true})}</div>`:'';
 el.innerHTML=`<div class="w-card__head"><div><span class="label">The burn</span><h2 class="w-card__title" id="burncard-title">${fmt.qty(qty)} $WORM burned forever</h2></div><a class="w-card__action" href="${EXPLORER}token/${esc(tr.token)}" target="_blank" rel="noopener noreferrer">See the token</a></div>
 <div class="w-stats w-stats--4"><div class="w-stat"><span class="label">Share of supply</span><span class="w-stat__value">${share.toFixed(2)}%</span><span class="w-stat__note">of 1 billion $WORM</span></div><div class="w-stat"><span class="label">Spent on burns</span><span class="w-stat__value">${fmt.usd(tr.burned_total)}</span><span class="w-stat__note">${nz(tr.owed_to_burn)>0?`+ ${fmt.usd(tr.owed_to_burn,{cents:true})} waiting`:'nothing waiting'}</span></div><div class="w-stat"><span class="label">Burns</span><span class="w-stat__value">${fmt.int(burns.length)}</span><span class="w-stat__note">${burns.length?'last '+ago(burns[burns.length-1].ts):'none yet'}${gasNote(s)}</span></div><div class="w-stat"><span class="label">Share of fees</span><span class="w-stat__value">${Math.round(nz(tr.burn_share)*100)}%</span><span class="w-stat__note">of every claim buys $WORM to burn</span></div></div>
 ${prog}
 ${burns.length?`<div class="burn-hist"><span class="label">Every burn since launch</span>${historyTrack('hist-treasury',s,burns)}</div>`:''}
 <div class="next-burn"><div class="next-burn__head"><span>Next regular burn</span><span>${savedLine(tr.owed_to_burn,min,'in the next small buy')}</span></div><span class="w-meter w-meter--ember${p>=80?' is-close':''}"><i data-m="next"></i></span></div>`;
 setMeter(el.querySelector('[data-m="next"]'),p);
}
// "$1.20 of $2 saved up" until the minimum is reached; past it the amount is simply waiting to go.
function savedLine(owed,min,ready){return nz(owed)>=min?`<b>${fmt.usd(owed,{cents:true})}</b> ${ready}`:`<b>${fmt.usd(owed,{cents:true})}</b> of ${fmt.usd(min)} saved up`}
function renderGold(s){
 const tr=s.treasury||{},min=nz(tr.gold_min_usd)||5,p=Math.min(100,100*nz(tr.owed_to_gold)/min);if(!changed('gold',[tr.gold_usd,tr.gold_held,tr.gold_total,tr.owed_to_gold,tr.gold_state]))return;
 $('#gold').innerHTML=`<div class="w-card__head"><div><span class="label label--gold">Gold reserve</span><h2 class="w-card__title" id="gold-title">${fmt.usd(tr.gold_usd,{cents:true})} in gold</h2></div><div class="ingots" aria-hidden="true"><i></i><i></i><i></i></div></div>
 <div class="w-stats"><div class="w-stat w-stat--s"><span class="label">Held</span><span class="w-stat__value">${isNum(tr.gold_held)?Number(tr.gold_held).toFixed(4):'—'} GLD</span></div><div class="w-stat w-stat--s"><span class="label">Bought for</span><span class="w-stat__value">${fmt.usd(tr.gold_total,{cents:true})}</span></div></div>
 <div class="next-burn next-gold"><div class="next-burn__head"><span>Next gold buy</span><span>${savedLine(tr.owed_to_gold,min,'ready to buy')}</span></div><span class="w-meter w-meter--gold"><i data-m="g"></i></span></div>
 <p class="w-note">${Math.round(nz(tr.gold_share)*100)}% of every claim buys tokenized gold (GLD) on Robinhood Chain. It is never spent on running costs and never counted in the runway.</p>`;
 setMeter($('#gold [data-m="g"]'),p);
}
function renderRunway(s){
 const rw=s.runway||{},tr=s.treasury||{},cp=s.compute||{},cost=nz(rw.cost_per_day_usd),credit=cp.balance_usd==null?null:nz(cp.balance_usd),compute=nz((rw.cost_parts||{}).compute);
 if(!changed('runway',[rw,cp.balance_usd,tr.accounting_error]))return;
 const reserve=nz(rw.reserve_needed_usd),avail=rw.treasury_usd,cov=isNum(avail)&&reserve>0?Math.min(100,100*nz(avail)/reserve):null;
 $('#runway').innerHTML=`<div class="w-card__head"><div><span class="label">Running costs</span><h2 class="w-card__title" id="runway-title">Can it pay for itself?</h2></div></div>
 <div class="w-stat w-stat--l"><span class="label">Runway</span><span class="w-stat__value">${tr.accounting_error?'—':fmt.int(rw.runway_days_no_income)}<small>days</small></span><span class="w-stat__note">of costs covered by the ${fmt.usd(avail)} treasury, with no new income</span></div>
 <div class="reserve"><div class="next-burn__head"><span>${fmt.usd(avail)} of ${fmt.usd(reserve,{cents:true})} needed for ${fmt.int(rw.reserve_days||90)} days</span><span class="${cov>=100?'pos':''}">${tr.accounting_error?'needs review':cov>=100?'covered':cov?'building':'awaiting funding'}</span></div><span class="w-meter"><i data-m="res"></i></span></div>
 <div class="w-stats w-stats--3"><div class="w-stat w-stat--s"><span class="label">Daily cost</span><span class="w-stat__value">${fmt.usd(cost,{cents:true})}</span><span class="w-stat__note">an estimate, not a bill</span></div>
 <div class="w-stat w-stat--s"><span class="label">AI credit</span><span class="w-stat__value">${credit==null?'—':fmt.usd(credit,{cents:true})}</span><span class="w-stat__note">${credit!=null&&compute>0?`≈ ${fmt.int(credit/compute)} days of thinking`:'prepaid'}</span></div>
 <div class="w-stat w-stat--s"><span class="label">Income a day</span><span class="w-stat__value">${rw.income_measured?fmt.usd(rw.income_per_day_usd):'—'}</span><span class="w-stat__note">${rw.income_measured?`its share, last ${num(rw.income_window_days)} days`:'not measured yet'}</span></div></div>`;
 setMeter($('#runway [data-m="res"]'),tr.accounting_error?0:cov||0);
}
let launchStatus=null,launchFetched=0;
async function fetchLaunch(){if(Date.now()-launchFetched<60000)return;launchFetched=Date.now();try{const r=await fetch('/api/launch/status',{cache:'no-store'});if(r.ok){launchStatus=await r.json();delete keys.wallet;if(lastState)safe(renderWallet,lastState)}}catch(_){}}
function renderWallet(s){
 const tr=s.treasury||{},cp=s.compute||{},la=s.launch||launchStatus;if(!la&&view==='treasury')fetchLaunch();
 if(!changed('wallet',[tr.wallet,tr.usdg,tr.eth,tr.claimable_usdg,cp.balance_usd,cp.provider,la&&[la.state,la.at,la.token],s.live,tr.accounting_error]))return;
 const bal=(v,unit,note,d=2)=>`<div class="w-stat w-stat--s"><span class="w-stat__value">${isNum(v)?Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}):'—'}<small>${unit}</small></span><span class="w-stat__note">${note}</span></div>`;
 const token=la&&isAddr(la.token)?la.token:isAddr(tr.token)?tr.token:null,launched=la&&la.state==='launched';
 $('#wallet').innerHTML=`<div class="w-card__head"><div><span class="label">Money in the open</span><h2 class="w-card__title" id="wallet-title">Wallet</h2></div></div>
 ${tr.accounting_error?'<p class="notice">Payment accounting needs operator review. Available funds cannot be verified.</p>':''}
 ${tr.wallet?`<div class="addr-row"><code>${esc(tr.wallet)}</code>${copyBtn(tr.wallet,'Copy')}<a class="w-btn w-btn--ghost w-btn--sm" href="${EXPLORER}address/${esc(tr.wallet)}" target="_blank" rel="noopener noreferrer">Explorer</a></div>
 <div class="w-stats w-stats--2">${bal(tr.usdg,'USDG','on Robinhood Chain')}${bal(tr.eth,'ETH','for gas',4)}${bal(tr.claimable_usdg,'USDG claimable','fees waiting in escrow')}<div class="w-stat w-stat--s"><span class="w-stat__value">${fmt.usd(cp.balance_usd,{cents:true})}<small>AI credit</small></span><span class="w-stat__note">${cp.provider==='aisurplus'?'AI Surplus · separate from the wallet':'separate from the wallet'}</span></div></div>
 <p class="w-note">${s.live?'Claims, burns and gold buys run on-chain from this wallet.':'Payments are paused.'}</p>`:'<p>The wallet is not connected yet. Once it is, its balances and every fee movement appear here.</p>'}
 ${token?`<div class="fact-row"><span>$WORM ${launched&&la.at?`launched ${fmt.date(la.at)}`:'token'}</span><a class="w-btn w-btn--ghost w-btn--sm" href="https://www.ponsfamily.com/launchpad/${esc(token)}" target="_blank" rel="noopener noreferrer">View token</a></div>`:''}`;
}
function renderTrading(s){
 const td=s.trader||{},rd=s.readiness||{},rw=s.runway||{},tr=s.treasury||{},off=td.enabled!==true;
 if(!changed('trading',[td.enabled,td.live_sell_ready,rd.score,rd.ready_at,rw.can_invest,tr.accounting_error]))return;
 const item=(ok,l,v)=>`<li class="${ok?'is-ok':'is-no'}"><span class="check" aria-hidden="true">${ok?'✓':'✕'}</span><span>${l}</span><b>${v}</b></li>`;
 $('#trades').innerHTML=`<div class="w-card__head"><div><span class="label">Honest status</span><h2 class="w-card__title" id="trades-title">${off?'Trading: paper only':'Trading: on, within limits'}</h2></div></div>
 <p>${off?'The worm studies outcomes and trades on paper while real trading stays off. Only the operator can turn it on, and the checks below still apply then.':'Every check below must pass before a trade.'}</p>
 <ul class="checklist">${item(!off,'Operator permission',off?'Off':'On')}${item(nz(rd.score)>=nz(rd.ready_at||80),'Readiness',`${num(rd.score??0)} / ${num(rd.ready_at??80)}`)}${item(!!rw.can_invest&&!tr.accounting_error,'Reserve surplus',tr.accounting_error?'Needs review':rw.can_invest?'Available':'Not yet')}${item(!!td.live_sell_ready,'Live exits',td.live_sell_ready?'Supported':'Not enabled')}</ul>
 <a class="w-card__action" href="#learning/paper">See paper trading</a>`;
}
function renderTreasuryDetails(s){
 const tr=s.treasury||{},rw=s.runway||{},cp=s.compute||{},td=s.trader||{},money=v=>fmt.usd(v,{cents:true}),row=(l,v)=>`<div><dt>${l}</dt><dd>${v}</dd></div>`;
 const link=(a,l)=>isAddr(a)?`<a href="${EXPLORER}address/${a}" target="_blank" rel="noopener noreferrer">${l}</a>`:'';
 if(changed('dSettle',[tr.claimed_total,tr.forwarded_total,tr.owed_to_owner,tr.burned_total,tr.burned_qty,tr.owed_to_burn,tr.gold_total,tr.gold_held,tr.gold_usd,tr.owed_to_gold,tr.burn_state,tr.gold_state,tr.claim_policy,tr.fee_sweep,tr.gas_refill]))
  $('#settle').innerHTML=`<dl class="facts">${row('Claimed fees',money(tr.claimed_total))}${row('Sent to creator',money(tr.forwarded_total))}${row('Still owed to creator',money(tr.owed_to_owner))}${row('Spent on $WORM burns',money(tr.burned_total))}${row('$WORM burned',fmt.int(tr.burned_qty))}${row('Waiting for the next burn',money(tr.owed_to_burn))}${row('Spent on gold',money(tr.gold_total))}${row('Gold held',(isNum(tr.gold_held)?Number(tr.gold_held).toFixed(6):'—')+' GLD')}${row('Gold valuation',money(tr.gold_usd))}${row('Waiting for the next gold buy',money(tr.owed_to_gold))}</dl>
  <p>${esc(tr.burn_state||'')} · ${esc(tr.gold_state||'')}</p>${tr.claim_policy&&tr.claim_policy.reason?`<p>Claim check: ${esc(tr.claim_policy.reason)}</p>`:''}${tr.fee_sweep&&tr.fee_sweep.reason?`<p>Curve fees: ${esc(tr.fee_sweep.reason)}</p>`:''}${tr.gas_refill&&tr.gas_refill.reason?`<p>Gas refill: ${esc(tr.gas_refill.reason)}</p>`:''}<p class="links-row">${link(tr.owner,'Creator wallet')}${isAddr(tr.token)?`<a href="https://www.ponsfamily.com/launchpad/${tr.token}" target="_blank" rel="noopener noreferrer">$WORM on pons</a>`:''}</p>`;
 const led=tr.ledger||[];
 if(changed('dLedger',[led.map(l=>l.id),Math.floor(Date.now()/60000)])){$('#d-ledger summary').textContent='Payment history'+(led.length?` · last ${led.length}`:'');
  $('#ledger').innerHTML=led.length?`<ul class="w-list">${led.slice(0,30).map(l=>{const u=txUrl(s,l.tx);return `<li class="w-row w-row--static"><span class="w-row__main"><span class="w-row__title">${esc(cap(String(l.kind||'').replaceAll('_',' ')))}</span><span class="w-row__sub">${esc(l.note||'')} · ${fmt.utc(l.ts)}</span></span><span class="w-row__value"><span class="w-row__delta">${isNum(l.amount)?Number(l.amount).toLocaleString('en-US',{maximumFractionDigits:4}):'—'} ${esc(l.asset||'')}</span>${u?`<a class="w-row__time" href="${esc(u)}" target="_blank" rel="noopener noreferrer">tx</a>`:''}</span></li>`}).join('')}</ul>`:'<p>No fee movements yet.</p>'}
 if(changed('dProj',rw.scenarios)){const sc=Object.entries(rw.scenarios||{});$('#projection').innerHTML=sc.length?tableWrap('90-day projection',`<table><thead><tr><th>scenario</th><th class="r">ending balance</th><th class="r">funds last</th></tr></thead><tbody>${sc.map(([n,v])=>`<tr><td>${esc(n==='current income'&&!rw.income_measured?'income baseline (unmeasured)':n)}</td><td class="r ${v.end_balance_usd<0?'neg':''}">${money(v.end_balance_usd)}</td><td class="r">${v.runs_out_day!=null?'day '+fmt.int(v.runs_out_day):'beyond 90 days'}</td></tr>`).join('')}</tbody></table>`)+'<p>Scenarios use planned costs and the claim history. A negative balance means a shortfall, not money already spent.</p>':'<p>No projection yet.</p>'}
 if(changed('dCosts',[rw.cost_parts,rw.compute_budget_per_day_usd,rw.surplus_usd,rw.claims_per_day_usd,rw.rule,cp.topup_usd,cp.topup_below_usd,cp.pays_with])){const c=rw.cost_parts||{};
  $('#costs').innerHTML=`<dl class="facts">${row('Compute / day',money(c.compute))}${row('Gas / day',money(c.gas))}${row('Bridge / day',money(c.bridge))}${row('Compute budget / day',money(rw.compute_budget_per_day_usd))}${row('Surplus above the reserve',money(rw.surplus_usd))}${row('Claims / day',rw.income_measured?money(rw.claims_per_day_usd):'not measured')}${row('AI credit top-up',money(cp.topup_usd))}${row('Top up below',money(cp.topup_below_usd))}${row('Pays with',esc(cp.pays_with||'not configured'))}</dl><p>${esc(rw.rule||'')} Funding the reserve does not enable trading.</p>`}
 if(changed('dPolicy',[td.policy,(td.positions||[]).length,(td.trades||[]).length]))$('#policy').innerHTML=`<p>${esc(td.policy||'Policy unavailable')}</p>${(td.positions||[]).length?`<p class="label">Positions</p><ul class="w-list">${td.positions.slice(0,30).map(p=>`<li class="w-row w-row--static"><span class="w-row__main"><span class="w-row__title">$${esc(p.symbol)}</span><span class="w-row__sub">${esc(p.mode||'')} · ${esc(p.status||'')}${p.reason?' · '+esc(p.reason):''}</span></span><span class="w-row__value"><span class="w-row__delta">${money(p.size_usd)}</span></span></li>`).join('')}</ul>`:'<p>No real positions recorded.</p>'}`;
}

/* ---------- boot ---------- */
applyMotion();navigate(true);
load();connect();setInterval(load,20000);
