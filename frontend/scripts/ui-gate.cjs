// UI GATE — the automated browser check this project did not have.
//
// Until 2026-08-28 the frontend had NO automated tests at all, so every UI claim
// rested on someone looking. This drives a real Chromium over every route at four
// viewports and asserts what a human would otherwise have to notice.
//
//   cd frontend
//   npm run build && npx vite preview --port 4173 &
//   node scripts/ui-gate.cjs                                       # local build
//   BASE=https://supply-chain-ui-bhwz.onrender.com node scripts/ui-gate.cjs   # live
//   API=http://localhost:8000 node scripts/ui-gate.cjs             # local build, local API
//
// It lives under frontend/ and is .cjs on purpose: it uses require(), and Node
// resolves modules from the SCRIPT's directory upward — so it must sit beside the
// node_modules that holds playwright and axe-core. Both are devDependencies here;
// run `npx playwright install chromium` once. Exits non-zero on any failure.
//
// WHAT IT CHECKS, and why each one is here — every check is a postmortem:
//   * horizontal overflow, measured against the REAL scroll container. Every route
//     renders inside `overflow-y-auto`, so the document never scrolls and
//     `document.scrollWidth` reports a false clean.
//   * emoji anywhere in the product UI (owner's standing rule).
//   * text under 11px, and prose under 12px.
//   * clipped SVG chart labels — `scrollWidth > clientWidth` NEVER fires on SVG
//     text, which is why three clipped axis labels shipped unnoticed.
//   * chart geometry: the TALLEST bar must clear 8px. A short bar is honest when
//     its value is small; a chart whose tallest bar is a hairline is a bug
//     (`barCategoryGap` as a raw number produced 0.7px bars).
//   * touch targets >= 44px, with WCAG 2.5.5's inline-link exemption.
//   * a chart legend overlapping its own axis labels. The Price-of-Resilience
//     chart shipped with `k — minimum distinct distributors per BOM (count)`
//     drawn ON TOP of its legend at all four viewports — the whole 286x15px
//     caption inside the legend's box, not a near miss. Recharts stacks a
//     bottom-aligned legend directly beneath the x-axis, which is exactly where
//     an `insideBottom` axis label lands, and neither reserves space from the
//     other. Nothing else here could see it: the label is SVG text, the legend
//     is an HTML sibling, neither overflows any container, and both render at a
//     legal size.
//   * chart legend contrast, hand-rolled ON PURPOSE and only here. axe-core
//     returns "incomplete" rather than a violation for recharts legend labels —
//     it colours them with the SERIES colour via an inline style over several
//     translucent ancestors — so two 12px labels shipped at 3.34:1 and 3.55:1
//     against a 4.5:1 requirement. This walks the ancestor chain compositing
//     alpha, and sidesteps the false-clean trap below by resolving colours
//     through a 1px canvas instead of a regex: the browser paints `oklch()` and
//     everything else correctly, and a colour it genuinely cannot resolve comes
//     back with alpha 0 and is REPORTED, never skipped.
//   * axe-core serious/critical. Do NOT hand-roll contrast for TAILWIND colours:
//     Tailwind v4 emits `oklch()` and a naive rgb parser returned a FALSE CLEAN
//     on 32 real failures.
//   * a route rendering the 404 page FAILS. A missing route trivially passes every
//     other check, so without this the gate reports a clean sheet on a dead page.
//
// VIEWPORTS: 390 / 768 / 1280 / 1440. 1280 is not decorative — a tenth nav link
// pushed the desktop row to 1371px while it collapsed only below `xl` (1280), so
// the bug lived in the gap between a breakpoint and the width content needs.
// Test AT the breakpoints, not around them.
//
// The nav's label breakpoint (`sm`, 640px) is checked SEPARATELY, at the bottom of
// this file, at 639/640/641 on a single route. It is a global component, so
// sweeping extra widths across every route would buy nothing.
//
// Since issue #16 the app is three pages (Route Plan, Simulation, Benchmarks) plus
// the landing page, with no login. Every other path, including the removed
// sourcing-era pages, must render the 404 page; that is asserted too.

