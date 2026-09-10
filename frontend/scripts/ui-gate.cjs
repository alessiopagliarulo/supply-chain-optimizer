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
// The nav's collapse point is checked SEPARATELY, at the bottom of this file, at
// 1399/1400/1401 on a single route. It is a global component, so sweeping three
// extra widths across all ten routes would buy nothing and cost thirty page loads
// on a gate that fronts a ~26-minute pipeline.

// Final pre-push gate. Drives a real browser against a LOCAL build with the live
// API proxied in, across every route and viewport, and asserts the specific
// regressions fixed today plus the global quality bars.
const { chromium } = require('playwright');
const fs=require('fs'), path=require('path');
const axeSource = fs.readFileSync(require.resolve('axe-core/axe.min.js'),'utf8');
const L=process.env.BASE||'http://localhost:4173';
const API='https://supply-chain-api-qy8x.onrender.com';
// The proxy must outlast the slowest endpoint the UI fires on mount, or it aborts
// a request that was going to SUCCEED and the page renders a failure that is the
// gate's own fault. `/newsvendor/evaluation` measured 259.9s on the deployed
// instance; 180s cut it off, so it is 300s.
const PROXY_TIMEOUT=300000;
const ROUTES=['/dashboard','/map','/components','/cart','/optimize','/benchmark','/resilience','/frontier','/model-card','/newsvendor'];
let pass=0, fail=0;
// "Jul 2026" — what a published data vintage has to look like.
const MONTH_YEAR=/\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(19|20)\d{2}\b/;
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
  // in-flight requests. Two routes auto-fire a solver on mount and therefore
  // cannot reach that state in any useful time:
  //
  //   GET /api/v1/newsvendor/evaluation  reports `wall_seconds: 259.897` on the
  //     deployed free-tier instance. /newsvendor fires it on mount. The gate
  //     waited 180s, gave up, and threw — a FATAL, not a FAIL.
  //   POST /api/v1/stochastic/frontier and POST /api/v1/optimize/vrp push
  //     /frontier and /optimize to ~13s and ~11s before they first go quiet.
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

  // How long each route's own solver is allowed to take before "still loading"
  // becomes a FAILURE. Sized from measurement, per route, so that a route which
  // hangs cannot cost the run 5 minutes and a route which legitimately takes 4
  // minutes is not called broken. `lru_cache` on the server means only the first
  // visit to /newsvendor pays the full price; the other three viewports are ~0.1s.
  // /newsvendor's 300000 cap was sized for the 259.9s recompute. Since 2026-08-30 all 72
  // reachable evaluations are served from docs/newsvendor.json in <4ms, so the cap is now
  // 30s -- generous for a page load, but tight enough that a regression BACK to recomputing
  // fails the gate instead of hiding inside a five-minute budget. Do not raise it to make a
  // slow page pass; a slow page IS the defect.
  const SETTLE_CAP={'/newsvendor':30000,'/frontier':180000,'/optimize':120000};
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

  // ---------- public landing page `/` — UNAUTHENTICATED, before login ------
  // `/` is the page a stranger actually lands on, and it exists specifically so
  // that a cold or sleeping free-tier API never blocks a first impression: every
  // number on it comes from `src/generated/landingData.ts`, a build-time file,
  // not a fetch. This whole block runs before the demo login below on purpose —
  // it is the one route in this file that must work with no session and no API.
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
      await p.waitForTimeout(2000);   // framer-motion `whileInView` etc. settling
      p.off('request', watchLandingApi);
      ok('/: zero API requests while rendering', landingApiCalls===0,
         `saw ${landingApiCalls} request(s) matching /api/v1 or /health`);

      // ── every published number actually rendered ───────────────────────
      // Re-derive the numbers from the SAME generated file the page imports,
      // read straight off disk — mirrors the regex-extraction approach in
      // backend/tests/test_landing_data_contract.py so this and the backend
      // contract test can never quietly disagree about how to parse the file.
      // A blank or missing stat renders nothing, and `bodyText.includes()`
      // on an empty/garbage string would silently pass — so the formatted
      // value is asserted, not just the stat's existence.
      const landingSrcPath = path.join(__dirname,'..','src','generated','landingData.ts');
      const landingSrc = fs.readFileSync(landingSrcPath,'utf8');
      const extractArray = name => {
        const m = landingSrc.match(new RegExp(`export const ${name}: \\w+\\[\\] = (\\[[\\s\\S]*?\\n\\])\\n`));
        if(!m) throw new Error(`ui-gate: could not find ${name} in ${landingSrcPath}`);
        return JSON.parse(m[1]);
      };
      const landingStats = extractArray('landingStats');
      const landingProofPoints = extractArray('landingProofPoints');
      // Same rounding rule as formatStatValue() in LandingPage.tsx — duplicated
      // here deliberately rather than imported, since this file is .cjs and the
      // component is a .tsx ES module; drift between the two would show up as
      // this check failing against a real rendered page, which is the point.
      const formatStatValue = ({value,unit}) => {
        const decimals = Number.isInteger(value) ? 0 : Math.abs(value)>=10 ? 1 : 2;
        return `${value.toFixed(decimals)}${unit}`;
      };
      const bodyText = await p.evaluate(()=>document.body.innerText);
      for(const stat of landingStats){
        const disp = formatStatValue(stat);
        ok(`/: stat "${stat.id}" renders its published value`, bodyText.includes(disp), disp);
      }

      // ── each stat shows its source path ─────────────────────────────────
      // The whole pitch of this page is "every number traces to a committed
      // artifact" — a stat with no visible source is an assertion, not a proof.
      const isArtifactSource = s => /^docs\/.+\.json →/.test(s) || s==='backend/supply_chain.db';
      const allSources = [...new Set([
        ...landingStats.map(s=>s.source),
        ...landingProofPoints.map(p=>p.source),
      ])].filter(isArtifactSource);
      const presentSources = allSources.filter(s=>bodyText.includes(s));
      ok('/: at least 6 distinct artifact source paths are printed',
         presentSources.length>=6,
         `${presentSources.length}/${allSources.length}: ${JSON.stringify(presentSources)}`);

      // ── the retracted 47.25% figure must not resurface ──────────────────
      // Published once as the optimizer's edge, which it is not (it is a
      // naive-global-catalogue comparison kept only for contrast — see
      // `naiveBaselineCaveat` in landingData.ts). It must never appear
      // unlabelled on the primary landing surface.
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

      // ── the CTA reaches a WORKING login, not a permanent spinner ────────
      // `authResolved` starts false and is only set by `initializeAuth()`,
      // which is deliberately skipped on `/`. An earlier version of this page
      // left auth bootstrapping in `App()` with `[]` deps, so navigating
      // `/` -> `/login` client-side left `PublicOnly` rendering `<AuthSplash/>`
      // forever — a permanent spinner on the primary CTA that nothing above
      // this would ever catch, since it only checks `/` in isolation. This is
      // what catches that regression coming back.
      await p.locator('a[href="/login"]').first().click();
      await p.waitForURL('**/login',{timeout:15000}).catch(()=>{});
      const onLogin = /\/login$/.test(new URL(p.url()).pathname);
      ok('/: CTA click lands on /login', onLogin, p.url());
      if(onLogin){
        const pwVisible = await p.locator('input[type="password"]').first()
          .isVisible({timeout:10000}).catch(()=>false);
        ok('/: /login renders a real form (not a permanent AuthSplash spinner)', pwVisible);
      }
    }
  }

  // ---------- document head ----------
  // Through gotoRoute like every other navigation, so that "the site is not
  // there" is a FAIL with a name on it rather than an unhandled throw.
  if(!await gotoRoute(L+'/login','/login')){
    console.log(`\n════ ${pass} passed, ${fail} failed ════`);
    await b.close(); process.exit(1);
  }
  const head=await p.evaluate(()=>({title:document.title,
    desc:(document.querySelector('meta[name=description]')||{}).content||null,
    og:[...document.querySelectorAll('meta[property^="og:"]')].map(m=>m.getAttribute('property'))}));
  ok('head: real <title>', head.title && !/^frontend$/i.test(head.title), JSON.stringify(head.title));
  ok('head: meta description', !!head.desc);
  ok('head: open graph tags', head.og.length>=3, head.og.join(','));

  // Login is a navigation too, and it died here once (`waitForURL: Timeout
  // 180000ms exceeded` waiting for **/dashboard) for the same reason: the login
  // POST was queued behind a starved API. Retry the NAVIGATION, three times, and
  // if it still will not happen say so as a FAIL — every assertion below this
  // point is authenticated, so continuing would only manufacture 200 vacuous
  // failures that hide the one real one.
  let loggedIn=false;
  for(let i=1;i<=3&&!loggedIn;i++){
    try{
      await p.getByRole('button',{name:/demo login/i}).click({timeout:30000});
      await p.waitForURL('**/dashboard',{timeout:120000});
      loggedIn=true;
    }catch(e){
      console.log(`RETRY  demo login attempt ${i}/3: ${String(e).split('\n')[0]}`);
      await p.waitForTimeout(3000*i);
      await gotoRoute(L+'/login','/login');
    }
  }
  ok('demo login reaches /dashboard', loggedIn);
  // Let the LANDING dashboard finish before the sweep starts asserting. Login
  // drops us on /dashboard, which immediately fires /components, /distributors,
  // /cart and /feeds/status; the old code navigated away a second later and
  // cancelled all four, so nothing on the API's side was ever warmed and the
  // sweep's first route then sat 60s on those same four calls — measured twice,
  // both times only on the FIRST load, every later load under a second. Absorb
  // that once, out loud, and unasserted: this is the same kind of warm-up as the
  // /version wake above. The readiness assertion itself is untouched and still
  // runs on all forty loads of the sweep.
  {
    const st=await settle(120000);
    console.log(`WARM   post-login dashboard settled=${st.ready} in ${Math.round(st.ms/1000)}s`+
                (st.ready?'':` — pending=${JSON.stringify(st.pending)}`));
  }
  if(!loggedIn){
    console.log(`\n════ ${pass} passed, ${fail} failed ════`);
    await b.close(); process.exit(1);
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
  // ── /optimize solver options: the flags must reach the API ────────────────
  // Until 2026-08-28 `optimizeAPI.vrp()` posted NO BODY, so `us_only` and
  // `graph_aware` were unreachable from the UI — the endpoint parsed two flags
  // that nothing could ever set, and every plan the site ever showed was solved
  // with both off. These assertions watch the WIRE, not the page text: a toggle
  // that renders "On" while sending nothing is precisely the failure being
  // guarded, and no amount of markup checking would catch it.
  //
  // The last assertion is the honest-UI one. A re-run must visibly resolve: the
  // cards change, or the page says the answer did not change, or it reports an
  // error. Silently returning the same screen is not an allowed outcome —
  // `graph_aware` genuinely is a no-op on some carts (raw betweenness runs small
  // across this catalogue) and the page has to say so rather than look broken.
  await p.setViewportSize({width:1440,height:900});
  await visit('/optimize','/optimize (solver options)');
  await p.waitForTimeout(6000);
  ok('/optimize: solver options panel renders',
     await p.locator('[data-testid="solver-options"]').count()===1);
  const usOnly=p.locator('[data-testid="toggle-us-only"]');
  const graphAware=p.locator('[data-testid="toggle-graph-aware"]');
  ok('/optimize: both solver flags default to off',
     (await usOnly.getAttribute('aria-pressed'))==='false' &&
     (await graphAware.getAttribute('aria-pressed'))==='false',
     'defaults must match the historical run — nothing published may move on load');
  const beforeCards=await p.locator('[data-testid="route-cards"]').innerText().catch(()=>'');
  const vrpBodies=[];
  const watchVrp=r=>{if(r.url().includes('/optimize/vrp')&&r.method()==='POST')vrpBodies.push(r.postData()||'')};
  p.on('request',watchVrp);
  await usOnly.click();
  await p.waitForTimeout(2500);
  ok('/optimize: toggling a flag re-solves', vrpBodies.length>=1, JSON.stringify(vrpBodies));
  ok('/optimize: the POST body carries us_only=true',
     /"us_only"\s*:\s*true/.test(vrpBodies.join('')), JSON.stringify(vrpBodies));
  await p.waitForFunction(()=>!/Solving sourcing MILP/i.test(document.body.innerText),
                          null,{timeout:180000});
  await p.waitForTimeout(2000);
  p.off('request',watchVrp);
  ok('/optimize: the toggle reflects the run it produced',
     (await usOnly.getAttribute('aria-pressed'))==='true');
  const afterCards=await p.locator('[data-testid="route-cards"]').innerText().catch(()=>'');
  const saidNoChange=await p.locator('[data-testid="solver-options-no-change"]').count();
  const saidError=await p.locator('[data-testid="optimize-error"]').count();
  ok('/optimize: a re-run resolves visibly (new plans, "same answer", or an error)',
     (beforeCards!==afterCards)||saidNoChange>0||saidError>0,
     `cardsChanged=${beforeCards!==afterCards} noChangeNotice=${saidNoChange} error=${saidError}`);

  // ── the macro stress banner must publish its DATA VINTAGE ─────────────────
  // The percentage in this banner is scored from ONE row of a MONTHLY feature
  // frame and prices a real stock-out surcharge into the plan above it. Until
  // 2026-08-28 it was rendered with no date at all, so a reading describing a
  // month already two months gone read as the state of the world right now.
  // Asserted only when a probability is actually on screen — when the regime
  // model is unavailable the banner prints "unavailable" and there is no claim
  // to qualify, and a check that cannot fail is worse than no check.
  {
    const sv=await p.evaluate(()=>{
      const px=e=>e?parseFloat(getComputedStyle(e).fontSize):null;
      const c=document.querySelector('[data-testid="stress-claim"]');
      const v=document.querySelector('[data-testid="stress-vintage"]');
      const n=document.querySelector('[data-testid="stress-vintage-note"]');
      return {present:!!document.querySelector('[data-testid="macro-stress-banner"]'),
              claim:(c&&c.innerText||'').trim(), claimPx:px(c),
              vintage:(v&&v.innerText||'').trim(), vintagePx:px(v),
              note:(n&&n.innerText||'').trim(), notePx:px(n),
              vintageVisible:!!(v&&v.getClientRects().length)};
    });
    if(sv.present && /\d/.test(sv.claim)){
      ok('/optimize: the macro stress banner names the observation month',
         sv.vintageVisible && MONTH_YEAR.test(sv.vintage), JSON.stringify(sv));
      ok('/optimize: the stress vintage is not smaller print than the % it qualifies',
         sv.vintagePx!==null && sv.claimPx!==null && sv.vintagePx>=sv.claimPx, JSON.stringify(sv));
      ok('/optimize: the banner says the surcharge is priced off that observation month',
         /observation month/i.test(sv.note) && sv.notePx>=sv.claimPx, JSON.stringify(sv));
    }
  }
  {
    const a=await p.evaluate(AUDIT);
    ok('/optimize after toggling a flag: no horizontal overflow', a.overflow.length===0,
       JSON.stringify(a.overflow.slice(0,3)));
    await p.addScriptTag({content:axeSource});
    const ax=await p.evaluate(async()=>{const r=await window.axe.run(document,
      {runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}});
      return r.violations.map(v=>({id:v.id,impact:v.impact,n:v.nodes.length,
        first:(v.nodes[0]&&v.nodes[0].html||'').slice(0,90)}))});
    const serious=ax.filter(v=>v.impact==='serious'||v.impact==='critical');
    ok('/optimize after toggling a flag: no serious/critical axe violations',
       serious.length===0, serious.map(v=>`${v.id}(${v.n}) ${v.first}`).join(' || '));
  }

  // ── /model-card: the same figure, the same rule ───────────────────────────
  // The model card is the page a reader lands on to check whether a number is
  // trustworthy, so it is the last place a probability may appear undated. The
  // observation month is rendered at the SAME font size as the percentage, on
  // the same line — this asserts that, in pixels, rather than trusting a class
  // name. A vintage set in smaller print than its claim has shipped from this
  // repo before and is the specific regression being guarded.
  await p.setViewportSize({width:1440,height:900});
  await visit('/model-card','/model-card (vintage)');
  await p.waitForTimeout(6000);
  {
    const figs=await p.locator('[data-testid="stress-figure"]').count();
    ok('/model-card: the macro stress figure renders', figs===1,
       'without it every vintage assertion below would be vacuous');
    if(figs===1){
      const v=await p.evaluate(()=>{
        const px=e=>e?parseFloat(getComputedStyle(e).fontSize):null;
        const f=document.querySelector('[data-testid="stress-probability"]');
        const d=document.querySelector('[data-testid="stress-vintage"]');
        return {claim:(f&&f.innerText||'').trim(), claimPx:px(f),
                vintage:(d&&d.innerText||'').trim(), vintagePx:px(d),
                visible:!!(d&&d.getClientRects().length)};
      });
      ok('/model-card: the stress probability is printed with a visible vintage',
         v.visible, JSON.stringify(v));
      if(/\d/.test(v.claim)){
        ok('/model-card: the vintage names the observation month',
           MONTH_YEAR.test(v.vintage), JSON.stringify(v));
        ok('/model-card: the vintage is not smaller print than the figure it qualifies',
           v.vintagePx!==null && v.claimPx!==null && v.vintagePx>=v.claimPx, JSON.stringify(v));
      }
    }
  }

  // ── the nav AT its own collapse point ─────────────────────────────────────
  // Nav overflow has shipped from this repo three times, and the
  // rule written down after the third was: measure AT the breakpoint and one
  // pixel either side. That rule was being followed against the WRONG NUMBER.
  // The viewport list above brackets 1280 — Tailwind's `xl`, where the nav used
  // to collapse — but `NavBar.tsx:142` now collapses at `min-[1400px]`. So the
  // gate was pinned to the location of an already-fixed bug and never touched
  // the band where the full row has to fit. A fourth recurrence would have been
  // invisible to it.
  //
  // Measured on the live site when this check was added (2026-09-04): 1399 ->
  // the desktop row is display:none and the hamburger is up; 1400 -> flex at
  // 936px inside a 1400px bar. Benign today, with 464px of headroom. The point
  // is that the next link added to the nav is now caught here instead of in
  // production.
  //
  // Asserted per width: the row's display flips on the correct side of the
  // boundary, the row does not overflow itself, the bar does not scroll, and no
  // descendant extends past the viewport. `scrollWidth > clientWidth` on the bar
  // alone is not enough — a flex child can paint outside its parent without ever
  // making the parent scroll, which is how 1371px-in-1280px looked clean.
  //
  // IF NavBar's BREAKPOINT MOVES AGAIN, MOVE THESE THREE WIDTHS WITH IT.
  const NAV_BREAKPOINT = 1400;
  for(const w of [NAV_BREAKPOINT-1, NAV_BREAKPOINT, NAV_BREAKPOINT+1]){
    await p.setViewportSize({width:w,height:900});
    await visit('/dashboard',`/dashboard (nav @ ${w}px)`);
    await p.waitForTimeout(3000);
    const n=await p.evaluate(()=>{
      const bar=document.querySelector('nav')||document.querySelector('header');
      if(!bar) return {err:'no nav element'};
      const row=[...bar.querySelectorAll('*')]
        .find(e=>String(e.className||'').includes('min-[1400px]:flex'));
      const over=[...bar.querySelectorAll('*')]
        .filter(e=>{const r=e.getBoundingClientRect();
                    return r.width>0 && r.right>window.innerWidth+1;})
        .map(e=>`${e.tagName}.${String(e.className||'').slice(0,40)}@${Math.round(e.getBoundingClientRect().right)}`);
      return {
        found:!!row,
        display: row?getComputedStyle(row).display:null,
        rowScrollW: row?row.scrollWidth:0,
        rowClientW: row?row.clientWidth:0,
        barScrollW: bar.scrollWidth,
        barClientW: bar.clientWidth,
        past: over.slice(0,3),
        nPast: over.length,
      };
    });
    ok(`nav @ ${w}px: the desktop link row is still locatable`, n.found===true,
       'the min-[1400px]:flex class is how this check finds the row — if the class '+
       'changed, this whole block went vacuous rather than red: '+JSON.stringify(n));
    if(n.found){
      const shouldShow = w >= NAV_BREAKPOINT;
      ok(`nav @ ${w}px: the row is ${shouldShow?'expanded':'collapsed'} on the correct side of ${NAV_BREAKPOINT}`,
         shouldShow ? n.display!=='none' : n.display==='none', JSON.stringify(n));
      if(shouldShow){
        ok(`nav @ ${w}px: the expanded row fits without overflowing itself`,
           n.rowScrollW<=n.rowClientW+1, JSON.stringify(n));
      }
    }
    ok(`nav @ ${w}px: the bar itself does not scroll horizontally`,
       n.barScrollW<=n.barClientW+1, JSON.stringify(n));
    ok(`nav @ ${w}px: nothing in the bar paints past the viewport`,
       n.nPast===0, JSON.stringify(n.past));
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
