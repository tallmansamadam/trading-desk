#!/usr/bin/env python
"""dashboard.py — a Commodore 64 style live monitor for the trading desk.

Serves a local web UI that polls Alpaca and shows the account, the risk limits,
positions, open orders, and a zoomable price chart. Everything refreshes on a
timer and changed values flash, so a glance tells you it is still running.

  python dashboard.py                 # http://127.0.0.1:6400
  python dashboard.py --port 8080 --symbol QQQ

Read-only. It never places, cancels or modifies an order — it is a window onto
the desk, not a control panel. Order entry stays in trade_alpaca.py where the
risk engine and the permission rules live.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from trading.brokers.alpaca import AlpacaBroker, AlpacaError
from trading.config import load_settings, trading_halted

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# --- cached broker access ----------------------------------------------------

class Feed:
    """Polls Alpaca behind a short cache so the UI can refresh briskly without
    burning through the rate limit."""

    STATE_TTL = 2.0
    BARS_TTL = 20.0

    def __init__(self, broker: AlpacaBroker, settings) -> None:
        self.broker = broker
        self.settings = settings
        self._lock = threading.Lock()
        self._state: tuple[float, dict] | None = None
        self._bars: dict[tuple[str, str], tuple[float, list]] = {}
        self.ticks = 0

    def state(self) -> dict:
        with self._lock:
            now = time.time()
            if self._state and now - self._state[0] < self.STATE_TTL:
                return self._state[1]
            payload = self._build_state()
            self._state = (now, payload)
            self.ticks += 1
            payload["ticks"] = self.ticks
            return payload

    def _build_state(self) -> dict:
        s = self.settings
        out = {
            "ok": True, "error": None,
            "mode": s.mode.upper(),
            "halted": trading_halted(),
            "server_time": time.strftime("%H:%M:%S"),
            "limits": {
                "order": s.max_order_notional,
                "position": s.max_position_notional,
                "gross": getattr(s, "max_gross_notional", None),
                "daily_loss": s.max_daily_loss,
                "open_orders": s.max_open_orders,
                "restricted": list(s.restricted_symbols),
            },
        }
        try:
            acct = self.broker.account(refresh=True)
            equity = float(acct.get("equity") or 0)
            last_equity = float(acct.get("last_equity") or equity)
            out["account"] = {
                "id": acct.get("account_number", "?"),
                "equity": equity,
                "cash": float(acct.get("cash") or 0),
                "buying_power": float(acct.get("buying_power") or 0),
                "daily_pnl": equity - last_equity,
                "market_open": self.broker.is_market_open(),
            }
            items = self.broker.portfolio()
            gross = sum(abs(i.marketValue) for i in items)
            out["positions"] = [
                {"symbol": i.contract.symbol, "qty": i.position,
                 "avg": i.averageCost, "price": i.marketPrice,
                 "value": i.marketValue, "upnl": i.unrealizedPNL,
                 "pct_cap": (abs(i.marketValue) / s.max_position_notional * 100)
                            if s.max_position_notional else 0}
                for i in items
            ]
            out["gross"] = gross
            out["orders"] = [
                {"id": t.order.orderId, "symbol": t.contract.symbol,
                 "side": t.order.action, "qty": t.order.totalQuantity,
                 "type": t.order.orderType, "limit": t.order.lmtPrice,
                 "filled": t.orderStatus.filled, "status": t.orderStatus.status}
                for t in self.broker.reqAllOpenOrders()
            ]
        except AlpacaError as exc:
            out["ok"] = False
            out["error"] = str(exc).split("\n")[0]
            out.setdefault("account", {})
            out.setdefault("positions", [])
            out.setdefault("orders", [])
            out.setdefault("gross", 0)
        return out

    def bars(self, symbol: str, timeframe: str) -> list:
        key = (symbol.upper(), timeframe)
        with self._lock:
            now = time.time()
            hit = self._bars.get(key)
            if hit and now - hit[0] < self.BARS_TTL:
                return hit[1]
        try:
            limit = {"1Min": 390, "5Min": 300, "15Min": 260,
                     "1Hour": 300, "1Day": 400}.get(timeframe, 300)
            raw = self.broker.historical_bars(symbol, timeframe, limit)
            data = [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"],
                     "c": b["c"], "v": b["v"]} for b in raw]
        except AlpacaError:
            data = []
        with self._lock:
            self._bars[key] = (time.time(), data)
        return data


# --- the page ----------------------------------------------------------------

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>TRADING DESK 64</title>
<style>
:root{
  --c64-border:#7869C4; --c64-screen:#40318D; --c64-text:#B8AEEE;
  --c64-cyan:#AAFFEE; --c64-green:#AAFF66; --c64-yellow:#EEEE77;
  --c64-red:#FF7777; --c64-orange:#DD8855; --c64-white:#FFFFFF;
  --c64-grey:#8A7FD0;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--c64-border);min-height:100%}
body{
  font-family:'Cascadia Mono',Consolas,'Courier New',monospace;
  font-weight:700; letter-spacing:.06em; text-transform:uppercase;
  color:var(--c64-text); padding:18px; font-size:13px; line-height:1.5;
}
#screen{
  background:var(--c64-screen); border:14px solid var(--c64-border);
  padding:18px 20px 24px; max-width:1180px; margin:0 auto;
  position:relative; overflow:hidden;
}
/* CRT scanlines + subtle glow */
#screen::after{
  content:''; position:absolute; inset:0; pointer-events:none;
  background:repeating-linear-gradient(to bottom,
    rgba(0,0,0,.16) 0 1px, rgba(0,0,0,0) 1px 3px);
}
h1{font-size:13px;font-weight:700;text-align:center;color:var(--c64-text)}
.sub{text-align:center;color:var(--c64-grey);margin-bottom:14px}
.rule{color:var(--c64-grey);white-space:nowrap;overflow:hidden;margin:10px 0}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(270px,1fr))}
.panel{border:2px solid var(--c64-grey);padding:10px 12px 12px;position:relative}
.panel h2{
  font-size:12px;color:var(--c64-cyan);margin:-18px 0 8px 0;
  background:var(--c64-screen);display:inline-block;padding:0 6px;
}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--c64-grey);font-weight:700;padding:2px 6px 2px 0}
td{padding:2px 6px 2px 0;white-space:nowrap}
td.n,th.n{text-align:right}
.k{color:var(--c64-grey)}
.v{color:var(--c64-white)}
.pos{color:var(--c64-green)} .neg{color:var(--c64-red)}
.warn{color:var(--c64-yellow)} .bad{color:var(--c64-red)}
.ok{color:var(--c64-green)}
/* value-change flash — the strongest "it is live" cue */
@keyframes flash{0%{background:var(--c64-cyan);color:#000}100%{background:transparent}}
.flash{animation:flash .55s ease-out}
#cursor{display:inline-block;width:9px;height:15px;background:var(--c64-text);
  vertical-align:-2px;animation:blink 1.05s steps(1) infinite}
@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}
.bar{height:7px;background:#2a2065;margin-top:3px;border:1px solid var(--c64-grey)}
.bar > i{display:block;height:100%;background:var(--c64-green)}
.bar.hot > i{background:var(--c64-yellow)} .bar.over > i{background:var(--c64-red)}
#chartwrap{border:2px solid var(--c64-grey);padding:10px 12px 12px;margin-top:16px;position:relative}
#chartwrap h2{font-size:12px;color:var(--c64-cyan);margin:-18px 0 6px 0;
  background:var(--c64-screen);display:inline-block;padding:0 6px}
canvas{display:block;width:100%;height:330px;cursor:crosshair;touch-action:none}
.controls{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:6px 0 8px}
button,input,select{
  font:inherit;text-transform:uppercase;letter-spacing:.06em;
  background:var(--c64-screen);color:var(--c64-text);
  border:2px solid var(--c64-grey);padding:2px 9px;cursor:pointer;
}
button:hover,select:hover{background:var(--c64-grey);color:var(--c64-screen)}
button.on{background:var(--c64-cyan);color:#000;border-color:var(--c64-cyan)}
input{width:86px;cursor:text}
.hint{color:var(--c64-grey);font-size:11px;margin-top:6px}
#status{margin-top:14px}
#err{color:var(--c64-red);margin-top:8px;text-transform:none}
.empty{color:var(--c64-grey)}
</style></head><body>
<div id="screen">
  <h1>**** CLAUDE TRADING DESK 64 ****</h1>
  <div class="sub">64K RAM SYSTEM &nbsp;·&nbsp; RISK ENGINE ONLINE &nbsp;·&nbsp; <span id="modeline">READ-ONLY MONITOR</span></div>
  <div class="rule" id="rule1"></div>

  <div class="grid">
    <div class="panel"><h2>ACCOUNT</h2><table id="acct"></table></div>
    <div class="panel"><h2>RISK LIMITS</h2><table id="limits"></table></div>
    <div class="panel"><h2>EXPOSURE</h2><table id="expo"></table><div id="expobars"></div></div>
  </div>

  <div id="chartwrap">
    <h2>PRICE</h2>
    <div class="controls">
      <input id="sym" value="SPY" maxlength="6" title="symbol">
      <button id="load">LOAD</button>
      <span class="k">&nbsp;TF:</span>
      <button class="tf" data-tf="1Min">1M</button>
      <button class="tf" data-tf="5Min">5M</button>
      <button class="tf" data-tf="15Min">15M</button>
      <button class="tf" data-tf="1Hour">1H</button>
      <button class="tf on" data-tf="1Day">1D</button>
      <span class="k">&nbsp;</span>
      <button id="zin">ZOOM +</button>
      <button id="zout">ZOOM -</button>
      <button id="zreset">RESET</button>
      <span class="k" id="zoominfo"></span>
    </div>
    <canvas id="chart"></canvas>
    <div class="hint">WHEEL = ZOOM AT CURSOR &nbsp;·&nbsp; DRAG = PAN &nbsp;·&nbsp; SHIFT+DRAG = ZOOM TO BOX &nbsp;·&nbsp; DBL-CLICK = RESET</div>
  </div>

  <div class="grid" style="margin-top:16px">
    <div class="panel"><h2>POSITIONS</h2><table id="pos"></table></div>
    <div class="panel"><h2>OPEN ORDERS</h2><table id="ord"></table></div>
  </div>

  <div class="rule" id="rule2"></div>
  <div id="status"></div>
  <div id="err"></div>
</div>

<script>
const $ = s => document.querySelector(s);
const money = n => (n<0?"-$":"$") + Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const prev = {};

function fillRule(el){
  const w = Math.max(20, Math.floor(el.clientWidth / 7.4));
  el.textContent = "-".repeat(w);
}
function cell(key, html, cls){
  const id = 'c_'+key.replace(/\W/g,'');
  const changed = prev[key] !== undefined && prev[key] !== html;
  prev[key] = html;
  return `<td class="${cls||'v'} ${changed?'flash':''}" id="${id}">${html}</td>`;
}

/* ---------- state polling ---------- */
let ticks = 0, lastOk = 0;
async function poll(){
  try{
    const r = await fetch('/api/state', {cache:'no-store'});
    const d = await r.json();
    render(d);
    lastOk = Date.now();
    $('#err').textContent = d.error ? ('! ' + d.error) : '';
  }catch(e){
    $('#err').textContent = '! DASHBOARD LOST THE SERVER — IS dashboard.py STILL RUNNING?';
  }
  ticks++;
  renderStatus();
}
function renderStatus(){
  const age = lastOk ? Math.round((Date.now()-lastOk)/1000) : '--';
  const live = lastOk && (Date.now()-lastOk) < 12000;
  $('#status').innerHTML =
    `<span class="${live?'ok':'bad'}">${live?'● LIVE':'● STALLED'}</span> `
    + `<span class="k">POLL</span> <span class="v">${ticks}</span> `
    + `<span class="k">LAST</span> <span class="v">${age}S AGO</span> `
    + `<span class="k">READY.</span><span id="cursor"></span>`;
}
setInterval(renderStatus, 1000);

function render(d){
  const a = d.account || {}, L = d.limits || {};
  $('#modeline').textContent = `${d.mode} · ${a.market_open ? 'MARKET OPEN' : 'MARKET CLOSED'}`
    + (d.halted ? ' · HALTED' : '');

  $('#acct').innerHTML = [
    ['ACCOUNT', a.id || '—'],
    ['EQUITY', money(a.equity||0)],
    ['CASH', money(a.cash||0)],
    ['BUYING POWER', money(a.buying_power||0)],
  ].map(([k,v],i)=>`<tr><td class="k">${k}</td>${cell('a'+i,v)}</tr>`).join('')
   + `<tr><td class="k">DAY P&amp;L</td>${cell('apnl', money(a.daily_pnl||0), (a.daily_pnl||0)>=0?'pos':'neg')}</tr>`
   + `<tr><td class="k">HALT</td>${cell('ahalt', d.halted?'YES':'NO', d.halted?'bad':'ok')}</tr>`;

  $('#limits').innerHTML = [
    ['PER ORDER', money(L.order||0)],
    ['PER SYMBOL', money(L.position||0)],
    ['GROSS BOOK', L.gross==null ? 'NOT ENFORCED' : money(L.gross)],
    ['DAILY LOSS', money(L.daily_loss||0)],
    ['MAX ORDERS', String(L.open_orders||0)],
  ].map(([k,v],i)=>`<tr><td class="k">${k}</td>${cell('l'+i,v)}</tr>`).join('')
   + `<tr><td class="k">RESTRICTED</td><td class="warn" style="white-space:normal">${(L.restricted||[]).join(' ')||'—'}</td></tr>`;

  const gross = d.gross||0, eq = a.equity||1;
  const lossUsed = Math.min(100, Math.max(0, (-(a.daily_pnl||0) / (L.daily_loss||1)) * 100));
  const grossUsed = L.gross ? Math.min(100, gross / L.gross * 100) : 0;
  $('#expo').innerHTML =
      `<tr><td class="k">GROSS</td>${cell('eg', money(gross))}</tr>`
    + `<tr><td class="k">% OF EQUITY</td>${cell('ee', (gross/eq*100).toFixed(1)+'%')}</tr>`
    + `<tr><td class="k">POSITIONS</td>${cell('ep', String((d.positions||[]).length))}</tr>`
    + `<tr><td class="k">OPEN ORDERS</td>${cell('eo', String((d.orders||[]).length))}</tr>`;
  $('#expobars').innerHTML =
      `<div class="k" style="margin-top:8px">GROSS VS CAP ${grossUsed.toFixed(0)}%</div>`
    + `<div class="bar ${grossUsed>90?'over':grossUsed>60?'hot':''}"><i style="width:${grossUsed}%"></i></div>`
    + `<div class="k" style="margin-top:6px">DAILY LOSS USED ${lossUsed.toFixed(0)}%</div>`
    + `<div class="bar ${lossUsed>80?'over':lossUsed>50?'hot':''}"><i style="width:${lossUsed}%"></i></div>`;

  const P = d.positions||[];
  $('#pos').innerHTML = P.length ? (
    `<tr><th>SYM</th><th class="n">QTY</th><th class="n">PRICE</th><th class="n">VALUE</th><th class="n">UPNL</th><th class="n">%CAP</th></tr>`
    + P.map((p,i)=>`<tr><td class="v">${p.symbol}</td><td class="n v">${p.qty}</td>`
      + `<td class="n v">${p.price.toFixed(2)}</td><td class="n v">${money(p.value)}</td>`
      + `<td class="n ${p.upnl>=0?'pos':'neg'}">${money(p.upnl)}</td>`
      + `<td class="n ${p.pct_cap>100?'bad':p.pct_cap>80?'warn':'v'}">${p.pct_cap.toFixed(0)}%</td></tr>`).join('')
  ) : `<tr><td class="empty">NO POSITIONS.</td></tr>`;

  const O = d.orders||[];
  $('#ord').innerHTML = O.length ? (
    `<tr><th>ID</th><th>SYM</th><th>SIDE</th><th class="n">QTY</th><th class="n">LIMIT</th><th>STATUS</th></tr>`
    + O.map(o=>`<tr><td class="v">${o.id}</td><td class="v">${o.symbol}</td>`
      + `<td class="${o.side==='BUY'?'pos':'neg'}">${o.side}</td><td class="n v">${o.qty}</td>`
      + `<td class="n v">${o.limit?o.limit.toFixed(2):'MKT'}</td><td class="warn">${o.status}</td></tr>`).join('')
  ) : `<tr><td class="empty">NO OPEN ORDERS.</td></tr>`;
}

/* ---------- chart with zoom ---------- */
const cv = $('#chart'), ctx = cv.getContext('2d');
let bars = [], view = null, drag = null, hover = null, tf = '1Day';

function sizeCanvas(){
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio||1;
  // A hidden pane, a collapsed panel or a background tab all report width 0.
  // Sizing to that leaves a permanently blank chart, so bail and let the
  // ResizeObserver below call us again once real width arrives.
  if(r.width < 1 || r.height < 1) return;
  cv.width = r.width*dpr; cv.height = r.height*dpr;
  ctx.setTransform(dpr,0,0,dpr,0,0);
  draw();
}
async function loadBars(){
  const sym = $('#sym').value.trim().toUpperCase() || 'SPY';
  const r = await fetch(`/api/bars?symbol=${encodeURIComponent(sym)}&timeframe=${tf}`, {cache:'no-store'});
  bars = await r.json();
  view = bars.length ? {a:0, b:bars.length-1} : null;
  draw();
}
function clampView(){
  if(!view || !bars.length) return;
  const min = 4;
  if(view.b - view.a < min) view.b = view.a + min;
  if(view.a < 0){ view.b -= view.a; view.a = 0; }
  if(view.b > bars.length-1){ const d = view.b-(bars.length-1); view.a-=d; view.b-=d; }
  view.a = Math.max(0, view.a); view.b = Math.min(bars.length-1, view.b);
}
function draw(){
  const W = cv.clientWidth, H = cv.clientHeight;
  if(W < 1 || H < 1) return;   // nothing to paint into yet
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle = '#2f2470'; ctx.fillRect(0,0,W,H);
  if(!bars.length || !view){
    ctx.fillStyle='#8A7FD0'; ctx.font='700 13px Consolas,monospace';
    ctx.fillText('NO BAR DATA — PRESS LOAD', 16, 28);
    $('#zoominfo').textContent=''; return;
  }
  clampView();
  const PADL=8, PADR=62, PADT=12, PADB=22;
  const slice = bars.slice(Math.floor(view.a), Math.ceil(view.b)+1);
  let lo=Infinity, hi=-Infinity;
  for(const b of slice){ lo=Math.min(lo,b.l??b.c); hi=Math.max(hi,b.h??b.c); }
  if(!isFinite(lo)||!isFinite(hi)){ return; }
  const pad=(hi-lo)*0.08||1; lo-=pad; hi+=pad;
  const x = i => PADL + (i-view.a)/(view.b-view.a) * (W-PADL-PADR);
  const y = p => PADT + (hi-p)/(hi-lo) * (H-PADT-PADB);

  // grid + price axis
  ctx.strokeStyle='#4a3aa0'; ctx.fillStyle='#8A7FD0';
  ctx.font='700 11px Consolas,monospace'; ctx.lineWidth=1;
  for(let k=0;k<=4;k++){
    const p = lo + (hi-lo)*k/4, yy = Math.round(y(p))+.5;
    ctx.beginPath(); ctx.moveTo(PADL,yy); ctx.lineTo(W-PADR,yy); ctx.stroke();
    ctx.fillText(p.toFixed(2), W-PADR+6, yy+4);
  }
  // time axis
  const n = Math.floor(view.b)-Math.floor(view.a);
  for(let k=0;k<=3;k++){
    const i = Math.floor(view.a + n*k/3);
    if(!bars[i]) continue;
    const label = (tf==='1Day') ? bars[i].t.slice(5,10) : bars[i].t.slice(11,16);
    ctx.fillText(label, Math.min(W-PADR-34, Math.max(PADL, x(i)-16)), H-6);
  }
  // the line
  ctx.strokeStyle='#AAFFEE'; ctx.lineWidth=2; ctx.beginPath();
  let started=false;
  for(let i=Math.floor(view.a); i<=Math.ceil(view.b) && i<bars.length; i++){
    const px=x(i), py=y(bars[i].c);
    if(!started){ ctx.moveTo(px,py); started=true; } else ctx.lineTo(px,py);
  }
  ctx.stroke();
  // last price marker
  const li = Math.min(Math.ceil(view.b), bars.length-1);
  ctx.fillStyle='#EEEE77'; ctx.fillRect(x(li)-3, y(bars[li].c)-3, 6, 6);

  // crosshair readout
  if(hover!==null){
    const i = Math.round(view.a + (hover/(W-PADL-PADR))*(view.b-view.a));
    const b = bars[Math.max(0,Math.min(bars.length-1,i))];
    if(b){
      const px=x(i);
      ctx.strokeStyle='#DD8855'; ctx.setLineDash([3,3]); ctx.beginPath();
      ctx.moveTo(px,PADT); ctx.lineTo(px,H-PADB); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle='#40318D'; ctx.fillRect(8,8,232,18);
      ctx.fillStyle='#EEEE77';
      ctx.fillText(`${b.t.slice(0,16).replace('T',' ')}  C ${b.c.toFixed(2)}  V ${(b.v||0).toLocaleString()}`, 12, 21);
    }
  }
  // zoom-box preview
  if(drag && drag.box){
    ctx.fillStyle='rgba(170,255,238,.18)'; ctx.strokeStyle='#AAFFEE';
    const x0=Math.min(drag.x0,drag.x1), w=Math.abs(drag.x1-drag.x0);
    ctx.fillRect(x0,PADT,w,H-PADT-PADB); ctx.strokeRect(x0,PADT,w,H-PADT-PADB);
  }
  $('#zoominfo').textContent =
    `${slice.length} / ${bars.length} BARS`;
}
function zoomAt(frac, factor){
  if(!view) return;
  const span = view.b-view.a, centre = view.a + span*frac, ns = span*factor;
  view.a = centre - ns*frac; view.b = centre + ns*(1-frac);
  clampView(); draw();
}
cv.addEventListener('wheel', e=>{
  e.preventDefault();
  const r=cv.getBoundingClientRect();
  zoomAt((e.clientX-r.left)/r.width, e.deltaY>0 ? 1.18 : 1/1.18);
},{passive:false});
cv.addEventListener('mousedown', e=>{
  const r=cv.getBoundingClientRect();
  drag={x0:e.clientX-r.left, x1:e.clientX-r.left, box:e.shiftKey, a:view?view.a:0, b:view?view.b:0};
});
window.addEventListener('mousemove', e=>{
  const r=cv.getBoundingClientRect();
  hover = (e.clientX>=r.left&&e.clientX<=r.right&&e.clientY>=r.top&&e.clientY<=r.bottom)
        ? e.clientX-r.left : null;
  if(drag){
    drag.x1 = e.clientX-r.left;
    if(!drag.box && view){
      const span=drag.b-drag.a, dx=(drag.x1-drag.x0)/r.width*span;
      view.a=drag.a-dx; view.b=drag.b-dx; clampView();
    }
  }
  draw();
});
window.addEventListener('mouseup', ()=>{
  if(drag && drag.box && view && Math.abs(drag.x1-drag.x0)>6){
    const r=cv.getBoundingClientRect(), span=view.b-view.a;
    const f0=Math.min(drag.x0,drag.x1)/r.width, f1=Math.max(drag.x0,drag.x1)/r.width;
    const a=view.a+span*f0, b=view.a+span*f1;
    view.a=a; view.b=b; clampView();
  }
  drag=null; draw();
});
cv.addEventListener('dblclick', ()=>{ if(bars.length){ view={a:0,b:bars.length-1}; draw(); }});
$('#zin').onclick = ()=>zoomAt(.5, 1/1.4);
$('#zout').onclick = ()=>zoomAt(.5, 1.4);
$('#zreset').onclick = ()=>{ if(bars.length){ view={a:0,b:bars.length-1}; draw(); }};
$('#load').onclick = loadBars;
$('#sym').addEventListener('keydown', e=>{ if(e.key==='Enter') loadBars(); });
document.querySelectorAll('.tf').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.tf').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); tf=b.dataset.tf; loadBars();
});

window.addEventListener('resize', ()=>{
  fillRule($('#rule1')); fillRule($('#rule2')); sizeCanvas();
});
// Catches the cases a window resize event never fires for: pane revealed,
// panel expanded, tab brought to the foreground.
if(window.ResizeObserver){
  new ResizeObserver(()=>{ fillRule($('#rule1')); fillRule($('#rule2')); sizeCanvas(); })
    .observe(cv);
}
fillRule($('#rule1')); fillRule($('#rule2'));
sizeCanvas(); loadBars(); poll();
setInterval(poll, 3000);
setInterval(loadBars, 30000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    feed: Feed = None  # set on the class before serving

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif parsed.path == "/api/state":
                self._send(200, json.dumps(self.feed.state()), "application/json")
            elif parsed.path == "/api/bars":
                q = parse_qs(parsed.query)
                symbol = (q.get("symbol") or ["SPY"])[0]
                timeframe = (q.get("timeframe") or ["1Day"])[0]
                self._send(200, json.dumps(self.feed.bars(symbol, timeframe)),
                           "application/json")
            else:
                self._send(404, "not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as exc:  # never let one bad request kill the board
            self._send(500, json.dumps({"error": str(exc)}), "application/json")

    def log_message(self, *args):
        pass  # the console is for the banner, not a request log


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=6400)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--symbol", default="SPY", help="symbol the chart opens on")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    broker = AlpacaBroker(mode=settings.mode)
    account = broker.connect_and_verify()

    Handler.feed = Feed(broker, settings)
    global PAGE
    if args.symbol.upper() != "SPY":
        PAGE = PAGE.replace('id="sym" value="SPY"', f'id="sym" value="{args.symbol.upper()}"')

    url = f"http://{args.host}:{args.port}"
    print("    **** CLAUDE TRADING DESK 64 ****")
    print(f"    ACCOUNT {account}   MODE {settings.mode.upper()}")
    print(f"    SERVING {url}")
    print("    READ-ONLY. CTRL+C TO STOP.")
    print("READY.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSTOPPED.")
        server.shutdown()


if __name__ == "__main__":
    try:
        main()
    except AlpacaError as exc:
        sys.exit(str(exc))