// Final pre-push gate. Drives a real browser against a LOCAL build with the live
// API proxied in, across every route and viewport, and asserts the specific
// regressions fixed today plus the global quality bars.
const { chromium } = require('playwright');
const fs=require('fs'), path=require('path');
const axeSource = fs.readFileSync(require.resolve('axe-core/axe.min.js'),'utf8');
const L=process.env.BASE||'http://localhost:4173';
const API=process.env.API||'https://supply-chain-api-qy8x.onrender.com';
// The proxy must outlast the slowest endpoint the UI fires on mount, or it aborts
// a request that was going to SUCCEED and the page renders a failure that is the
// gate's own fault. `/newsvendor/evaluation` measured 259.9s on the deployed
// instance; 180s cut it off, so it is 300s.
const PROXY_TIMEOUT=300000;
const ROUTES=['/route-plan','/simulation','/benchmarks'];
// Pages the app used to have. Each must now be an honest 404, not a crash or a redirect.
const REMOVED_ROUTES=['/login','/register','/dashboard','/components','/cart','/resilience','/model-card','/newsvendor'];
let pass=0, fail=0;
const ok=(n,c,d='')=>{c?pass++:fail++; console.log(`${c?'PASS':'FAIL'}  ${n}${d?'\n        '+d:''}`)};

