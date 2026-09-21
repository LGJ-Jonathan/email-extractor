"""Spec 14: the upload page. One static page, plain HTML + fetch, no framework."""

UPLOAD_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Extractor</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Serif:wght@600&display=swap">
<style>
  :root{
    --ground:#FAF8F4; --surface:#fff; --ink:#1A1814; --muted:#6B6558; --rule:#E4DFD4;
    --accent:#17564A; --accent-soft:#E8F0EC; --amber:#8A4F0D; --amber-soft:#FBF3E4;
    --brick:#8C2F23;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--ground);color:var(--ink);
       font-family:'IBM Plex Sans',ui-sans-serif,system-ui,sans-serif}
  code,.mono{font-family:'IBM Plex Mono',ui-monospace,monospace}
  header{display:flex;align-items:baseline;gap:12px;padding:0 32px;height:72px;
         border-bottom:1px solid var(--rule)}
  header h1{font-family:'IBM Plex Serif',Georgia,serif;font-size:21px;margin:0}
  header span{font-size:12px;color:var(--muted)}
  nav{display:flex;gap:28px;padding:0 32px;height:52px;align-items:center;
      border-bottom:1px solid var(--rule);font-size:11px;letter-spacing:.09em;
      text-transform:uppercase;font-weight:600;color:#C3BCAC}
  nav .on{color:var(--accent)}
  main{max-width:1100px;margin:0 auto;padding:32px 24px 64px;display:flex;
       flex-direction:column;gap:20px}
  .card{background:var(--surface);border:1px solid var(--rule);border-radius:10px;padding:20px 22px}
  label{font-size:13px;font-weight:600;display:block;margin-bottom:8px}
  input[type=password],input[type=file],select{
    font-size:14px;padding:11px 12px;border:1px solid #D8D2C5;border-radius:7px;
    background:#FBFAF7;color:var(--ink);width:100%;min-height:44px}
  input[type=password],select{font-family:'IBM Plex Mono',ui-monospace,monospace}
  .hint{font-size:12px;color:var(--muted);margin:8px 0 0;line-height:1.5}
  .row{display:flex;gap:16px;flex-wrap:wrap}
  .row>*{flex:1 1 280px}
  button{font-family:inherit;font-size:14px;font-weight:600;padding:13px 22px;
         border-radius:8px;border:none;background:var(--accent);color:#fff;
         min-height:44px;cursor:pointer}
  button.secondary{background:var(--surface);color:var(--ink);border:1px solid #D8D2C5}
  button[disabled]{opacity:.45;cursor:not-allowed}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
     color:var(--muted);padding:10px 12px;border-bottom:1px solid var(--rule)}
  td{padding:10px 12px;border-bottom:1px solid #F0EDE5;font-family:'IBM Plex Mono',monospace}
  .pill{display:inline-block;padding:3px 9px;border-radius:5px;font-size:12px;
        font-weight:600;font-family:'IBM Plex Sans',sans-serif}
  .ok{background:var(--accent-soft);color:var(--accent)}
  .no{background:#F3F1EB;color:var(--muted)}
  .stats{display:flex;gap:28px;flex-wrap:wrap}
  .stat b{display:block;font-family:'IBM Plex Mono',monospace;font-size:22px;font-weight:500}
  .stat span{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
  .bar{height:10px;border-radius:999px;background:#EDE9E0;overflow:hidden}
  .bar i{display:block;height:100%;background:var(--accent);width:0%}
  .err{background:#F8E9E6;border:1px solid #E0C4BF;color:var(--brick);
       border-radius:8px;padding:12px 14px;font-size:13px}
  .warn{background:var(--amber-soft);border:1px solid #EBD9B8;color:var(--amber);
        border-radius:8px;padding:12px 14px;font-size:13px}
  [hidden]{display:none !important}
</style>
</head>
<body>
<header><h1>Extractor</h1><span class="mono">contact finder</span></header>
<nav>
  <span id="s1" class="on">1 &middot; Upload</span>
  <span id="s2">2 &middot; Confirm column</span>
  <span id="s3">3 &middot; Run</span>
  <span id="s4">4 &middot; Download</span>
</nav>
<main>
  <div id="error" class="err" hidden></div>

  <section id="step1" class="card">
    <div class="row">
      <div>
        <label for="key">API key</label>
        <input id="key" type="password" placeholder="X-API-Key" autocomplete="off">
        <p class="hint">Held in this browser tab only. Never written to disk or into the job.</p>
      </div>
      <div>
        <label for="file">Lead list</label>
        <input id="file" type="file" accept=".csv,.tsv,.txt,.xlsx">
        <p class="hint">CSV, TSV or XLSX. Up to 50,000 rows and 50&nbsp;MB.</p>
      </div>
    </div>
    <p style="margin:18px 0 0"><button id="previewBtn">Preview file</button></p>
  </section>

  <section id="step2" hidden>
    <div class="card">
      <div class="row" style="align-items:flex-end">
        <div>
          <label for="col">Website column</label>
          <select id="col"></select>
        </div>
        <div style="flex:0 0 auto"><span id="conf" class="pill ok"></span></div>
        <div style="flex:0 0 auto">
          <label style="font-weight:400;font-size:13px">
            <input type="checkbox" id="cache" checked style="width:auto;min-height:0">
            Reuse results crawled in the last 90 days
          </label>
        </div>
      </div>
      <div id="noCol" class="warn" style="margin-top:14px" hidden></div>
      <div class="stats" style="margin-top:18px">
        <div class="stat"><b id="n_rows">0</b><span>rows</span></div>
        <div class="stat"><b id="n_uniq">0</b><span>unique domains</span></div>
        <div class="stat"><b id="n_dupe">0</b><span>duplicates</span></div>
        <div class="stat"><b id="n_empty">0</b><span>empty</span></div>
        <div class="stat"><b id="n_bad">0</b><span>unusable</span></div>
      </div>
    </div>
    <div class="card" style="margin-top:16px;padding:0;overflow:hidden">
      <table><thead><tr><th>Value in file</th><th>Normalises to</th><th>Outcome</th></tr></thead>
      <tbody id="samples"></tbody></table>
    </div>
    <p style="margin:18px 0 0;display:flex;gap:12px">
      <button id="startBtn">Start job</button>
      <button id="backBtn" class="secondary">Back</button>
    </p>
  </section>

  <section id="step3" hidden>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <div><span id="p_done" class="mono" style="font-size:34px">0</span>
             <span style="color:var(--muted)"> of <span id="p_total">0</span> domains</span></div>
        <div class="mono" style="font-size:20px"><span id="p_pct">0</span>%</div>
      </div>
      <div class="bar" style="margin:14px 0"><i id="p_bar"></i></div>
      <div class="stats">
        <div class="stat"><b id="p_found">0</b><span>with an email</span></div>
        <div class="stat"><b id="p_failed">0</b><span>failed</span></div>
        <div class="stat"><b id="p_status">queued</b><span>job status</span></div>
      </div>
      <div id="paused" class="warn" style="margin-top:16px" hidden></div>
      <p class="hint">Polling every 5 seconds. You can close this tab &mdash; the job keeps running.</p>
      <p style="margin:18px 0 0;display:flex;gap:12px">
        <button id="dlBtn" class="secondary">Download results.csv</button>
        <button id="dlJson" class="secondary">Download JSON</button>
      </p>
    </div>
  </section>
</main>
<script>
(function(){
  var $ = function(id){ return document.getElementById(id); };
  var state = { preview:null, jobId:null, timer:null };

  try { if (sessionStorage.getItem('xkey')) $('key').value = sessionStorage.getItem('xkey'); }
  catch(e){}

  function key(){ return $('key').value.trim(); }
  function headers(){ return { 'X-API-Key': key() }; }
  function fail(msg){ $('error').textContent = msg; $('error').hidden = false; }
  function clearErr(){ $('error').hidden = true; }
  function step(n){
    ['step1','step2','step3'].forEach(function(id, i){ $(id).hidden = (i+1) !== n; });
    ['s1','s2','s3','s4'].forEach(function(id, i){
      $(id).className = (i+1) === n ? 'on' : '';
    });
  }

  $('previewBtn').onclick = function(){
    clearErr();
    if (!key()) return fail('Enter your API key.');
    var f = $('file').files[0];
    if (!f) return fail('Choose a file.');
    try { sessionStorage.setItem('xkey', key()); } catch(e){}
    var fd = new FormData(); fd.append('file', f);
    $('previewBtn').disabled = true;
    fetch('/jobs/preview', { method:'POST', headers: headers(), body: fd })
      .then(function(r){ return r.json().then(function(j){
        if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status)); return j; }); })
      .then(showPreview)
      .catch(function(e){ fail(e.message); })
      .then(function(){ $('previewBtn').disabled = false; });
  };

  function showPreview(p){
    state.preview = p;
    var sel = $('col'); sel.innerHTML = '';
    (p.columns || []).forEach(function(c){
      var o = document.createElement('option'); o.value = c; o.textContent = c;
      if (c === p.detected_column || c === p.email_column) o.selected = true;
      sel.appendChild(o);
    });
    var conf = $('conf');
    if (p.detected_column) {
      conf.className = 'pill ok';
      conf.textContent = p.confidence + ' confidence \\u00b7 ' + p.method + ' match';
      $('noCol').hidden = true;
    } else {
      conf.className = 'pill no';
      conf.textContent = 'not detected';
      $('noCol').hidden = false;
      $('noCol').textContent = p.email_column
        ? ('No website column found. "' + p.email_column + '" looks like email addresses; ' +
           'domains can be derived from it. Free providers are skipped.')
        : 'No website column detected. Pick one above.';
    }
    $('n_rows').textContent  = (p.row_count || 0).toLocaleString();
    $('n_uniq').textContent  = (p.unique_domains || 0).toLocaleString();
    $('n_dupe').textContent  = (p.duplicate_domains || 0).toLocaleString();
    $('n_empty').textContent = (p.empty_website_count || 0).toLocaleString();
    $('n_bad').textContent   = (p.invalid_count || 0).toLocaleString();
    var tb = $('samples'); tb.innerHTML = '';
    (p.samples || []).forEach(function(s){
      var tr = document.createElement('tr');
      var a = document.createElement('td'); a.textContent = s.value || '(blank)';
      a.style.color = 'var(--muted)';
      var b = document.createElement('td'); b.textContent = s.domain || '\\u2014';
      var c = document.createElement('td');
      var pill = document.createElement('span');
      pill.className = 'pill ' + (s.domain ? 'ok' : 'no');
      pill.textContent = s.domain ? 'will crawl' : ('skipped \\u00b7 ' + (s.reason || ''));
      c.appendChild(pill);
      tr.appendChild(a); tr.appendChild(b); tr.appendChild(c); tb.appendChild(tr);
    });
    step(2);
  }

  $('backBtn').onclick = function(){ step(1); };

  $('startBtn').onclick = function(){
    clearErr();
    var f = $('file').files[0];
    if (!f) return fail('Choose a file.');
    var fd = new FormData();
    fd.append('file', f);
    fd.append('column', $('col').value);
    fd.append('fresh', $('cache').checked ? 'false' : 'true');
    $('startBtn').disabled = true;
    fetch('/jobs', { method:'POST', headers: headers(), body: fd })
      .then(function(r){ return r.json().then(function(j){
        if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status)); return j; }); })
      .then(function(j){
        state.jobId = j.job_id;
        $('p_total').textContent = (j.unique_domains || 0).toLocaleString();
        step(3); poll(); state.timer = setInterval(poll, 5000);
      })
      .catch(function(e){ fail(e.message); })
      .then(function(){ $('startBtn').disabled = false; });
  };

  function poll(){
    if (!state.jobId) return;
    fetch('/jobs/' + state.jobId, { headers: headers() })
      .then(function(r){ return r.json(); })
      .then(function(j){
        var total = j.unique_domains || 0, done = j.done || 0;
        var pct = total ? Math.round(done / total * 100) : 0;
        $('p_done').textContent   = done.toLocaleString();
        $('p_total').textContent  = total.toLocaleString();
        $('p_pct').textContent    = pct;
        $('p_bar').style.width    = pct + '%';
        $('p_found').textContent  = (j.found || 0).toLocaleString();
        $('p_failed').textContent = (j.failed || 0).toLocaleString();
        $('p_status').textContent = j.status || '';
        if (j.paused) {
          $('paused').hidden = false;
          $('paused').textContent = 'Paused: ' + (j.pause_reason || 'provider error') +
            '. No domain was marked failed; queued domains keep their place.';
        } else { $('paused').hidden = true; }
        if (j.status === 'done' || j.status === 'failed') {
          clearInterval(state.timer);
          ['s1','s2','s3','s4'].forEach(function(id,i){
            $(id).className = i === 3 ? 'on' : ''; });
        }
      })
      .catch(function(){ /* transient; the next tick retries */ });
  }

  function download(path, name){
    if (!state.jobId) return;
    fetch('/jobs/' + state.jobId + path, { headers: headers() })
      .then(function(r){ return r.blob(); })
      .then(function(b){
        var u = URL.createObjectURL(b), a = document.createElement('a');
        a.href = u; a.download = name; document.body.appendChild(a); a.click();
        a.remove(); URL.revokeObjectURL(u);
      })
      .catch(function(e){ fail('Download failed: ' + e.message); });
  }
  $('dlBtn').onclick  = function(){ download('/results.csv', 'results.csv'); };
  $('dlJson').onclick = function(){ download('/results.json', 'results.jsonl'); };
})();
</script>
</body>
</html>
"""
