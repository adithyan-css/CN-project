"""
Level 6 — Dashboard server. Deliberately stdlib-only (http.server), not
Flask/FastAPI: zero extra dependencies to install on any demo laptop.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import urlparse, parse_qs


def make_dashboard_server(port: int, state_provider: Callable[[], dict],
                           send_handler: Callable[[dict], dict], html_page: str,
                           browse_handler: Optional[Callable[[Optional[str]], dict]] = None,
                           simulate_handler: Optional[Callable[[dict], dict]] = None) -> ThreadingHTTPServer:
    post_routes = {"/api/send": send_handler}
    if simulate_handler is not None:
        post_routes["/api/simulate"] = simulate_handler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/" or parsed.path == "/index.html":
                body = html_page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/state":
                self._send_json(state_provider())
            elif parsed.path == "/api/browse" and browse_handler is not None:
                requested = parse_qs(parsed.query).get("path", [None])[0]
                try:
                    self._send_json(browse_handler(requested))
                except Exception as e:
                    self._send_json({"error": str(e)}, status=400)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            handler = post_routes.get(self.path)
            if handler is not None:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    self._send_json(handler(payload))
                except Exception as e:
                    self._send_json({"error": str(e)}, status=400)
            else:
                self.send_response(404)
                self.end_headers()

    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def start_dashboard_thread(server: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PathSense</title>
<style>
:root{
  --primary:#0066cc; --primary-focus:#0071e3; --primary-on-dark:#2997ff;
  --ink:#1d1d1f; --body-on-dark:#ffffff; --body-muted:#cccccc;
  --ink-muted-80:#333333; --ink-muted-48:#7a7a7a;
  --hairline:#e0e0e0; --divider-soft:#f0f0f0;
  --canvas:#ffffff; --parchment:#f5f5f7; --pearl:#fafafc;
  --tile-1:#272729; --tile-3:#252527; --black:#000000;
  --status-good:#1d9a6c; --status-bad:#d92d20;
  --font-display:-apple-system,BlinkMacSystemFont,"SF Pro Display",Inter,system-ui,sans-serif;
  --font-text:-apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,system-ui,sans-serif;
  --font-mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:var(--parchment);color:var(--ink);font-family:var(--font-text);
  font-size:17px;line-height:1.47;letter-spacing:-0.374px;-webkit-font-smoothing:antialiased}

.nav{position:sticky;top:0;z-index:10;background:var(--black);height:44px}
.nav-inner{max-width:900px;margin:0 auto;height:100%;display:flex;align-items:center;
  justify-content:space-between;gap:12px;padding:0 24px}
.nav-logo{flex:none;color:var(--body-on-dark);font-family:var(--font-display);font-weight:600;
  font-size:15px;letter-spacing:-0.224px}
.nav-status{display:flex;align-items:center;gap:8px;color:var(--body-muted);
  font-family:var(--font-text);font-size:12px;letter-spacing:-0.12px;
  min-width:0;justify-content:flex-end}
#nodeLabel{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.dot{width:8px;height:8px;border-radius:9999px;background:var(--status-bad);flex:none;
  transition:background-color .3s}
.dot.live{background:var(--status-good);box-shadow:0 0 0 3px rgba(29,154,108,0.18)}

.hero{background:var(--parchment);padding:64px 24px 40px}
.hero-inner{max-width:900px;margin:0 auto}
.hero h1{font-family:var(--font-display);font-weight:600;font-size:34px;line-height:1.1;
  letter-spacing:-0.374px;margin:0 0 8px}
.hero p{font-family:var(--font-text);font-weight:400;font-size:17px;color:var(--ink-muted-80);
  margin:0;letter-spacing:-0.374px}

.content{max-width:900px;margin:0 auto;padding:0 24px 80px;display:flex;flex-direction:column;gap:24px}

.card{background:var(--canvas);border:1px solid var(--hairline);border-radius:18px;padding:24px}
.card-dark{background:var(--tile-1);border-color:var(--tile-1)}
.card-title{font-family:var(--font-display);font-weight:600;font-size:21px;letter-spacing:0.231px;
  margin:0 0 16px}
.card-title-dark{color:var(--body-on-dark)}

.empty-state{color:var(--ink-muted-48);font-size:14px;letter-spacing:-0.224px;padding:8px 0}

.peer-row{display:grid;grid-template-columns:1.4fr 0.8fr 0.7fr 0.9fr 1.2fr;align-items:center;
  gap:12px;padding:12px 0;border-bottom:1px solid var(--divider-soft)}
.peer-row:last-child{border-bottom:none}
.peer-row.excluded{opacity:0.5}
.peer-head{color:var(--ink-muted-48);font-size:12px;letter-spacing:-0.12px;font-weight:400;
  border-bottom:1px solid var(--hairline);padding-bottom:8px;margin-bottom:4px}
.peer-name{font-weight:600;font-size:17px;letter-spacing:-0.374px}
.peer-stat{font-size:14px;color:var(--ink-muted-80);letter-spacing:-0.224px;font-variant-numeric:tabular-nums}
.score-wrap{display:flex;align-items:center;gap:8px}
.score-track{flex:1;height:4px;border-radius:9999px;background:var(--divider-soft);overflow:hidden}
.score-fill{height:100%;background:var(--primary);border-radius:9999px;transition:width .4s}
.score-fill.bad{background:var(--status-bad)}
.score-num{font-size:14px;font-weight:600;letter-spacing:-0.224px;width:34px;text-align:right;
  font-variant-numeric:tabular-nums}
.tag-excluded{display:inline-block;font-size:12px;font-weight:600;letter-spacing:-0.12px;
  color:var(--status-bad);background:rgba(217,45,32,0.08);border-radius:9999px;padding:2px 10px;
  margin-left:8px}

.send-row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.input{font-family:var(--font-text);font-size:14px;letter-spacing:-0.224px;color:var(--ink);
  background:var(--canvas);border:1px solid rgba(0,0,0,0.12);border-radius:9999px;
  padding:11px 18px;height:44px}
.select{flex:0 0 auto;min-width:140px}
.file-input{flex:1 1 260px;min-width:200px}
.btn-primary{flex:none;font-family:var(--font-text);font-size:17px;font-weight:400;letter-spacing:-0.374px;
  color:#fff;background:var(--primary);border:none;border-radius:9999px;padding:11px 22px;
  height:44px;cursor:pointer;transition:transform .12s}
.btn-primary:active{transform:scale(0.95)}
.btn-primary:focus-visible{outline:2px solid var(--primary-focus);outline-offset:2px}
.btn-secondary{flex:none;font-family:var(--font-text);font-size:14px;font-weight:400;
  letter-spacing:-0.224px;color:var(--primary);background:transparent;
  border:1px solid var(--primary);border-radius:9999px;padding:10px 18px;
  height:44px;cursor:pointer;transition:transform .12s}
.btn-secondary:active{transform:scale(0.95)}

.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:100;
  display:flex;align-items:center;justify-content:center;padding:24px}
.modal-backdrop[hidden]{display:none}
.modal-card{background:var(--canvas);border-radius:18px;width:100%;max-width:480px;
  max-height:min(70vh,520px);display:flex;flex-direction:column;overflow:hidden;
  box-shadow:0 20px 60px rgba(0,0,0,0.3)}
.modal-head{padding:16px 20px;border-bottom:1px solid var(--hairline);
  display:flex;align-items:center;justify-content:space-between;gap:12px}
.modal-title{font-family:var(--font-display);font-weight:600;font-size:17px;flex:none}
.modal-path{font-size:12px;color:var(--ink-muted-48);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;flex:1;min-width:0;text-align:right}
.modal-close{flex:none;background:none;border:none;font-size:20px;color:var(--ink-muted-48);
  cursor:pointer;line-height:1;padding:4px}
.modal-list{overflow-y:auto;flex:1}
.modal-row{display:flex;align-items:center;gap:10px;padding:11px 20px;cursor:pointer;
  font-size:14px;letter-spacing:-0.224px;border-bottom:1px solid var(--divider-soft)}
.modal-row:last-child{border-bottom:none}
.modal-row:hover{background:var(--parchment)}
.modal-row .icon{width:18px;text-align:center;flex:none}
.modal-empty{padding:20px;color:var(--ink-muted-48);font-size:14px;text-align:center}

.transfer-idle{color:var(--ink-muted-48);font-size:14px;letter-spacing:-0.224px}
.transfer-active .t-row{display:flex;justify-content:space-between;font-size:14px;
  color:var(--ink-muted-80);letter-spacing:-0.224px;margin-bottom:6px}
.transfer-active .t-name{font-weight:600;color:var(--ink);font-size:17px;letter-spacing:-0.374px;
  margin-bottom:12px}
.progress-track{height:6px;border-radius:9999px;background:var(--divider-soft);overflow:hidden;
  margin:12px 0}
.progress-fill{height:100%;background:var(--primary);border-radius:9999px;transition:width .3s}
.status-pill{display:inline-block;font-size:12px;font-weight:600;letter-spacing:-0.12px;
  border-radius:9999px;padding:3px 10px}
.status-pill.TRANSFERRING{color:var(--primary);background:rgba(0,102,204,0.08)}
.status-pill.COMPLETE{color:var(--status-good);background:rgba(29,154,108,0.08)}
.status-pill.DEGRADED,.status-pill.FAILED{color:var(--status-bad);background:rgba(217,45,32,0.08)}
.status-pill.IDLE{color:var(--ink-muted-48);background:var(--divider-soft)}
.status-pill.PAUSED,.status-pill.INTERRUPTED{color:#b25e00;background:rgba(230,126,0,0.10)}
.status-pill.RECEIVING{color:var(--primary);background:rgba(0,102,204,0.08)}
.health{font-size:13px;letter-spacing:-0.2px;margin-top:10px;color:var(--ink-muted-80)}
.health.good b{color:var(--status-good)}
.health.bad b{color:var(--status-bad)}
.t-actions{display:flex;align-items:center;gap:12px;margin-top:14px;flex-wrap:wrap}
.t-actions .btn-secondary{font-size:13px;padding:6px 14px}
.tag-sim{display:inline-block;margin-left:8px;font-size:11px;font-weight:600;color:#b25e00;
  background:rgba(230,126,0,0.10);border-radius:9999px;padding:2px 8px;vertical-align:middle}

.events-log{font-family:var(--font-mono);font-size:12px;line-height:1.6;color:var(--body-muted);
  white-space:pre-wrap;height:180px;overflow-y:auto;letter-spacing:0}
.events-log .ev-line{color:var(--body-on-dark)}
.events-log .ev-line.dim{color:var(--body-muted)}

.footer{max-width:900px;margin:0 auto;padding:0 24px 64px;color:var(--ink-muted-48);
  font-family:var(--font-text);font-size:12px;line-height:1.3;letter-spacing:-0.12px}

@media (max-width:640px){
  .hero{padding:48px 20px 32px}
  .hero h1{font-size:28px}
  .content{padding:0 16px 64px}
  .card{padding:20px}
  .peer-row{grid-template-columns:1fr 1fr;row-gap:4px}
  .peer-head{display:none}
  .peer-stat::before{content:attr(data-label) ": "; color:var(--ink-muted-48)}
  .score-wrap{grid-column:1 / -1}
  .send-row{flex-direction:column;align-items:stretch}
  .send-row .input,.send-row .btn-primary,.send-row .btn-secondary{
    flex:0 0 auto;width:100%;min-width:0}
}
</style></head>
<body>
<nav class="nav"><div class="nav-inner">
  <span class="nav-logo">PathSense</span>
  <span class="nav-status"><span class="dot" id="statusDot"></span><span id="nodeLabel">connecting…</span></span>
</div></nav>

<header class="hero"><div class="hero-inner">
  <h1>Nearby Devices &amp; Network Health</h1>
  <p>Automatic discovery, live link health, and an agent that pauses and resumes your transfer when the link degrades.</p>
</div></header>

<main class="content">
  <section class="card">
    <h2 class="card-title">Nearby Devices</h2>
    <div class="peer-row peer-head">
      <div>Device</div><div>Latency</div><div>Loss</div><div>Throughput</div><div>Score</div>
    </div>
    <div id="peerList"><div class="empty-state">Searching for nearby devices…</div></div>
  </section>

  <section class="card">
    <h2 class="card-title">Send a File</h2>
    <div class="send-row">
      <select id="peerSelect" class="input select"></select>
      <input id="filePath" class="input file-input" placeholder="Full file path on this machine, e.g. C:\\path\\to\\file.zip">
      <button type="button" class="btn-secondary" onclick="openBrowser()">Browse…</button>
      <button class="btn-primary" onclick="sendFile()">Send</button>
    </div>
  </section>

  <section class="card">
    <h2 class="card-title">Current Transfer</h2>
    <div id="transferBody"><div class="transfer-idle">No active transfer</div></div>
  </section>

  <section class="card">
    <h2 class="card-title">Incoming Transfer</h2>
    <div id="incomingBody"><div class="transfer-idle">Nothing being received</div></div>
  </section>

  <section class="card card-dark">
    <h2 class="card-title card-title-dark">Agent Decisions</h2>
    <div id="events" class="events-log"></div>
  </section>
</main>

<footer class="footer">
  Review 2 prototype. A transfer always stays with the receiver you picked: when the link
  degrades the agent pauses it, keeps every verified chunk, and resumes from the first missing
  chunk once the link recovers. "Simulate link degradation" injects clearly labelled fake
  measurements for a demo on a healthy network. Transfer channel is plaintext (encryption is
  planned for Review 3); auto-accept is on. The file browser is for a trusted demo network only.
</footer>

<div class="modal-backdrop" id="browseModal" hidden>
  <div class="modal-card">
    <div class="modal-head">
      <span class="modal-title">Choose a file</span>
      <span class="modal-path" id="modalPath"></span>
      <button type="button" class="modal-close" onclick="closeBrowser()">&times;</button>
    </div>
    <div class="modal-list" id="modalList"></div>
  </div>
</div>

<script>
function fmt(v, digits, suffix){
  return (v === null || v === undefined) ? '—' : v.toFixed(digits) + suffix;
}
function scoreColor(sc){
  if (!sc) return '';
  if (sc.excluded) return 'bad';
  return sc.score < 0.35 ? 'bad' : '';
}
async function tick(){
  let s;
  try {
    const res = await fetch('/api/state');
    s = await res.json();
  } catch (e) {
    document.querySelector('#statusDot').className = 'dot';
    document.querySelector('#nodeLabel').textContent = 'disconnected';
    return;
  }
  document.querySelector('#statusDot').className = 'dot live';
  document.querySelector('#nodeLabel').textContent = s.name + ' · ' + s.device_id;

  const peerList = document.querySelector('#peerList');
  const sel = document.querySelector('#peerSelect');
  const prevSelected = sel.value;
  peerList.innerHTML = '';
  sel.innerHTML = '';

  const peers = s.peers || [];
  if (peers.length === 0) {
    peerList.innerHTML = '<div class="empty-state">Searching for nearby devices…</div>';
  }
  peers.forEach(p => {
    const m = (s.metrics || {})[p.device_id] || {};
    const sc = (s.scores || {})[p.device_id];
    const scorePct = sc ? Math.max(0, Math.min(100, sc.score * 100)) : 0;
    const fillClass = scoreColor(sc);
    const row = document.createElement('div');
    row.className = 'peer-row' + (sc && sc.excluded ? ' excluded' : '');
    row.innerHTML = `
      <div class="peer-name">${p.name}${sc && sc.excluded ? '<span class="tag-excluded">excluded</span>' : ''}${m.simulated ? '<span class="tag-sim">simulated</span>' : ''}</div>
      <div class="peer-stat" data-label="Latency">${fmt(m.latency_ms, 1, ' ms')}</div>
      <div class="peer-stat" data-label="Loss">${fmt(m.loss_pct, 1, '%')}</div>
      <div class="peer-stat" data-label="Throughput">${fmt(m.throughput_mbps, 1, ' Mbps')}</div>
      <div class="score-wrap">
        <div class="score-track"><div class="score-fill ${fillClass}" style="width:${scorePct}%"></div></div>
        <div class="score-num">${sc ? sc.score.toFixed(2) : '—'}</div>
      </div>`;
    peerList.appendChild(row);
    const opt = document.createElement('option');
    opt.value = p.device_id; opt.textContent = p.name;
    sel.appendChild(opt);
  });
  if (prevSelected) sel.value = prevSelected;

  const t = s.transfer;
  const body = document.querySelector('#transferBody');
  if (!t) {
    body.innerHTML = '<div class="transfer-idle">No active transfer</div>';
  } else {
    const h = t.link_health;
    const healthHtml = h ? `<div class="health ${h.healthy ? 'good' : 'bad'}">Link to ${escapeHtml(t.peer_name || '')}:
        <b>${h.healthy ? 'healthy' : 'degraded'}</b> — ${escapeHtml(h.reason)}</div>` : '';
    const active = ['TRANSFERRING','PAUSED','DEGRADED'].includes(t.status);
    const sim = t.simulation_remaining_sec > 0
      ? `<span class="tag-sim">simulating degradation · ${Math.ceil(t.simulation_remaining_sec)}s</span>` : '';
    body.innerHTML = `
      <div class="transfer-active">
        <div class="t-name">${escapeHtml(t.file_id)} → ${escapeHtml(t.peer_name || '')}</div>
        <div class="progress-track"><div class="progress-fill" style="width:${t.progress_pct}%"></div></div>
        <div class="t-row"><span>${t.acked_chunks} / ${t.total_chunks} chunks (${t.progress_pct}%)</span>
          <span class="status-pill ${t.status}">${t.status}</span></div>
        <div class="t-row"><span>Pauses: ${t.pauses} · Resumes: ${t.resumes}</span><span></span></div>
        ${healthHtml}
        ${active ? `<div class="t-actions"><button type="button" class="btn-secondary" onclick="simulate()">Simulate link degradation (8 s)</button>${sim}</div>` : ''}
      </div>`;
  }

  const inc = s.incoming;
  const incBody = document.querySelector('#incomingBody');
  if (!inc) {
    incBody.innerHTML = '<div class="transfer-idle">Nothing being received</div>';
  } else {
    const note = inc.status === 'INTERRUPTED' ? 'Link dropped. Chunks already received are kept; waiting for the sender to resume.'
               : inc.status === 'COMPLETE' ? 'Saved to ' + inc.dest_path : 'Every chunk is checked with SHA-256 before it is written.';
    incBody.innerHTML = `
      <div class="transfer-active">
        <div class="t-name">${escapeHtml(inc.name)} ← ${escapeHtml(inc.sender)}</div>
        <div class="progress-track"><div class="progress-fill" style="width:${inc.progress_pct}%"></div></div>
        <div class="t-row"><span>${inc.received} / ${inc.total_chunks} chunks (${inc.progress_pct}%) · ${(inc.size/1048576).toFixed(1)} MB</span>
          <span class="status-pill ${inc.status}">${inc.status}</span></div>
        <div class="t-row"><span>Connections: ${inc.sessions}</span><span></span></div>
        <div class="health">${escapeHtml(note)}</div>
      </div>`;
  }

  const events = (s.events || []).slice(-40);
  document.querySelector('#events').innerHTML = events
    .map(e => `<div class="ev-line">${e.replace(/</g,'&lt;')}</div>`).join('');
  const log = document.querySelector('#events');
  log.scrollTop = log.scrollHeight;
}
async function sendFile(){
  const peer_id = document.querySelector('#peerSelect').value;
  const file_path = document.querySelector('#filePath').value;
  if(!peer_id || !file_path){ alert('Pick a peer and enter a file path'); return; }
  const res = await fetch('/api/send', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({peer_id, file_path})});
  const out = await res.json();
  if(out.error) alert('Send failed: ' + out.error);
}

async function simulate(){
  const res = await fetch('/api/simulate', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({seconds: 8})});
  const out = await res.json();
  if(out.error) alert('Simulation failed: ' + out.error);
}

async function openBrowser(){
  document.querySelector('#browseModal').hidden = false;
  await loadDir(null);
}
function closeBrowser(){
  document.querySelector('#browseModal').hidden = true;
}
function escapeHtml(s){
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
async function loadDir(path){
  const list = document.querySelector('#modalList');
  list.innerHTML = '<div class="modal-empty">Loading…</div>';
  let data;
  try {
    const url = '/api/browse' + (path ? ('?path=' + encodeURIComponent(path)) : '');
    const res = await fetch(url);
    data = await res.json();
  } catch (e) {
    list.innerHTML = '<div class="modal-empty">Could not read this folder.</div>';
    return;
  }
  document.querySelector('#modalPath').textContent = data.path || '';
  list.innerHTML = '';
  if (data.parent) {
    const up = document.createElement('div');
    up.className = 'modal-row';
    up.innerHTML = '<span class="icon">⬆️</span><span>.. (up one level)</span>';
    up.onclick = () => loadDir(data.parent);
    list.appendChild(up);
  }
  const entries = data.entries || [];
  if (entries.length === 0 && !data.parent) {
    list.innerHTML += '<div class="modal-empty">This folder is empty.</div>';
  }
  entries.forEach(e => {
    const row = document.createElement('div');
    row.className = 'modal-row';
    row.innerHTML = `<span class="icon">${e.is_dir ? '📁' : '📄'}</span><span>${escapeHtml(e.name)}</span>`;
    row.onclick = () => { e.is_dir ? loadDir(e.path) : pickFile(e.path); };
    list.appendChild(row);
  });
}
function pickFile(path){
  document.querySelector('#filePath').value = path;
  closeBrowser();
}
document.querySelector('#browseModal').addEventListener('click', (ev) => {
  if (ev.target.id === 'browseModal') closeBrowser();
});

setInterval(tick, 500); tick();
if (location.hash === '#browse') openBrowser();
</script>
</body></html>"""