const AUDIT=()=>{
  const vw=window.innerWidth;
  const desc=e=>{let s=e.tagName.toLowerCase();const c=String(e.className||'');
    if(c&&typeof e.className==='string')s+='.'+c.split(/\s+/).slice(0,5).join('.');return s.slice(0,110)};
  const scroller=[...document.querySelectorAll('body *')]
    .filter(e=>e.scrollHeight>e.clientHeight+2&&/auto|scroll/.test(getComputedStyle(e).overflowY))
    .sort((a,b)=>b.scrollHeight-a.scrollHeight)[0]||document.scrollingElement;
  const all=[...document.querySelectorAll('body *')];
  // horizontal overflow: any element crossing the viewport that is NOT inside an
  // element that legitimately scrolls horizontally (tables are allowed to)
  const overflow=all.filter(e=>{
    const r=e.getBoundingClientRect();
    if(!(r.width>0&&(r.right>vw+1||r.left<-1)))return false;
    if(e.closest('.maplibregl-map,.mapboxgl-map'))return false;     // world markers
    let n=e.parentElement;
    while(n&&n!==document.body){ if(/auto|scroll/.test(getComputedStyle(n).overflowX))return false; n=n.parentElement; }
    return true;
  }).map(e=>({sel:desc(e),right:Math.round(e.getBoundingClientRect().right),
              text:(e.textContent||'').trim().slice(0,40)})).slice(0,6);
  const emoji=all.filter(e=>[...e.childNodes].some(n=>n.nodeType===3&&
      /[\u{1F300}-\u{1FAFF}\u{2699}\u{26A0}\u{1F5FA}]/u.test(n.textContent)))
      .map(e=>(e.textContent||'').trim().slice(0,28)).slice(0,8);
  const tiny=all.filter(e=>{const cs=getComputedStyle(e);
    if(cs.display==='none'||cs.visibility==='hidden')return false;
    const own=[...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').trim();
    if(!own)return false;
    const px=parseFloat(cs.fontSize);
    // <11px is too small for anything. A 12px caption is a normal pattern; the
    // anti-pattern the design database names is sub-12px BODY text.
    return px<11 || (px<12 && own.length>60);})
    .map(e=>({sel:desc(e),size:getComputedStyle(e).fontSize,
              t:(e.textContent||'').trim().slice(0,30)})).slice(0,8);
  // Leaked JS placeholders in USER-VISIBLE text. Postmortem 2026-08-28: /frontier
  // shipped the sentence "...instead of the risk-neutral undefined." for real, to
  // production. `${riskNeutral?.n_suppliers}` interpolates the STRING "undefined"
  // when optional chaining short-circuits, and a template literal will happily
  // print it. Nothing in this gate looked at rendered words, so 10 routes x 4
  // viewports of green said nothing about it. Only own text nodes are inspected,
  // so a parent never inherits a child's match; word boundaries keep legitimate
  // prose ("undefined behaviour", "NaN is returned") from tripping it.
  const leaks=all.filter(e=>{const cs=getComputedStyle(e);
    if(cs.display==='none'||cs.visibility==='hidden')return false;
    const own=[...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').trim();
    if(!own)return false;
    return /(^|[\s\[(>:,])(undefined|NaN|null|\[object Object\])([\s\])<:,.]|$)/.test(own);})
    .map(e=>({sel:desc(e),t:(e.textContent||'').trim().slice(0,80)})).slice(0,8);
  const small=[...document.querySelectorAll('button,a,[role=button],select,summary')].filter(e=>{
    const cs=getComputedStyle(e); if(cs.display==='none'||cs.visibility==='hidden')return false;
    if(e.closest('.maplibregl-map'))return false;
    if(e.ownerSVGElement||e.closest('svg'))return false;
    if(e.tagName==='A'&&e.closest('p,li,td'))return false;
    const r=e.getBoundingClientRect(); return r.width>0&&r.height>0&&(r.width<44||r.height<44);})
    .map(e=>{const r=e.getBoundingClientRect();
      return{sel:desc(e),w:Math.round(r.width),h:Math.round(r.height),t:(e.textContent||'').trim().slice(0,24)}}).slice(0,10);
  const bars=[...document.querySelectorAll('.recharts-bar-rectangle path,.recharts-rectangle')]
    .map(e=>Math.round(e.getBoundingClientRect().height));
  // ── chart legend vs axis labels ────────────────────────────────────────────
  // Recharts renders the legend as an HTML sibling of the plot SVG. A bottom
  // legend is stacked immediately under the x-axis — the same band an
  // `insideBottom` axis label is placed into — and neither reserves space from
  // the other, so `k — minimum distinct distributors per BOM (count)` was drawn
  // straight through the legend at all four viewports. Take the plot surface by
  // its CLASS: a plain `querySelector('svg')` can return a legend swatch,
  // because recharts emits the legend wrapper BEFORE the surface in the DOM —
  // that is exactly why an earlier hand-check of this reported a clean sheet.
  const legendCharts=[...document.querySelectorAll('.recharts-wrapper')].filter(w=>{
    const lw=w.querySelector('.recharts-legend-wrapper');
    if(!lw)return false; const r=lw.getBoundingClientRect();
    return r.width>1&&r.height>1;});
  // Measure against the legend's ITEM boxes, not the wrapper. The wrapper is a
  // full-width band that includes whatever padding holds the plot off it, so a
  // y-axis tick label — which is centred on its tick and therefore always pokes
  // half its height above the plot area — reads as an overlap against the
  // wrapper while no glyph is anywhere near a legend entry. Items are what the
  // reader actually sees collide.
  const legendOverlap=legendCharts.flatMap(w=>{
    const surface=w.querySelector('svg.recharts-surface');
    const lw=w.querySelector('.recharts-legend-wrapper');
    const items=[...lw.querySelectorAll('li,.recharts-legend-item')];
    const boxes=(items.length?items:[lw]).map(e=>e.getBoundingClientRect())
      .filter(r=>r.width>1&&r.height>1);
    if(!surface||!boxes.length)return [];
    return [...surface.querySelectorAll('text')].map(t=>{
      const r=t.getBoundingClientRect();
      if(r.width<1||r.height<1)return null;
      let worst=null;
      for(const lr of boxes){
        const ox=Math.min(r.right,lr.right)-Math.max(r.left,lr.left);
        const oy=Math.min(r.bottom,lr.bottom)-Math.max(r.top,lr.top);
        if(ox>1&&oy>1&&(!worst||ox*oy>worst.ox*worst.oy))worst={ox,oy};
      }
      return worst
        ?{t:(t.textContent||'').trim().slice(0,44),
          over:`${Math.round(worst.ox)}x${Math.round(worst.oy)}`}:null;
    }).filter(Boolean);
  }).slice(0,6);

  // ── chart legend contrast ──────────────────────────────────────────────────
  // Colours are resolved by PAINTING them, not by parsing them. A hand-written
  // rgb() parser returned a false clean on 32 real failures here once, because
  // Tailwind v4 emits `oklch()`; the canvas resolves every syntax the browser
  // supports, and anything it still cannot resolve comes back with alpha 0 and
  // is REPORTED as unreadable rather than silently skipped.
  const _c=document.createElement('canvas'); _c.width=_c.height=1;
  const _x=_c.getContext('2d',{willReadFrequently:true});
  const paint=css=>{ if(!css)return null;
    _x.clearRect(0,0,1,1); _x.fillStyle='rgba(0,0,0,0)'; _x.fillStyle=css;
    _x.fillRect(0,0,1,1); const d=_x.getImageData(0,0,1,1).data;
    return {r:d[0],g:d[1],b:d[2],a:d[3]/255}; };
  const bgOf=el=>{ const st=[]; let n=el;
    while(n&&n.nodeType===1){ const c=paint(getComputedStyle(n).backgroundColor);
      if(c&&c.a>0)st.push(c); if(c&&c.a>=1)break; n=n.parentElement; }
    st.push(paint(getComputedStyle(document.body).backgroundColor)||{r:255,g:255,b:255,a:1});
    let o=st[st.length-1];
    for(let i=st.length-2;i>=0;i--){ const f=st[i];
      o={r:f.r*f.a+o.r*(1-f.a),g:f.g*f.a+o.g*(1-f.a),b:f.b*f.a+o.b*(1-f.a),a:1}; }
    return o; };
  const lumi=c=>{const f=v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)};
    return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b)};
  const cratio=(a,b)=>{const l1=lumi(a),l2=lumi(b);
    return (Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05)};
  const legendLabels=[...document.querySelectorAll('.recharts-legend-wrapper *')].filter(e=>{
    const cs=getComputedStyle(e);
    if(cs.display==='none'||cs.visibility==='hidden')return false;
    return [...e.childNodes].some(n=>n.nodeType===3&&n.textContent.trim().length>1);});
  const legendContrast=legendLabels.map(e=>{
    const cs=getComputedStyle(e); const fg=paint(cs.color);
    const t=(e.textContent||'').trim().slice(0,24);
    if(!fg||fg.a===0)return {t,color:cs.color,ratio:null,need:null,why:'colour unreadable'};
    const px=parseFloat(cs.fontSize), bold=parseInt(cs.fontWeight,10)>=700;
    const need=(px>=24||(px>=18.66&&bold))?3:4.5;      // WCAG 1.4.3 large-text carve-out
    const ratio=Math.round(cratio(fg,bgOf(e))*100)/100;
    return ratio<need?{t,color:cs.color,size:cs.fontSize,ratio,need}:null;
  }).filter(Boolean).slice(0,8);

  const svgClip=[...document.querySelectorAll('.recharts-wrapper svg')].flatMap(svg=>{
    const sr=svg.getBoundingClientRect();
    return [...svg.querySelectorAll('text')].map(t=>{const r=t.getBoundingClientRect();
      const over=Math.max(sr.left-r.left, r.right-sr.right, sr.top-r.top, r.bottom-sr.bottom);
      return over>1?{t:t.textContent.slice(0,34),over:Math.round(over)}:null}).filter(Boolean);
  }).slice(0,6);
  return {viewport:vw, scrollerW:scroller.scrollWidth, scrollerCW:scroller.clientWidth,
          overflow, emoji, tiny, leaks, small, bars, svgClip,
          nLegends:legendCharts.length, legendOverlap,
          nLegendLabels:legendLabels.length, legendContrast};
};

(async()=>{
  const b=await chromium.launch();
  const ctx=await b.newContext({viewport:{width:1440,height:900}});
  await ctx.route('**/api/v1/**', async r=>{const q=r.request();
    const u=API+'/api/v1'+q.url().split('/api/v1')[1];
    try{const res=await ctx.request.fetch(u,{method:q.method(),headers:q.headers(),
      data:q.postData()||undefined,timeout:PROXY_TIMEOUT});
      r.fulfill({status:res.status(),headers:{...res.headers(),'access-control-allow-origin':'*'},body:await res.body()});}
    // An aborted proxy fetch makes the PAGE render an error state, so it must never
    // be silent: a run that ends with "The evaluation failed" on screen and no
    // explanation in the log is a run nobody can diagnose.
    catch(e){console.log(`PROXY-ABORT ${q.method()} ${u.slice(0,96)} — ${String(e).split('\n')[0]}`); r.abort();}});
  const p=await ctx.newPage(); p.setDefaultTimeout(180000);

  // ── in-flight request tracking, for the readiness wait below ──────────────
  // FIRST-PARTY ONLY, and that distinction is load-bearing. /map streams
  // MapLibre vector tiles from `tiles-*.basemaps.cartocdn.com` for as long as it
  // is on screen; a run measured here sat 60s on a single outstanding
  // `carto.streets/v1/2/2/0.mvt`. Counting third-party basemap tiles would make
  // "has this page finished loading?" unanswerable for exactly the route where
  // it matters, which is how `networkidle` failed. Nothing is skipped by this:
  // the map's OWN data call (`/api/v1/distributors`) is first-party and is
  // counted, the basemap is decorative imagery no assertion reads, and every
  // third-party host that is ignored is printed below so this cannot hide.
  const firstParty=u=>{ try{const h=new URL(u).host;
      return h===new URL(L).host || h===new URL(API).host;}catch{return true;} };
  const thirdPartyHosts=new Set();
  const inflight=new Map();
  p.on('request',r=>{ if(firstParty(r.url()))inflight.set(r,Date.now());
    else {try{thirdPartyHosts.add(new URL(r.url()).host);}catch{}} });
  p.on('requestfinished',r=>inflight.delete(r));
  p.on('requestfailed',r=>inflight.delete(r));

  // ── navigation, DECOUPLED from readiness ──────────────────────────────────
  // This gate could not finish a run for four consecutive attempts, and the
  // cause was measured, not guessed. Every route was navigated with
  // `waitUntil:'networkidle'`, which resolves only after 500ms with ZERO
  // in-flight requests. A route that auto-fires a slow computation on mount
  // cannot reach that state in any useful time:
  //
  //   GET /api/v1/newsvendor/evaluation  reports `wall_seconds: 259.897` on the
  //     deployed free-tier instance. /newsvendor fires it on mount. The gate
  //     waited 180s, gave up, and threw — a FATAL, not a FAIL.
  //
  // The API is ONE uvicorn worker on a 0.5-CPU instance (`render.yaml`:
  // `python -m uvicorn app.main:app`), so that 260-second computation starves
  // every other request while it runs — and abandoning it at 180s does not stop
  // it. That is why the abort landed on a DIFFERENT route each run and moved
  // EARLIER each run: each aborted run left the server still burning CPU for the
  // next one. Twelve green curls of /cart said nothing about it; they were
  // served by Cloudflare (`cf-cache-status: HIT`) and never touched the API.
  //
  // So: navigate on `domcontentloaded` — the static host answers in ~250ms from
  // cache on every route, measured — and make readiness a SEPARATE, bounded,
  // REPORTED wait. Nothing is skipped and nothing is softened: a route that will
  // not navigate after three tries is a FAIL, and a route that never finishes
  // loading is a FAIL. The only thing that changed is that neither one can now
  // kill the run before the remaining checks have been allowed to speak.
  const NAV_ATTEMPTS=3;
  const gotoRoute=async(url,label)=>{
    let last=null;
    for(let i=1;i<=NAV_ATTEMPTS;i++){
      inflight.clear();                       // the old page's requests are not this page's
      try{
        await p.goto(url,{waitUntil:'domcontentloaded',timeout:60000});
        await p.waitForSelector('#root > *',{timeout:60000});   // the SPA actually mounted
        return true;
      }catch(e){
        last=String(e).split('\n')[0];
        console.log(`RETRY  navigation ${label} attempt ${i}/${NAV_ATTEMPTS}: ${last}`);
        await p.waitForTimeout(2000*i);
      }
    }
    ok(`${label}: navigable`, false, `${NAV_ATTEMPTS} attempts all failed — ${last}`);
    return false;
  };

  // How long a route may take to finish loading before "still loading" is a
  // FAILURE. Every page's first request is a cheap GET; solving, simulating and
  // tuning only happen on a click, so no route has a reason to be slow on mount.
  const SETTLE_CAP={};
  const capFor=r=>SETTLE_CAP[r]||60000;

  // Readiness = no request in flight AND no spinner still turning, held for
  // 500ms. The spinner half matters on its own: a response can arrive and the
  // section still be mid-render, and every loading state in this app renders an
  // `.animate-spin` element (several of them are a bare spinner with no text).
  const spinners=async()=>{
    try{ return await p.evaluate(()=>[...document.querySelectorAll('.animate-spin')]
      .filter(e=>{const cs=getComputedStyle(e);
        if(cs.display==='none'||cs.visibility==='hidden')return false;
        const r=e.getBoundingClientRect(); return r.width>0&&r.height>0;})
      .map(e=>((e.parentElement&&e.parentElement.textContent)||'').trim().slice(0,44))); }
    catch{ return ['<mid-navigation>']; }      // not settled, by definition
  };
  const settle=async cap=>{
    const t0=Date.now(); let quiet=null, spin=['<unmeasured>'];
    while(Date.now()-t0<cap){
      spin=inflight.size?[]:await spinners();
      if(inflight.size===0&&spin.length===0){
        if(quiet===null)quiet=Date.now();
        else if(Date.now()-quiet>=500)return{ready:true,ms:Date.now()-t0,pending:[],spin:[]};
      } else quiet=null;
      await p.waitForTimeout(250);
    }
    return{ready:false,ms:Date.now()-t0,
           pending:[...inflight.keys()].map(r=>r.url().slice(0,110)).slice(0,4),
           spin:await spinners()};
  };
  // One call site for "get to this route and be ready", so the two post-loop
  // sections cannot drift back to `networkidle`.
  const visit=async(route,label)=>{
    if(!await gotoRoute(L+route,label))return false;
    const st=await settle(capFor(route));
    ok(`${label}: finished loading within ${capFor(route)/1000}s`, st.ready,
       st.ready?'':`still busy after ${Math.round(st.ms/1000)}s — spinners=${JSON.stringify(st.spin)} pending=${JSON.stringify(st.pending)}`);
    return true;
  };
  const errs=[]; p.on('pageerror',e=>errs.push(String(e).slice(0,140)));
  p.on('console',m=>{if(m.type()==='error')errs.push('console: '+m.text().slice(0,140))});

  // ── wake the API before anything is asserted ──────────────────────────────
  // The API is a Render FREE service, so it spins down after ~15 idle minutes
  // and the next request pays a cold boot. Measured here: the first three calls
  // of the first route sat in flight for 60s while the instance started, with no
  // spinner on screen, and the run reported "/dashboard did not finish loading"
  // — a true statement about the environment and a false one about the UI.
  // Pay that cost once, out loud, before any assertion depends on it. This is
  // NOT a softened check: if the API never answers, this goes red and the whole
  // run is red, which is the correct verdict.
  {
    const t0=Date.now(); let up=false, why='';
    while(Date.now()-t0<180000 && !up){
      try{ const res=await ctx.request.fetch(API+'/version',{timeout:60000});
           if(res.status()===200) up=true; else why=`HTTP ${res.status()}`; }
      catch(e){ why=String(e).split('\n')[0]; }
      if(!up) await p.waitForTimeout(2000);
    }
    ok(`API awake before the sweep (${Math.round((Date.now()-t0)/1000)}s)`, up, why);
  }

  // ---------- public landing page `/` ----------------------------------------
  // `/` is the page a stranger actually lands on, and it exists specifically so
  // that a cold or sleeping free-tier API never blocks a first impression: it
  // is static text and three links, no fetch. It is the one route in this file
  // that must work with no API at all.
  {
    const landingNav = await gotoRoute(L+'/', '/');
    ok('/: navigable and mounts', landingNav);
    if(landingNav){
      // ── zero API requests while on `/` ─────────────────────────────────
      // This is the entire point of the page. A "public landing page" that
      // quietly fires /api/v1/... on mount is not zero-dependency, it only
      // LOOKS zero-dependency while the free-tier instance happens to be warm
      // — and it is asleep most of the time this project gets looked at cold.
      // Count from `page.on('request')`, not from the readiness `inflight`
      // map or the ctx.route proxy above: `ctx.route('**/api/v1/**', ...)`
      // intercepts and re-fulfills matching requests, but the request event
      // still fires for every request Chromium ISSUES regardless of what a
      // route handler later does to it, so this sees a call the page made
      // even though the proxy would have served it.
      let landingApiCalls = 0;
      const watchLandingApi = r => { if(/\/api\/v1|\/health/.test(r.url())) landingApiCalls++; };
      p.on('request', watchLandingApi);
      await settle(15000);
      await p.waitForTimeout(2000);
      p.off('request', watchLandingApi);
      ok('/: zero API requests while rendering', landingApiCalls===0,
         `saw ${landingApiCalls} request(s) matching /api/v1 or /health`);

      // ── a link to each of the three pages ──────────────────────────────
      const bodyText = await p.evaluate(()=>document.body.innerText);
      for(const route of ROUTES){
        const n = await p.locator(`a[href="${route}"]`).count();
        ok(`/: links to ${route}`, n>0);
      }

      // ── the retracted 47.25% figure must not resurface ──────────────────
      // Published once as the (since-archived) sourcing optimizer's edge, which it
      // was not (it was a naive-global-catalogue comparison). It must never
      // reappear on the primary landing surface.
      ok('/: the retracted 47.25% figure is not shown', !/47\.25/.test(bodyText), bodyText.match(/.{0,20}47\.25.{0,20}/)?.[0]||'');

      // ── no horizontal overflow at phone / tablet / desktop ──────────────
      // Reuses AUDIT, the same page.evaluate audit the authenticated sweep
      // below runs on every route — a second, hand-rolled overflow check here
      // could quietly diverge from it and give this one page a different bar.
      for(const w of [390,768,1440]){
        await p.setViewportSize({width:w,height:900});
        await p.waitForTimeout(400);
        const a = await p.evaluate(AUDIT);
        ok(`/: no horizontal overflow @${w}`, a.overflow.length===0,
           a.overflow.length?JSON.stringify(a.overflow.slice(0,3)):'');
      }

      // ── each link reaches a working page, client-side ───────────────────
      // `/` stays offline and main.tsx skips the warm-up there, so the app must
      // still work when the visitor navigates on without a reload.
      for(const route of ROUTES){
        await gotoRoute(L+'/', '/');
        await p.locator(`a[href="${route}"]`).first().click();
        await p.waitForURL('**'+route,{timeout:15000}).catch(()=>{});
        const landed = new URL(p.url()).pathname===route;
        const is404 = await p.evaluate(()=>/doesn't exist/.test(document.body.innerText));
        ok(`/: the ${route} link lands on a real page`, landed && !is404, p.url());
      }
    }
  }

  // ---------- document head ----------
  // Through gotoRoute like every other navigation, so that "the site is not
  // there" is a FAIL with a name on it rather than an unhandled throw.
  if(!await gotoRoute(L+'/route-plan','/route-plan')){
    console.log(`\n════ ${pass} passed, ${fail} failed ════`);
    await b.close(); process.exit(1);
  }
  const head=await p.evaluate(()=>({title:document.title,
    desc:(document.querySelector('meta[name=description]')||{}).content||null,
    og:[...document.querySelectorAll('meta[property^="og:"]')].map(m=>m.getAttribute('property'))}));
  ok('head: real <title>', head.title && !/^frontend$/i.test(head.title), JSON.stringify(head.title));
  ok('head: meta description', !!head.desc);
  ok('head: open graph tags', head.og.length>=3, head.og.join(','));

  // ── removed pages are an honest 404 ───────────────────────────────────────
  for(const route of REMOVED_ROUTES){
    if(!await gotoRoute(L+route,route)) continue;
    await p.waitForTimeout(500);
    const t=await p.evaluate(()=>document.body.innerText);
    ok(`${route}: removed page renders the 404 page`, /404/.test(t) && /doesn't exist/.test(t), t.slice(0,80));
  }

  const report={};
  for(const route of ROUTES){
    // 1280 is here because a real regression hid exactly there: the nav collapsed
    // below Tailwind's `xl` (1280px) while the full row needed 1371px, so at
    // precisely 1280 it rendered into a bar 91px too narrow. Testing 390/768/1440
    // missed it completely — the bug lived in the gap between a breakpoint and the
    // width the content actually needs. Test AT the breakpoints, not around them.
    for(const [vp,w,h] of [['d1440',1440,900],['x1280',1280,800],['t768',768,1024],['m390',390,844]]){
      await p.setViewportSize({width:w,height:h});
      if(!await visit(route,`${route} @${w}`)) continue;
      await p.waitForTimeout(vp==='d1440'?6000:3000);
      await p.evaluate(async()=>{const t=[...document.querySelectorAll('body *')]
        .filter(e=>e.scrollHeight>e.clientHeight+2&&/auto|scroll/.test(getComputedStyle(e).overflowY))
        .sort((a,b)=>b.scrollHeight-a.scrollHeight)[0]; if(!t)return;
        for(let y=0;y<=Math.min(t.scrollHeight,14000);y+=500){t.scrollTop=y;await new Promise(r=>setTimeout(r,45));}
        t.scrollTop=0;await new Promise(r=>setTimeout(r,350));});
      const notFound=await p.evaluate(()=>/page not found|404/i.test(document.body.innerText.slice(0,400)));
      if(notFound){ ok(`${route} @${w}: route exists`, false, 'renders the 404 page'); continue; }
      const a=await p.evaluate(AUDIT);
      (report[route]??={})[vp]=a;
      ok(`${route} @${w}: no horizontal overflow`, a.overflow.length===0,
         a.overflow.length?JSON.stringify(a.overflow.slice(0,3)):'');
      ok(`${route} @${w}: no emoji`, a.emoji.length===0, a.emoji.join(' | '));
      // Only asserted where a legend actually renders — a check that cannot fail
      // is worse than no check, and on the seven routes with no charted legend
      // this would be exactly that.
      if(a.nLegends>0)
        ok(`${route} @${w}: chart legend clear of the axis labels`,
           a.legendOverlap.length===0, JSON.stringify(a.legendOverlap));
      if(vp==='d1440'){
        ok(`${route}: no tiny text / small prose`, a.tiny.length===0,
           a.tiny.length?JSON.stringify(a.tiny.slice(0,4)):'');
        ok(`${route}: no clipped chart labels`, a.svgClip.length===0,
           a.svgClip.length?JSON.stringify(a.svgClip):'');
        if(a.bars.length) ok(`${route}: tallest chart bar >= 8px`, Math.max(...a.bars)>=8, JSON.stringify(a.bars));
        // Contrast does not move with the viewport, so this runs once, beside axe
        // — which does NOT cover these labels (it reports them "incomplete").
        if(a.nLegendLabels>0)
          ok(`${route}: chart legend text meets its WCAG contrast minimum`,
             a.legendContrast.length===0, JSON.stringify(a.legendContrast));
        await p.addScriptTag({content:axeSource});
        const ax=await p.evaluate(async()=>{const r=await window.axe.run(document,
          {runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}});
          return r.violations.map(v=>({id:v.id,impact:v.impact,n:v.nodes.length,
            first:(v.nodes[0]&&v.nodes[0].html||'').slice(0,90)}))});
        report[route].axe=ax;
        const serious=ax.filter(v=>v.impact==='serious'||v.impact==='critical');
        ok(`${route}: no serious/critical axe violations`, serious.length===0,
           serious.map(v=>`${v.id}(${v.n}) ${v.first}`).join(' || '));
      }
      ok(`${route} @${vp.replace(/^\D+/,'')}: no leaked undefined/NaN/null in visible text`,
         a.leaks.length===0, a.leaks.length?JSON.stringify(a.leaks.slice(0,3)):'');
      if(vp==='m390') ok(`${route} @390: touch targets >= 44px`, a.small.length===0,
         a.small.length?JSON.stringify(a.small.slice(0,4)):'');
      try{
        fs.mkdirSync(path.join(__dirname,'gate-shots'),{recursive:true});
        await p.screenshot({path:path.join(__dirname,'gate-shots',
          `${route.replace(/\W+/g,'')||'root'}-${vp}.png`)});
      }catch{}
    }
  }
  // ── the main flow actually works ──────────────────────────────────────────
  // Every check above passes on a page that renders but cannot do anything. So
  // solve the first built-in instance, then simulate the plan it produced.
  await p.setViewportSize({width:1440,height:900});
  await visit('/route-plan','/route-plan (solve)');
  await p.getByRole('button',{name:/^Solve$/}).click().catch(()=>{});
  // `waitFor`, not `isVisible`: isVisible() answers immediately and ignores its timeout.
  const drawn=await p.getByText('Route 1',{exact:true}).first().waitFor({state:'visible',timeout:60000}).then(()=>true,()=>false);
  ok('/route-plan: Solve draws the routes and their legend', drawn);
  const plotted=await p.evaluate(()=>document.querySelectorAll('svg[role=img] polyline').length);
  ok('/route-plan: one polyline per route on the x/y plot', plotted>0, `polylines=${plotted}`);
  await p.getByRole('link',{name:/Simulate this plan/}).click().catch(()=>{});
  await p.waitForURL('**/simulation',{timeout:15000}).catch(()=>{});
  await p.getByRole('button',{name:/^Run simulation$/}).click().catch(()=>{});
  const simulated=await p.getByText('On-time rate',{exact:true}).first().waitFor({state:'visible',timeout:60000}).then(()=>true,()=>false);
  ok('/simulation: the solved plan simulates and shows its KPIs', simulated);

  // ── the nav AT its own label breakpoint ───────────────────────────────────
  // Nav overflow has shipped from this repo three times; the rule written down
  // after the third is to measure AT the breakpoint and one pixel either side.
  // Three links always fit, so there is no hamburger: below `sm` (640px) the
  // labels drop and only the icons remain. Asserted per width: the labels are
  // shown on the correct side of the boundary, the bar does not scroll, and no
  // descendant paints past the viewport.
  //
  // IF NavBar's BREAKPOINT MOVES, MOVE THESE THREE WIDTHS WITH IT.
  const NAV_BREAKPOINT = 640;
  for(const w of [NAV_BREAKPOINT-1, NAV_BREAKPOINT, NAV_BREAKPOINT+1]){
    await p.setViewportSize({width:w,height:900});
    await visit('/benchmarks',`/benchmarks (nav @ ${w}px)`);
    const n=await p.evaluate(()=>{
      const bar=document.querySelector('nav[aria-label="Main"]');
      if(!bar) return {err:'no nav element'};
      const label=bar.querySelector('a span.hidden');
      const over=[...bar.querySelectorAll('*')]
        .filter(e=>{const r=e.getBoundingClientRect(); return r.width>0 && r.right>window.innerWidth+1;})
        .map(e=>`${e.tagName}@${Math.round(e.getBoundingClientRect().right)}`);
      return {found:!!label, display: label?getComputedStyle(label).display:null,
              barScrollW: bar.scrollWidth, barClientW: bar.clientWidth, past: over.slice(0,3), nPast: over.length};
    });
    ok(`nav @ ${w}px: the link labels are locatable`, n.found===true, JSON.stringify(n));
    if(n.found){
      const shouldShow = w >= NAV_BREAKPOINT;
      ok(`nav @ ${w}px: labels ${shouldShow?'shown':'hidden'} on the correct side of ${NAV_BREAKPOINT}`,
         shouldShow ? n.display!=='none' : n.display==='none', JSON.stringify(n));
    }
    ok(`nav @ ${w}px: the bar itself does not scroll horizontally`, n.barScrollW<=n.barClientW+1, JSON.stringify(n));
    ok(`nav @ ${w}px: nothing in the bar paints past the viewport`, n.nPast===0, JSON.stringify(n.past));
  }
  await p.setViewportSize({width:1440,height:900});

  console.log('\nTHIRD-PARTY HOSTS NOT COUNTED TOWARDS READINESS:',
              thirdPartyHosts.size?[...thirdPartyHosts].join(', '):'none');
  console.log('\nPAGE/CONSOLE ERRORS:', errs.length?[...new Set(errs)].slice(0,6):'none');
  ok('no console or page errors', errs.length===0);
  fs.writeFileSync(path.join(__dirname,'gate-report.json'), JSON.stringify(report,null,2));
  console.log(`\n════ ${pass} passed, ${fail} failed ════`);
  await b.close();
  process.exit(fail?1:0);
})().catch(e=>{console.error('FATAL',e);process.exit(2);});
