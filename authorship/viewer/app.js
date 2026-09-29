/* Authorship ledger viewer. Plain ES2019, no build step, no network beyond this origin.
 *
 * Views: Stages, Reasoning, Genealogy, Branches, Replay, Review. State lives in the URL hash
 * (e.g. #view=reasoning&claim=13&seq=40). The only write is POST /api/confirm (Review).
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------------------------------
  // 0. Secret: move #k=<secret> into sessionStorage and strip it from the URL before anything else.
  var SECRET_KEY = 'authorship.viewer.secret';
  var memorySecret = null;
  (function takeSecret() {
    var raw = location.hash.replace(/^#/, '');
    if (!raw) return;
    var params = new URLSearchParams(raw);
    if (!params.has('k')) return;
    var k = params.get('k');
    params.delete('k');
    try { sessionStorage.setItem(SECRET_KEY, k); } catch (e) { memorySecret = k; }
    var rest = params.toString();
    history.replaceState(null, '', location.pathname + location.search + (rest ? '#' + rest : ''));
  })();

  function getSecret() {
    try { return sessionStorage.getItem(SECRET_KEY) || memorySecret; } catch (e) { return memorySecret; }
  }

  // ------------------------------------------------------------------------------------------
  // Constants

  var POLL_MS = 4000;
  var ELK_MAX_NODES = window.__AUTHORSHIP_ELK_MAX || 600;  // above this, deterministic swimlane positions (preset)
  var BIG_RENDER_NODES = 1500;    // above this, cytoscape performance options are switched on
  var LANES = ['Issues', 'Positions', 'Arguments', 'Decisions'];
  var LANE_OF = { issue: 0, position: 1, remark: 1, response: 1, argument: 2, action: 2, decision: 3, claim: 3 };
  var LINEAGE_TYPES = { derived_from: 1, modifies: 1, refines: 1, responds_to: 1 };
  var NEG_TYPES = { objects_to: 1, rejects: 1, discards: 1 };
  var ADOPT_TYPES = { implements: 1, supports: 1, derived_from: 1, refines: 1 };
  var DEAD_STATUS = { discarded: 1, rejected: 1 };
  var GENEALOGY_DEPTH = 10; // same as the report and the MCP lineage tool
  var GRAPH_VIEWS = { reasoning: 1, genealogy: 1, branches: 1, replay: 1 };
  var VIEWS = ['stages', 'reasoning', 'genealogy', 'branches', 'replay', 'review'];

  // ------------------------------------------------------------------------------------------
  // State

  var S = {
    view: 'stages', claim: null, seq: null, conf: false, sel: null, rv: 'reasoning',
    expanded: {}, collapsed: false
  };
  var G = null;          // last /api/graph payload
  var M = null;          // derived model
  var etag = null;
  var modelVersion = 0;
  var cy = null, cyVersion = -1, ec = null;
  var branchesVersion = -1, branchesSeq = undefined;
  var stagesVersion = -1, reviewVersion = -1;
  var playTimer = null;
  var textCache = {};
  var perf = window.__authorshipPerf = { layout: null, ready: false };

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function short(s, n) {
    s = String(s || '').replace(/\s+/g, ' ').trim();
    return s.length <= n ? s : s.slice(0, n - 1) + '…';
  }
  function stripTags(s) {
    return String(s || '').replace(/(^|\s)#(?:stage|etapa)\s+(?:"[^"]*"|\S+)/gi, ' ')
      .replace(/(^|\s)#[a-z][a-z-]*\b/gi, ' ').replace(/\s+/g, ' ').trim();
  }
  function nodeSeq(id) { return parseInt(String(id).split('.')[0], 10); }
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === 'class') e.className = attrs[k];
      else if (k === 'text') e.textContent = attrs[k];
      else e.setAttribute(k, attrs[k]);
    });
    if (text != null) e.textContent = text;
    return e;
  }

  // ------------------------------------------------------------------------------------------
  // URL hash state

  function readHash() {
    var p = new URLSearchParams(location.hash.replace(/^#/, ''));
    var v = p.get('view');
    S.view = VIEWS.indexOf(v) >= 0 ? v : 'stages';
    S.claim = p.get('claim') || null;
    var seq = p.get('seq');
    S.seq = seq && /^\d+$/.test(seq) ? parseInt(seq, 10) : null;
    S.conf = p.get('conf') === '1';
    S.sel = p.get('sel') || null;
    var rv = p.get('rv');
    S.rv = rv === 'genealogy' || rv === 'branches' ? rv : 'reasoning';
  }

  function writeHash() {
    var p = new URLSearchParams();
    p.set('view', S.view);
    if (S.claim) p.set('claim', S.claim);
    if (S.seq != null) p.set('seq', String(S.seq));
    if (S.conf) p.set('conf', '1');
    if (S.sel) p.set('sel', S.sel);
    if (S.view === 'replay' && S.rv !== 'reasoning') p.set('rv', S.rv);
    var h = '#' + p.toString();
    if (location.hash !== h) history.replaceState(null, '', location.pathname + location.search + h);
  }

  // ------------------------------------------------------------------------------------------
  // Model

  function buildModel(g) {
    var m = {
      byId: {}, entries: {}, edges: [], out: {}, inc: {}, children: {}, seqs: [], nodeList: g.nodes,
      nodeCount: g.nodes.length, limit: (g.limits && g.limits.stages_only_above) || 5000
    };
    m.stagesOnly = m.nodeCount > m.limit;
    m.big = m.nodeCount > BIG_RENDER_NODES;
    g.entries.forEach(function (e) { m.entries[e.seq] = e; });
    g.nodes.forEach(function (n) {
      n.id = String(n.id);
      n.lane = LANE_OF[n.ibis] != null ? LANE_OF[n.ibis] : 1;
      m.byId[n.id] = n;
    });
    g.nodes.forEach(function (n) {
      var pid = String(n.seq);
      n.parent = null;
      if (n.id !== pid && m.byId[pid] && m.byId[pid].ibis === 'response') {
        n.parent = pid;
        (m.children[pid] = m.children[pid] || []).push(n.id);
      }
    });
    var seen = {};
    g.edges.forEach(function (e) {
      var src = String(e.src), dst = String(e.dst);
      if (!m.byId[src] || !m.byId[dst] || src === dst) return;
      var key = src + '|' + e.type + '|' + dst;
      var x = seen[key];
      if (!x) {
        x = seen[key] = { id: 'e' + m.edges.length, src: src, dst: dst, type: e.type, sources: [] };
        m.edges.push(x);
        (m.out[src] = m.out[src] || []).push(x);
        (m.inc[dst] = m.inc[dst] || []).push(x);
      }
      if (x.sources.indexOf(e.source) < 0) x.sources.push(e.source);
    });
    m.edges.forEach(function (x) {
      x.confirmed = x.sources.indexOf('confirmed') >= 0;
      x.suggested = x.sources.length === 1 && x.sources[0] === 'annotation';
    });
    var seqSet = {};
    g.nodes.forEach(function (n) { seqSet[n.seq] = 1; });
    m.seqs = Object.keys(seqSet).map(Number).sort(function (a, b) { return a - b; });
    m.minSeq = m.seqs.length ? m.seqs[0] : 0;
    m.maxSeq = m.seqs.length ? m.seqs[m.seqs.length - 1] : 0;
    return m;
  }

  function nodeTitle(n) {
    var t = stripTags(n.label) || n.label || '';
    return t;
  }

  function isDead(n) { return !!DEAD_STATUS[n.status]; }

  // ------------------------------------------------------------------------------------------
  // Networking

  function fetchGraph() {
    var headers = {};
    if (etag) headers['If-None-Match'] = etag;
    return fetch('/api/graph', { headers: headers, cache: 'no-store' }).then(function (r) {
      if (r.status === 304) { setSync('ok'); return null; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json().then(function (g) {
        etag = r.headers.get('ETag') || g.etag;
        setSync('ok');
        onGraph(g);
        return g;
      });
    }).catch(function (err) {
      setSync('error', err);
    });
  }

  function setSync(state, err) {
    var s = $('sync');
    if (state === 'error') {
      s.textContent = 'offline: ' + (err && err.message || 'no connection');
      s.className = 'sync error';
    } else {
      s.textContent = 'updated ' + new Date().toLocaleTimeString();
      s.className = 'sync';
    }
  }

  function getText(entry) {
    var key = entry.seq;
    if (textCache[key]) return textCache[key];
    var p;
    if (entry.blob) {
      p = fetch('/api/blob/' + entry.blob, { cache: 'no-store' }).then(function (r) {
        if (!r.ok) throw new Error('blob ' + r.status);
        return r.text();
      });
    } else {
      p = fetch('/api/entry/' + entry.seq, { cache: 'no-store' }).then(function (r) {
        if (!r.ok) throw new Error('entry ' + r.status);
        return r.json();
      }).then(function (j) { return j.text || ''; });
    }
    p = p.catch(function () { delete textCache[key]; return entry.preview || ''; });
    textCache[key] = p;
    return p;
  }

  function getBlobJson(sha) {
    var key = 'b:' + sha;
    if (!textCache[key]) {
      textCache[key] = fetch('/api/blob/' + sha, { cache: 'no-store' }).then(function (r) {
        if (!r.ok) throw new Error('blob ' + r.status);
        return r.text();
      }).then(function (t) { try { return JSON.parse(t); } catch (e) { return { _raw: t }; } })
        .catch(function () { delete textCache[key]; return null; });
    }
    return textCache[key];
  }

  // ------------------------------------------------------------------------------------------
  // Graph arrival

  function onGraph(g) {
    G = g;
    M = buildModel(g);
    modelVersion++;
    $('project').textContent = g.project || '';
    renderBadge(g.chain);
    var n = (g.review || []).length;
    $('review-count').textContent = n ? String(n) : '';
    $('review-count').setAttribute('aria-label', n + ' to review');
    applyLimits();
    if (S.seq != null && S.seq >= M.maxSeq) S.seq = null;
    render();
  }

  function renderBadge(c) {
    var b = $('chain-badge'), banner = $('broken-banner');
    if (c.ok) {
      b.className = 'badge ok';
      b.textContent = '✓ chain verified · ' + c.entries + ' entries · ' +
        (c.sealed_upto ? 'sealed to #' + c.sealed_upto : 'not sealed');
      b.title = 'head ' + (c.head || '-') + (c.unsealed ? ' · ' + c.unsealed + ' unsealed' : '');
      banner.hidden = true;
    } else {
      b.className = 'badge broken';
      b.textContent = '✗ broken at #' + c.broken_at;
      b.title = c.reason || '';
      banner.textContent = 'Chain integrity check failed: broken at #' + c.broken_at + ' (' + (c.reason || 'unknown') +
        '). Entries from #' + c.broken_at + ' on cannot be trusted as evidence.';
      banner.hidden = false;
    }
  }

  function applyLimits() {
    var notice = $('notice');
    ['reasoning', 'genealogy', 'branches', 'replay'].forEach(function (v) {
      $('tab-' + v).disabled = M.stagesOnly;
    });
    if (M.stagesOnly) {
      notice.textContent = 'This ledger has ' + M.nodeCount.toLocaleString('en-US') + ' nodes, above the ' +
        M.limit.toLocaleString('en-US') + '-node limit for graph views. Showing the Stages view only.';
      notice.hidden = false;
      if (GRAPH_VIEWS[S.view]) S.view = 'stages';
    } else {
      notice.hidden = true;
    }
  }

  // ------------------------------------------------------------------------------------------
  // Tabs

  function initTabs() {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { setView(t.getAttribute('data-view')); });
      t.addEventListener('keydown', function (ev) {
        var d = ev.key === 'ArrowRight' ? 1 : ev.key === 'ArrowLeft' ? -1 : 0;
        if (ev.key === 'Home') d = -i;
        if (ev.key === 'End') d = tabs.length - 1 - i;
        if (!d) return;
        ev.preventDefault();
        var j = i;
        for (var k = 0; k < tabs.length; k++) {
          j = (j + (d > 0 ? 1 : -1) + tabs.length) % tabs.length;
          if (ev.key === 'Home') j = 0;
          if (ev.key === 'End') j = tabs.length - 1;
          if (!tabs[j].disabled) break;
        }
        tabs[j].focus();
        setView(tabs[j].getAttribute('data-view'));
      });
    });
  }

  function setView(v) {
    if (M && M.stagesOnly && GRAPH_VIEWS[v]) v = 'stages';
    if (v !== 'replay') stopPlay();
    S.view = v;
    writeHash();
    render();
  }

  function graphMode() {
    return S.view === 'replay' ? S.rv : S.view; // reasoning | genealogy | branches
  }

  function render() {
    if (!M) return;
    document.querySelectorAll('[role="tab"]').forEach(function (t) {
      var on = t.getAttribute('data-view') === S.view;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
    });
    $('panel-stages').hidden = S.view !== 'stages';
    $('panel-review').hidden = S.view !== 'review';
    $('panel-graph').hidden = !GRAPH_VIEWS[S.view];
    $('panel-graph').setAttribute('aria-labelledby', 'tab-' + (GRAPH_VIEWS[S.view] ? S.view : 'reasoning'));
    document.body.className = 'view-' + (GRAPH_VIEWS[S.view] ? graphMode() : S.view);
    if (S.view === 'stages') renderStages();
    else if (S.view === 'review') renderReview();
    else renderGraphView();
    writeHash();
  }

  // ------------------------------------------------------------------------------------------
  // Stages view

  function entryKind(e) {
    if (e.event === 'UserPromptSubmit') return 'Prompt';
    if (e.event === 'ManualNote') return 'Note';
    if (e.event === 'Stop') return 'Response';
    if (e.event === 'SubagentStop') return 'Subagent response';
    if (e.kind === 'tool') {
      var k = e.tool || 'Tool';
      if (e.file) k += ' ' + e.file;
      return k;
    }
    return e.event;
  }

  function renderStages() {
    if (stagesVersion === modelVersion) return;
    var box = $('stages');
    // keep scroll positions and expanded cards across refreshes
    var scrollLeft = box.scrollLeft, colScroll = {};
    box.querySelectorAll('.stage-col').forEach(function (c) {
      var ol = c.querySelector('ol');
      if (ol) colScroll[c.getAttribute('data-stage')] = ol.scrollTop;
    });
    stagesVersion = modelVersion;
    var cols = [], byStage = {}, unstaged = [];
    G.stages.forEach(function (s) { byStage[s.name] = []; cols.push(s); });
    G.entries.forEach(function (e) {
      if (e.actor === 'system' || e.event === 'Confirm') return;
      var st = e.stage || null;
      if (st && byStage[st]) byStage[st].push(e);
      else unstaged.push(e);
    });
    var frag = document.createDocumentFragment();
    if (unstaged.length) frag.appendChild(stageColumn('Unstaged', null, unstaged));
    cols.forEach(function (s) { frag.appendChild(stageColumn(s.name, s, byStage[s.name])); });
    if (!unstaged.length && !cols.length) frag.appendChild(el('p', { class: 'empty' }, 'No entries yet.'));
    box.textContent = '';
    box.appendChild(frag);
    box.scrollLeft = scrollLeft;
    box.querySelectorAll('.stage-col').forEach(function (c) {
      var v = colScroll[c.getAttribute('data-stage')];
      if (v) c.querySelector('ol').scrollTop = v;
    });
  }

  function stageColumn(name, stage, entries) {
    var col = el('section', { class: 'stage-col', 'data-stage': name, 'aria-label': 'Stage ' + name });
    var head = el('header');
    head.appendChild(el('h2', null, name));
    var range = entries.length ? '#' + entries[0].seq + '–#' + entries[entries.length - 1].seq : '';
    head.appendChild(el('span', { class: 'range' }, range + ' · ' + entries.length));
    col.appendChild(head);
    var ol = el('ol');
    entries.forEach(function (e) { ol.appendChild(stageCard(e)); });
    col.appendChild(ol);
    return col;
  }

  function nodeStatusChip(n) {
    if (!n || !n.status || n.status === 'open') return null;
    return el('span', { class: 'chip st-' + n.status }, n.status);
  }

  function stageCard(e) {
    var n = M.byId[String(e.seq)];
    var failed = e.kind === 'tool' && (e.outcome === 'failure' || e.outcome === 'interrupted' ||
      (n && n.ibis === 'argument' && /^tests fail/.test(n.label || '')));
    var dead = n && isDead(n);
    var li = el('li', {
      class: 'card ' + (e.actor === 'human' ? 'human' : 'ai') + (dead ? ' dead' : '') + (failed ? ' failed' : ''),
      'data-seq': String(e.seq)
    });
    var head = el('div', { class: 'card-head' });
    head.appendChild(el('i', { class: 'sh ' + (e.actor === 'human' ? 'human' : 'ai'), 'aria-hidden': 'true' }));
    head.appendChild(el('span', { class: 'seq' }, '#' + e.seq));
    head.appendChild(el('span', { class: 'kind' }, (e.actor === 'human' ? 'Human · ' : 'AI · ') + entryKind(e)));
    (e.tags || []).forEach(function (t) { head.appendChild(el('span', { class: 'chip tag' }, t)); });
    if (dead) head.appendChild(el('span', { class: 'chip st-' + n.status }, n.status === 'rejected' ? 'rejected' : 'discarded'));
    else { var c = nodeStatusChip(n); if (c) head.appendChild(c); }
    if (e.kind === 'tool' && n && n.ibis === 'argument') {
      head.appendChild(el('span', { class: 'chip ' + (failed ? 'fail' : 'pass') }, failed ? 'tests fail' : 'tests pass'));
    } else if (failed) {
      head.appendChild(el('span', { class: 'chip fail' }, e.outcome));
    }
    ((n && n.milestones) || []).forEach(function (ms) {
      if (ms.type === 'session_boundary') return;
      head.appendChild(el('span', { class: 'chip ms', title: 'tier ' + ms.tier + (ms.confirmed === 1 ? ', confirmed' : ms.confirmed === -1 ? ', rejected' : ', pending') },
        ms.type.replace(/_/g, ' ') + (ms.confirmed === 1 ? ' ✓' : ms.confirmed === -1 ? ' ✗' : ' ?')));
    });
    li.appendChild(head);
    var title = e.kind === 'tool' ? (e.command || (n && n.label) || entryKind(e)) : (e.preview || '');
    li.appendChild(el('p', { class: 'title' }, short(title, 220)));
    var kids = M.children[String(e.seq)] || [];
    if (kids.length) {
      var ul = el('ol', { class: 'opts', 'aria-label': 'Options in this response' });
      kids.forEach(function (id) {
        var k = M.byId[id];
        var item = el('li', { class: isDead(k) ? 'dead' : '' });
        item.textContent = '#' + id + ' ' + short(k.label, 140);
        var chip = nodeStatusChip(k);
        if (chip) { item.appendChild(document.createTextNode(' ')); item.appendChild(chip); }
        var why = deadReason(k);
        if (why) item.appendChild(el('span', { class: 'meta-line' }, ' ' + why));
        ul.appendChild(item);
      });
      li.appendChild(ul);
    }
    if (dead) {
      var why2 = deadReason(n);
      if (why2) li.appendChild(el('div', { class: 'meta-line' }, why2));
    }
    var btn = el('button', { type: 'button', class: 'more', 'aria-expanded': 'false' }, 'Show more');
    var body = el('div', { class: 'body' });
    body.hidden = true;
    btn.addEventListener('click', function () {
      var open = btn.getAttribute('aria-expanded') !== 'true';
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      btn.textContent = open ? 'Show less' : 'Show more';
      body.hidden = !open;
      if (open) { S.expanded[e.seq] = 1; fillCardBody(e, body); } else delete S.expanded[e.seq];
    });
    li.appendChild(btn);
    li.appendChild(body);
    if (S.expanded[e.seq]) {
      btn.setAttribute('aria-expanded', 'true');
      btn.textContent = 'Show less';
      body.hidden = false;
      fillCardBody(e, body);
    }
    return li;
  }

  function deadReason(n) {
    if (!n || !isDead(n)) return '';
    var why = (M.inc[n.id] || []).filter(function (x) { return NEG_TYPES[x.type] && !x.suggested; });
    if (why.length) {
      var src = M.byId[why[0].src];
      return (n.status === 'rejected' ? 'Rejected at #' : 'Discarded at #') + why[0].src + (src ? ': ' + short(nodeTitle(src), 90) : '');
    }
    return n.status === 'discarded' ? 'Discarded; the entry states its own reason.' : '';
  }

  function fillCardBody(e, body) {
    if (body.getAttribute('data-filled') === '1') return;
    body.setAttribute('data-filled', '1');
    body.textContent = 'Loading…';
    var parts = [];
    if (e.kind === 'tool') {
      parts.push(toolDetail(e));
    } else {
      parts.push(getText(e).then(function (t) {
        var pre = el('pre', { class: 'text' });
        pre.textContent = t;
        return pre;
      }));
    }
    Promise.all(parts).then(function (nodes) {
      body.textContent = '';
      var meta = el('div', { class: 'meta-line mono' },
        e.ts + ' · ' + e.event + ' · hash ' + (e.hash || '').slice(0, 12) + (e.session ? ' · session ' + e.session : ''));
      body.appendChild(meta);
      nodes.forEach(function (x) { if (x) body.appendChild(x); });
    });
  }

  function toolDetail(e) {
    var wrap = el('div');
    if (e.command) {
      var pre = el('pre', { class: 'text' });
      pre.textContent = '$ ' + e.command;
      wrap.appendChild(pre);
    }
    if (e.error) wrap.appendChild(el('div', { class: 'meta-line' }, 'Error: ' + short(e.error, 400)));
    var jobs = [];
    if (e.input_blob && (e.tool === 'Edit' || e.tool === 'Write' || e.tool === 'MultiEdit' || e.tool === 'NotebookEdit')) {
      jobs.push(getBlobJson(e.input_blob).then(function (inp) {
        if (!inp) return el('div', { class: 'meta-line' }, 'Input blob unavailable.');
        return renderToolDiff(e.tool, inp);
      }));
    }
    if (e.response_blob && e.tool === 'Bash') {
      jobs.push(getBlobJson(e.response_blob).then(function (resp) {
        if (!resp) return null;
        var out = [resp.stdout, resp.stderr].filter(Boolean).join('\n');
        if (!out && resp._raw) out = resp._raw;
        if (!out) return null;
        var pre = el('pre', { class: 'text' });
        pre.textContent = out.length > 6000 ? out.slice(0, 6000) + '\n…' : out;
        return pre;
      }));
    }
    return Promise.all(jobs).then(function (xs) {
      xs.forEach(function (x) { if (x) wrap.appendChild(x); });
      return wrap;
    });
  }

  function renderToolDiff(tool, inp) {
    var wrap = el('div');
    var pairs = [];
    if (tool === 'Edit') pairs.push([inp.old_string || '', inp.new_string || '']);
    else if (tool === 'MultiEdit') (inp.edits || []).forEach(function (x) { pairs.push([x.old_string || '', x.new_string || '']); });
    else if (tool === 'Write') pairs.push(['', inp.content || '']);
    else if (tool === 'NotebookEdit') pairs.push(['', inp.new_source || '']);
    var path = inp.file_path || inp.notebook_path;
    if (path) wrap.appendChild(el('div', { class: 'meta-line mono' }, path + (tool === 'Write' ? ' (written in full)' : '')));
    pairs.forEach(function (p) { wrap.appendChild(diffBlock(p[0], p[1])); });
    return wrap;
  }

  function lineDiff(a, b) {
    var A = a === '' ? [] : a.split('\n'), B = b === '' ? [] : b.split('\n');
    var n = A.length, m = B.length, out = [];
    if (n * m > 4000000) {
      A.forEach(function (x) { out.push(['-', x]); });
      B.forEach(function (x) { out.push(['+', x]); });
      return out;
    }
    var W = m + 1, L = new Uint32Array((n + 1) * W);
    for (var i = n - 1; i >= 0; i--) {
      for (var j = m - 1; j >= 0; j--) {
        L[i * W + j] = A[i] === B[j] ? L[(i + 1) * W + j + 1] + 1 : Math.max(L[(i + 1) * W + j], L[i * W + j + 1]);
      }
    }
    i = 0; j = 0;
    while (i < n && j < m) {
      if (A[i] === B[j]) { out.push([' ', A[i]]); i++; j++; }
      else if (L[(i + 1) * W + j] >= L[i * W + j + 1]) { out.push(['-', A[i]]); i++; }
      else { out.push(['+', B[j]]); j++; }
    }
    while (i < n) out.push(['-', A[i++]]);
    while (j < m) out.push(['+', B[j++]]);
    return out;
  }

  function diffBlock(a, b) {
    var pre = el('pre', { class: 'diff', 'aria-label': 'Line diff' });
    lineDiff(a, b).forEach(function (d) {
      var span = el('span', { class: d[0] === '+' ? 'add' : d[0] === '-' ? 'del' : 'ctx' });
      span.textContent = d[0] + ' ' + d[1];
      pre.appendChild(span);
    });
    return pre;
  }

  // ------------------------------------------------------------------------------------------
  // Graph panel: shared toolbar, replay bar, side panel

  function renderGraphView() {
    var mode = graphMode();
    $('tools-replay').hidden = S.view !== 'replay';
    $('replay-view').value = S.rv;
    $('tools-reasoning').hidden = mode === 'branches';
    $('gen-panel').hidden = mode !== 'genealogy';
    $('lane-labels').hidden = mode === 'branches';
    $('legend').hidden = false;
    updateReplayBar();
    if (mode === 'branches') {
      $('cy').hidden = true;
      $('branches').hidden = false;
      renderBranches();
    } else {
      $('branches').hidden = true;
      $('cy').hidden = false;
      ensureCy().then(function () {
        if (mode === 'genealogy') renderGenealogyPanel();
        applyGraphClasses();
        updateLaneLabels();
      });
    }
    renderDetail();
  }

  function updateReplayBar() {
    var sl = $('rp-slider');
    var seqs = M.seqs;
    sl.min = '0';
    sl.max = String(Math.max(0, seqs.length - 1));
    var idx = S.seq == null ? seqs.length - 1 : seqIndexAtOrBelow(S.seq);
    sl.value = String(Math.max(0, idx));
    var shown = S.seq == null ? M.nodeCount : M.nodeList.filter(function (n) { return n.seq <= S.seq; }).length;
    $('rp-label').textContent = (S.seq == null ? 'latest #' + M.maxSeq : 'up to #' + S.seq) + ' · ' + shown + '/' + M.nodeCount + ' nodes';
    $('rp-play').textContent = playTimer ? '❚❚' : '▶';
    $('rp-play').setAttribute('aria-label', playTimer ? 'Pause' : 'Play');
  }

  function seqIndexAtOrBelow(seq) {
    var s = M.seqs, lo = 0, hi = s.length - 1, ans = 0;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (s[mid] <= seq) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }

  function setReplaySeq(seq) {
    S.seq = seq == null || seq >= M.maxSeq ? null : seq;
    writeHash();
    updateReplayBar();
    var mode = graphMode();
    if (mode === 'branches') renderBranches();
    else if (cy) { applyGraphClasses(); if (mode === 'genealogy') renderGenealogyPanel(); }
  }

  function initReplay() {
    var sl = $('rp-slider');
    var pending = null;
    sl.addEventListener('input', function () {
      var v = parseInt(sl.value, 10);
      if (pending) cancelAnimationFrame(pending);
      pending = requestAnimationFrame(function () { pending = null; setReplaySeq(M.seqs[v]); });
    });
    $('rp-prev').addEventListener('click', function () { step(-1); });
    $('rp-next').addEventListener('click', function () { step(1); });
    $('rp-live').addEventListener('click', function () { stopPlay(); setReplaySeq(null); });
    $('rp-play').addEventListener('click', function () {
      if (playTimer) { stopPlay(); updateReplayBar(); return; }
      if (S.seq == null) setReplaySeq(M.seqs[0]);
      playTimer = setInterval(function () {
        if (S.seq == null) { stopPlay(); updateReplayBar(); return; }
        step(1);
      }, 600);
      updateReplayBar();
    });
    $('replay-view').addEventListener('change', function () {
      S.rv = $('replay-view').value;
      writeHash();
      render();
    });
  }

  function step(d) {
    var idx = S.seq == null ? M.seqs.length - 1 : seqIndexAtOrBelow(S.seq);
    var j = Math.max(0, Math.min(M.seqs.length - 1, idx + d));
    setReplaySeq(M.seqs[j]);
  }

  function stopPlay() {
    if (playTimer) { clearInterval(playTimer); playTimer = null; }
  }

  function selectNode(id, center) {
    S.sel = id;
    writeHash();
    renderDetail();
    if (cy) {
      cy.nodes(':selected').unselect();
      var n = cy.getElementById(id);
      if (n && n.nonempty()) {
        n.select();
        if (center && graphMode() !== 'branches') cy.animate({ center: { eles: n }, duration: 250 });
      }
    }
    if (graphMode() === 'branches') markBranchSelection();
  }

  function renderDetail() {
    var box = $('detail');
    var n = S.sel && M.byId[S.sel];
    box.textContent = '';
    if (!n) {
      box.appendChild(el('p', { class: 'hint' }, 'Select a node to see its entry.'));
      return;
    }
    var e = M.entries[n.seq] || {};
    var h = el('h2');
    h.appendChild(el('i', { class: 'sh ' + (n.author === 'human' ? 'human' : 'ai'), 'aria-hidden': 'true' }));
    h.appendChild(document.createTextNode('#' + n.id + ' · ' + (n.author === 'human' ? 'Human' : 'AI') + ' ' + n.ibis));
    box.appendChild(h);
    var dl = el('dl');
    function row(k, v) { if (v == null || v === '') return; dl.appendChild(el('dt', null, k)); dl.appendChild(el('dd', null, v)); }
    row('Lane', LANES[n.lane]);
    row('Stage', n.stage || 'Unstaged');
    row('Status', n.status);
    row('Maturity', n.maturity);
    row('Event', n.event);
    row('Time', n.ts);
    row('Hash', (n.hash || '').slice(0, 16));
    row('Tags', (n.tags || []).join(' '));
    if (n.file) row('File', n.file);
    box.appendChild(dl);
    var why = deadReason(n);
    if (why) box.appendChild(el('p', { class: 'meta-line' }, why));
    if ((n.milestones || []).length) {
      box.appendChild(el('h3', null, 'Milestones'));
      var ul = el('ul');
      n.milestones.forEach(function (m) {
        ul.appendChild(el('li', null, m.type.replace(/_/g, ' ') + ' · tier ' + m.tier +
          (m.score != null ? ' · ' + Number(m.score).toFixed(2) : '') + ' · ' +
          (m.confirmed === 1 ? 'confirmed' : m.confirmed === -1 ? 'rejected' : 'pending') +
          (m.evidence_seq ? ' · evidence #' + m.evidence_seq : '')));
      });
      box.appendChild(ul);
    }
    box.appendChild(el('h3', null, 'Text'));
    var pre = el('pre', { class: 'text' });
    if (n.id !== String(n.seq)) {
      pre.textContent = n.label;
    } else {
      pre.textContent = e.preview || n.label || '';
      if (e.seq) getText(e).then(function (t) { if (S.sel === n.id) pre.textContent = t; });
    }
    box.appendChild(pre);
    edgeList(box, 'Outgoing', M.out[n.id] || [], 'dst');
    edgeList(box, 'Incoming', M.inc[n.id] || [], 'src');
  }

  function edgeList(box, title, list, end) {
    if (!list.length) return;
    box.appendChild(el('h3', null, title));
    var ul = el('ul');
    list.forEach(function (x) {
      var li = el('li');
      var other = x[end];
      var b = el('button', { type: 'button', class: 'linkish' }, '#' + other);
      b.addEventListener('click', function () { selectNode(other, true); });
      li.appendChild(document.createTextNode(x.type.replace(/_/g, ' ') + ' '));
      li.appendChild(b);
      li.appendChild(document.createTextNode(' · ' + x.sources.join(', ')));
      ul.appendChild(li);
    });
    box.appendChild(ul);
  }

  // ------------------------------------------------------------------------------------------
  // Reasoning (cytoscape + elkjs)

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function cyStyle(big) {
    var C = {
      text: css('--text'), panel: css('--panel'), human: css('--human'), ai: css('--ai'), aiBg: css('--ai-bg'),
      ok: css('--ok'), danger: css('--danger'), lineage: css('--lineage'), edge: css('--edge'),
      lane1: css('--lane-1'), lane2: css('--lane-2'), hl: css('--highlight'), focus: css('--focus'), muted: css('--muted')
    };
    return [
      { selector: 'node', style: {
        'width': 24, 'height': 24, 'label': 'data(label)', 'font-size': big ? 10 : 11, 'color': C.text,
        'text-valign': 'bottom', 'text-halign': 'center', 'text-margin-y': 3, 'text-wrap': 'wrap',
        'text-max-width': big ? 58 : 96, 'min-zoomed-font-size': big ? 9 : 5, 'border-width': 2,
        'z-index-compare': 'manual', 'z-index': 30, 'text-background-color': C.panel, 'text-background-opacity': 0.75,
        'text-background-padding': 1, 'overlay-opacity': 0 } },
      { selector: 'node.human', style: { 'shape': 'ellipse', 'background-color': C.human, 'border-color': C.human } },
      { selector: 'node.ai', style: { 'shape': 'round-rectangle', 'background-color': C.ai, 'border-color': C.ai } },
      { selector: 'node.dead', style: { 'border-style': 'dashed', 'background-color': C.panel, 'border-width': 2.5 } },
      { selector: 'node.claim', style: { 'width': 30, 'height': 30, 'border-style': 'double', 'border-width': 5, 'border-color': C.text } },
      { selector: 'node.decision', style: { 'border-color': C.text } },
      { selector: 'node.group', style: {
        'shape': 'round-rectangle', 'background-color': C.aiBg, 'background-opacity': 0.55, 'border-color': C.ai,
        'border-width': 1.5, 'border-style': 'solid', 'padding': 8, 'text-valign': 'top', 'text-halign': 'center',
        'text-margin-y': -3, 'z-index': 20, 'font-size': 10 } },
      { selector: 'node.cy-expand-collapse-collapsed-node', style: {
        'shape': 'round-rectangle', 'width': 28, 'height': 28, 'background-color': C.ai, 'background-opacity': 1,
        'border-style': 'double', 'border-width': 5, 'border-color': C.ai, 'label': 'data(clabel)', 'text-valign': 'bottom',
        'text-margin-y': 3 } },
      { selector: 'node.lane', style: {
        'shape': 'rectangle', 'width': 'data(w)', 'height': 'data(h)', 'background-color': C.lane1, 'border-width': 0,
        'label': '', 'events': 'no', 'z-index': 0, 'z-compound-depth': 'bottom' } },
      { selector: 'node.lane.alt', style: { 'background-color': C.lane2 } },
      { selector: 'edge', style: {
        'width': 1.2, 'line-color': C.edge, 'target-arrow-color': C.edge, 'target-arrow-shape': big ? 'none' : 'triangle',
        'arrow-scale': 0.75, 'curve-style': big ? 'haystack' : 'bezier', 'opacity': 0.8, 'z-index-compare': 'manual', 'z-index': 10,
        'overlay-opacity': 0 } },
      { selector: 'edge.implements, edge.supports', style: { 'line-color': C.ok, 'target-arrow-color': C.ok } },
      { selector: 'edge.modifies, edge.refines, edge.derived_from', style: { 'line-color': C.lineage, 'target-arrow-color': C.lineage } },
      { selector: 'edge.neg', style: { 'line-style': 'dashed', 'line-dash-pattern': [6, 4], 'line-color': C.danger, 'target-arrow-color': C.danger } },
      { selector: 'edge.confirmed', style: { 'width': 2.2, 'opacity': 0.95 } },
      { selector: 'edge.suggested', style: { 'opacity': 0.35 } },
      { selector: '.future', style: { 'display': 'none' } },
      { selector: '.dim', style: { 'opacity': 0.12 } },
      { selector: 'node.dimbox', style: { 'background-opacity': 0.08, 'border-opacity': 0.3, 'text-opacity': 0.3 } },
      { selector: 'node.lin', style: { 'underlay-color': C.hl, 'underlay-opacity': 0.55, 'underlay-padding': 6, 'underlay-shape': 'ellipse' } },
      { selector: 'node.root', style: { 'underlay-opacity': 0.9, 'underlay-padding': 9 } },
      { selector: 'edge.lin', style: { 'width': 3, 'opacity': 1 } },
      { selector: 'node:selected', style: { 'overlay-color': C.focus, 'overlay-opacity': 0.25, 'overlay-padding': 7 } }
    ];
  }

  function graphLabel(n) {
    if (n.ibis === 'response' && M.children[n.id]) {
      return '#' + n.id + ' AI response · ' + M.children[n.id].length + ' options';
    }
    return '#' + n.id + ' ' + short(nodeTitle(n), 30);
  }

  // x: time. Every distinct seq gets its own column, so x is monotonic in seq; nodes of one entry
  // (a response's options) share the column and stack inside their lane.
  function columnsOf(nodes) {
    var col = {}, i = 0;
    M.seqs.forEach(function (s) { col[s] = i++; });
    return col;
  }

  function relativeYPreset(nodes) {
    // Deterministic: stagger consecutive columns of a lane over 3 rows so labels do not collide.
    var ROW = 56, rel = {}, laneCount = [0, 0, 0, 0], laneLastSeq = [-1, -1, -1, -1], stack = {};
    nodes.forEach(function (n) {
      if (M.children[n.id]) return; // compound parent: positioned by its children
      if (laneLastSeq[n.lane] !== n.seq) { laneCount[n.lane]++; laneLastSeq[n.lane] = n.seq; }
      var key = n.lane + ':' + n.seq;
      var k = stack[key] = (stack[key] == null ? 0 : stack[key] + 1);
      rel[n.id] = ((laneCount[n.lane] - 1) % 3 + k) * ROW;
    });
    return rel;
  }

  function relativeYElk(nodes) {
    // ELK layered, direction DOWN, one partition per lane: ELK orders the lanes and layers the
    // nodes inside each lane (y). x is replaced afterwards by the time column.
    return new Promise(function (resolve, reject) {
      var leaf = nodes.filter(function (n) { return !M.children[n.id]; });
      var ids = {};
      leaf.forEach(function (n) { ids[n.id] = 1; });
      function endpoint(id) { return ids[id] ? id : (M.children[id] ? M.children[id][0] : null); }
      var edgeSeen = {};
      var headless = cytoscape({ headless: true, styleEnabled: true, style: [{ selector: 'node', style: { width: 28, height: 28 } }] });
      var els = leaf.map(function (n) { return { group: 'nodes', data: { id: n.id, lane: n.lane }, position: { x: 0, y: 0 } }; });
      M.edges.forEach(function (x) {
        var s = endpoint(x.dst), t = endpoint(x.src); // earlier -> later
        if (!s || !t || s === t) return;
        var k = s + '>' + t;
        if (edgeSeen[k]) return;
        edgeSeen[k] = 1;
        els.push({ group: 'edges', data: { id: 'l' + x.id, source: s, target: t } });
      });
      headless.add(els);
      var layout = headless.layout({
        name: 'elk', fit: false, animate: false,
        nodeLayoutOptions: function (node) { return { 'elk.partitioning.partition': node.data('lane') }; },
        elk: {
          'algorithm': 'layered', 'elk.direction': 'DOWN', 'elk.partitioning.activate': true,
          'elk.layered.spacing.nodeNodeBetweenLayers': 26, 'elk.spacing.nodeNode': 18,
          // only the layer (y) is kept, so skip the expensive in-layer work
          'elk.layered.crossingMinimization.strategy': 'NONE', 'elk.layered.nodePlacement.strategy': 'SIMPLE',
          'elk.layered.thoroughness': 1, 'elk.edgeRouting': 'POLYLINE'
        },
        stop: function () {
          var rel = {};
          headless.nodes().forEach(function (n) { rel[n.id()] = n.position('y'); });
          headless.destroy();
          resolve(rel);
        }
      });
      try { layout.run(); } catch (err) { reject(err); }
    });
  }

  function computePositions(useElk) {
    var nodes = M.nodeList;
    var col = columnsOf(nodes);
    var DX = M.big ? 60 : 104, ROW = 42, PAD = M.big ? 30 : 48, GAP = 14, STACK = 52;
    var relP = useElk ? relativeYElk(nodes).catch(function (err) {
      if (window.console) console.warn('ELK layout failed, using deterministic lanes', err);
      useElk = false;
      return relativeYPreset(nodes);
    }) : Promise.resolve(relativeYPreset(nodes));
    return relP.then(function (rel) {
      // nodes sharing a column inside a lane (options of one response) stack with a minimum gap
      if (useElk) {
        var groups = {};
        nodes.forEach(function (n) {
          if (rel[n.id] == null) return;
          var k = n.lane + ':' + n.seq;
          (groups[k] = groups[k] || []).push(n);
        });
        Object.keys(groups).forEach(function (k) {
          var g = groups[k];
          if (g.length < 2) return;
          g.sort(function (a, b) { return rel[a.id] - rel[b.id] || (a.option || 0) - (b.option || 0); });
          for (var i = 1; i < g.length; i++) rel[g[i].id] = Math.max(rel[g[i].id], rel[g[i - 1].id] + STACK);
        });
      }
      var lo = [Infinity, Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity, -Infinity];
      nodes.forEach(function (n) {
        var r = rel[n.id];
        if (r == null) return;
        lo[n.lane] = Math.min(lo[n.lane], r);
        hi[n.lane] = Math.max(hi[n.lane], r);
      });
      var lanes = [], cursor = 0;
      for (var l = 0; l < 4; l++) {
        var h = isFinite(lo[l]) ? hi[l] - lo[l] + 2 * PAD : ROW + PAD;
        lanes.push({ top: cursor, h: h, base: cursor + PAD - (isFinite(lo[l]) ? lo[l] : 0) });
        cursor += h + GAP;
      }
      var pos = {};
      nodes.forEach(function (n) {
        if (rel[n.id] == null) return;
        pos[n.id] = { x: col[n.seq] * DX, y: lanes[n.lane].base + rel[n.id] };
      });
      var width = Math.max(1, M.seqs.length) * DX;
      return { pos: pos, lanes: lanes, width: width, DX: DX, mode: useElk ? 'elk' : 'preset' };
    });
  }

  var cyPending = null, cyPendingVersion = -1;
  function ensureCy() {
    if (cy && cyVersion === modelVersion) return Promise.resolve(cy);
    if (cyPending && cyPendingVersion === modelVersion) return cyPending;
    cyPendingVersion = modelVersion;
    cyPending = buildCy().then(function (c) { cyPending = null; return c; }, function (err) { cyPending = null; throw err; });
    return cyPending;
  }

  function buildCy() {
    var t0 = performance.now();
    var useElk = M.nodeCount <= ELK_MAX_NODES && typeof ELK !== 'undefined';
    var version = modelVersion;
    return computePositions(useElk).then(function (L) {
      if (version !== modelVersion) return buildCy(); // newer data arrived meanwhile
      var tLayout = performance.now();
      var keepViewport = cy ? { zoom: cy.zoom(), pan: cy.pan() } : null;
      var wasCollapsed = S.collapsed;
      if (cy) { cy.destroy(); cy = null; ec = null; }
      var els = [];
      L.lanes.forEach(function (ln, i) {
        els.push({ group: 'nodes', data: { id: 'lane-' + i, w: L.width + L.DX * 2, h: ln.h, lane: i },
          position: { x: L.width / 2 - L.DX / 2, y: ln.top + ln.h / 2 }, classes: 'lane' + (i % 2 ? ' alt' : ''),
          selectable: false, grabbable: false });
      });
      M.nodeList.forEach(function (n) {
        var cls = [n.author === 'human' ? 'human' : 'ai', n.ibis];
        if (isDead(n)) cls.push('dead');
        if (M.children[n.id]) cls.push('group');
        var d = { id: n.id, seq: n.seq, label: graphLabel(n), lane: n.lane };
        if (M.children[n.id]) d.clabel = '#' + n.id + ' AI response (+' + M.children[n.id].length + ')';
        if (n.parent) d.parent = n.parent;
        var item = { group: 'nodes', data: d, classes: cls.join(' ') };
        if (L.pos[n.id]) item.position = L.pos[n.id];
        els.push(item);
      });
      M.edges.forEach(function (x) {
        var cls = [x.type];
        if (NEG_TYPES[x.type]) cls.push('neg');
        if (x.confirmed) cls.push('confirmed');
        if (x.suggested) cls.push('suggested');
        els.push({ group: 'edges', data: { id: x.id, source: x.src, target: x.dst, type: x.type,
          seq: Math.max(nodeSeq(x.src), nodeSeq(x.dst)) }, classes: cls.join(' ') });
      });
      var big = M.big;
      cy = cytoscape({
        container: $('cy'), elements: els, style: cyStyle(big), layout: { name: 'preset', fit: false },
        minZoom: 0.02, maxZoom: 3, autoungrabify: true, boxSelectionEnabled: false, selectionType: 'single',
        textureOnViewport: big, hideEdgesOnViewport: big, pixelRatio: big ? 1 : 'auto', motionBlur: false
      });
      cyVersion = modelVersion;
      bindCy();
      if (typeof cy.expandCollapse === 'function') {
        ec = cy.expandCollapse({ layoutBy: null, fisheye: false, animate: false, undoable: false, cueEnabled: !big,
          expandCollapseCuePosition: 'top-left' });
        if (wasCollapsed) ec.collapseAll();
      }
      if (keepViewport) { cy.viewport(keepViewport); }
      else initialViewport(L);
      if (S.sel) { var s = cy.getElementById(S.sel); if (s.nonempty()) s.select(); }
      return new Promise(function (resolve) {
        cy.one('render', function () {
          requestAnimationFrame(function () {
            var t1 = performance.now();
            perf.layout = { mode: L.mode, nodes: M.nodeCount, edges: M.edges.length, positionsMs: Math.round(tLayout - t0),
              totalMs: Math.round(t1 - t0) };
            perf.ready = true;
            resolve(cy);
          });
        });
        cy.forceRender();
      });
    });
  }

  function fitNodes() {
    // like cy.fit, but keeps the lane labels (left) and the legend (bottom) clear of the nodes
    var eles = cy.nodes().not('.lane');
    if (!eles.length) return;
    var bb = eles.boundingBox(), W = cy.width(), H = cy.height(), left = 120, right = 30, top = 30, bottom = 76;
    var z = Math.min((W - left - right) / Math.max(bb.w, 1), (H - top - bottom) / Math.max(bb.h, 1), 1.4);
    z = Math.max(cy.minZoom(), Math.min(cy.maxZoom(), z));
    cy.viewport({ zoom: z, pan: { x: left - bb.x1 * z + Math.max(0, (W - left - right - bb.w * z) / 2),
      y: top - bb.y1 * z + Math.max(0, (H - top - bottom - bb.h * z) / 2) } });
  }

  function initialViewport(L) {
    fitNodes();
    if (cy.zoom() < 0.5) {
      // long sessions: show the latest turns at a readable zoom (labels on), lanes from the top
      var z = 0.85;
      var bb = cy.nodes().not('.lane').boundingBox();
      var lanesBox = cy.nodes('.lane').boundingBox();
      cy.viewport({ zoom: z, pan: { x: cy.width() - bb.x2 * z - 60, y: 16 - lanesBox.y1 * z } });
    }
  }

  function bindCy() {
    cy.on('tap', 'node', function (ev) {
      var n = ev.target;
      if (n.hasClass('lane')) return;
      selectNode(n.id(), false);
    });
    cy.on('dbltap', 'node.group, node.cy-expand-collapse-collapsed-node', function (ev) {
      if (!ec) return;
      var n = ev.target;
      if (ec.isCollapsible(n)) ec.collapse(n); else if (ec.isExpandable(n)) ec.expand(n);
      applyGraphClasses();
    });
    cy.on('expandcollapse.afterexpand', function () { applyGraphClasses(); });
    var raf = null;
    cy.on('viewport resize', function () {
      if (raf) return;
      raf = requestAnimationFrame(function () { raf = null; updateLaneLabels(); });
    });
  }

  function updateLaneLabels() {
    var box = $('lane-labels');
    if (!cy || $('cy').hidden) return;
    if (!box.children.length) LANES.forEach(function (n) { box.appendChild(el('span', null, n)); });
    var h = cy.height();
    cy.nodes('.lane').forEach(function (ln) {
      var i = ln.data('lane');
      var bb = ln.renderedBoundingBox();
      var y = Math.max(bb.y1 + 12, Math.min(bb.y2 - 12, (bb.y1 + bb.y2) / 2));
      var span = box.children[i];
      span.style.top = y + 'px';
      span.hidden = bb.y2 < 0 || bb.y1 > h;
    });
  }

  function lineage(root, confirmedOnly, limitSeq) {
    var depth = {}, queue = [root];
    depth[root] = 0;
    while (queue.length) {
      var id = queue.shift();
      if (depth[id] >= GENEALOGY_DEPTH) continue;
      (M.out[id] || []).forEach(function (x) {
        if (!LINEAGE_TYPES[x.type]) return;
        if (confirmedOnly && !x.confirmed) return;
        if (limitSeq != null && nodeSeq(x.dst) > limitSeq) return;
        if (depth[x.dst] == null) { depth[x.dst] = depth[id] + 1; queue.push(x.dst); }
      });
    }
    var edges = [];
    M.edges.forEach(function (x) {
      if (depth[x.src] != null && depth[x.dst] != null && (!confirmedOnly || x.confirmed)) edges.push(x);
    });
    var ids = Object.keys(depth);
    var ai = [], human = 0;
    ids.forEach(function (id) {
      var n = M.byId[id];
      if (!n) return;
      if (n.author === 'human' && id !== root) human++;
      if (n.author === 'ai') {
        var mods = edges.filter(function (x) {
          return x.dst === id && (x.type === 'modifies' || x.type === 'refines') && M.byId[x.src] && M.byId[x.src].author === 'human';
        }).map(function (x) { return x.src; });
        mods = mods.filter(function (v, i) { return mods.indexOf(v) === i; }).sort(function (a, b) { return nodeSeq(a) - nodeSeq(b); });
        ai.push({ id: id, mods: mods });
      }
    });
    ai.sort(function (a, b) { return nodeSeq(a.id) - nodeSeq(b.id) || (a.id < b.id ? -1 : 1); });
    return { depth: depth, ids: ids, edges: edges, human: human, ai: ai };
  }

  function currentClaim() {
    var claims = (G.claims || []).map(String);
    if (!claims.length) return null;
    if (S.claim && claims.indexOf(String(S.claim)) >= 0) return String(S.claim);
    return claims[claims.length - 1];
  }

  function renderGenealogyPanel() {
    var sel = $('gen-claim');
    var claims = (G.claims || []).map(String);
    var cur = currentClaim();
    var sig = claims.join(',');
    if (sel.getAttribute('data-sig') !== sig) {
      sel.textContent = '';
      claims.forEach(function (c) {
        var n = M.byId[c];
        sel.appendChild(el('option', { value: c }, '#' + c + ' ' + short(n ? nodeTitle(n) : '', 60)));
      });
      sel.setAttribute('data-sig', sig);
    }
    sel.disabled = !claims.length;
    if (cur) sel.value = cur;
    $('gen-confirmed').checked = S.conf;
    var box = $('gen-summary');
    box.textContent = '';
    if (!cur) {
      box.appendChild(el('p', { class: 'empty' }, 'No #claim entries yet. Tag a prompt with #claim to trace its genealogy.'));
      return;
    }
    if (S.seq != null && nodeSeq(cur) > S.seq) {
      box.appendChild(el('p', { class: 'empty' }, 'Claim #' + cur + ' is after the replay position (#' + S.seq + ').'));
      return;
    }
    var L = lineage(cur, S.conf, S.seq);
    var counts = el('div', { class: 'counts' });
    counts.appendChild(el('span', { class: 'chip', 'data-count': 'human' }, 'Human-originated ancestors: ' + L.human));
    counts.appendChild(el('span', { class: 'chip', 'data-count': 'ai' }, 'AI-originated ancestors: ' + L.ai.length));
    box.appendChild(counts);
    var sorted = L.ids.slice().sort(function (a, b) { return nodeSeq(a) - nodeSeq(b) || (a < b ? -1 : 1); });
    var linP = el('p', { class: 'meta-line', id: 'gen-lineage', 'data-lineage': sorted.join(',') },
      'Lineage of #' + cur + (S.conf ? ' (confirmed edges only)' : ' (all edges)') + ': ' + sorted.map(function (x) { return '#' + x; }).join(', '));
    box.appendChild(linP);
    if (L.ai.length) {
      box.appendChild(el('h3', null, 'AI-originated ancestors'));
      var ul = el('ul', { id: 'gen-ai' });
      L.ai.forEach(function (a) {
        var n = M.byId[a.id];
        var li = el('li', { 'data-node': a.id });
        var b = el('button', { type: 'button', class: 'linkish' }, '#' + a.id);
        b.addEventListener('click', function () { selectNode(a.id, true); });
        li.appendChild(b);
        var txt = a.mods.length
          ? ' AI-originated, modified by the human at ' + a.mods.map(function (x) { return '#' + x; }).join(', ')
          : ' AI-originated, not modified by the human';
        li.appendChild(document.createTextNode(txt + ' — ' + short(n ? nodeTitle(n) : '', 80)));
        ul.appendChild(li);
      });
      box.appendChild(ul);
    } else {
      box.appendChild(el('p', { class: 'meta-line' }, 'No AI-originated ancestors.'));
    }
    box.appendChild(el('p', { class: 'meta-line' }, 'Depth ≤ ' + GENEALOGY_DEPTH + ', as in the report. Human count excludes the claim itself.'));
  }

  function applyGraphClasses() {
    if (!cy) return;
    var mode = graphMode();
    var limit = S.seq == null ? Infinity : S.seq;
    var L = null, root = null;
    if (mode === 'genealogy') {
      root = currentClaim();
      if (root && nodeSeq(root) <= limit) L = lineage(root, S.conf, S.seq);
    }
    var linNodes = L ? L.depth : null, linEdges = {};
    if (L) L.edges.forEach(function (x) { linEdges[x.id] = 1; });
    cy.batch(function () {
      cy.nodes().forEach(function (n) {
        if (n.hasClass('lane')) return;
        var id = n.id();
        n.toggleClass('future', n.data('seq') > limit);
        if (!L) { if (n.hasClass('dim') || n.hasClass('lin') || n.hasClass('dimbox') || n.hasClass('root')) n.removeClass('dim lin dimbox root'); return; }
        var on = linNodes[id] != null;
        var kids = M.children[id];
        var kidOn = kids && kids.some(function (k) { return linNodes[k] != null; });
        if (n.hasClass('cy-expand-collapse-collapsed-node')) on = on || kidOn;
        if (kids && !n.hasClass('cy-expand-collapse-collapsed-node')) {
          // compound parent: dimming it with opacity would also dim its children
          n.removeClass('dim');
          n.toggleClass('dimbox', !on);
          n.toggleClass('lin', on);
        } else {
          n.toggleClass('dim', !on);
          n.toggleClass('lin', on);
        }
        n.toggleClass('root', id === root);
      });
      cy.edges().forEach(function (e) {
        // edges of a collapsed group keep their ids (the extension only moves their endpoints)
        if (L) {
          var on = !!linEdges[e.id()];
          e.toggleClass('lin', on);
          e.toggleClass('dim', !on);
        } else if (e.hasClass('lin') || e.hasClass('dim')) {
          e.removeClass('lin dim');
        }
      });
    });
  }

  function initGraphTools() {
    $('btn-fit').addEventListener('click', function () { if (cy) fitNodes(); });
    $('btn-collapse').addEventListener('click', function () {
      if (!ec) return;
      S.collapsed = true;
      ec.collapseAll();
      applyGraphClasses();
    });
    $('btn-expand').addEventListener('click', function () {
      if (!ec) return;
      S.collapsed = false;
      ec.expandAll();
      applyGraphClasses();
    });
    $('gen-claim').addEventListener('change', function () {
      S.claim = $('gen-claim').value;
      writeHash();
      renderGenealogyPanel();
      applyGraphClasses();
    });
    $('gen-confirmed').addEventListener('change', function () {
      S.conf = $('gen-confirmed').checked;
      writeHash();
      renderGenealogyPanel();
      applyGraphClasses();
    });
    $('cy').addEventListener('keydown', function (ev) {
      if (!cy) return;
      var d = 60, k = ev.key;
      if (k === 'ArrowLeft') cy.panBy({ x: d, y: 0 });
      else if (k === 'ArrowRight') cy.panBy({ x: -d, y: 0 });
      else if (k === 'ArrowUp') cy.panBy({ x: 0, y: d });
      else if (k === 'ArrowDown') cy.panBy({ x: 0, y: -d });
      else if (k === '+' || k === '=') cy.zoom({ level: cy.zoom() * 1.2, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
      else if (k === '-') cy.zoom({ level: cy.zoom() / 1.2, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
      else return;
      ev.preventDefault();
    });
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var onChange = function () { if (cy) cy.style(cyStyle(M.big)); branchesVersion = -1; if (graphMode() === 'branches') renderBranches(); };
      if (mq.addEventListener) mq.addEventListener('change', onChange); else if (mq.addListener) mq.addListener(onChange);
    }
  }

  // ------------------------------------------------------------------------------------------
  // Branches (git-graph metaphor), plain SVG

  function isApproach(n) { return n.ibis === 'position'; }
  function isWork(n) { return n.ibis === 'action' || n.ibis === 'argument'; }

  function branchModel(limit) {
    var nodes = M.nodeList.filter(function (n) { return n.seq <= limit; });
    nodes = nodes.slice().sort(function (a, b) { return a.seq - b.seq || (a.id === String(a.seq) ? -1 : b.id === String(b.seq) ? 1 : (a.option || 0) - (b.option || 0) || (a.id < b.id ? -1 : 1)); });
    var visible = {};
    nodes.forEach(function (n) { visible[n.id] = 1; });
    function edgesOut(id) { return (M.out[id] || []).filter(function (x) { return visible[x.dst] && visible[x.src] && !x.suggested; }); }
    function edgesIn(id) { return (M.inc[id] || []).filter(function (x) { return visible[x.dst] && visible[x.src] && !x.suggested; }); }
    var branchOf = {}, branches = [], byRoot = {};
    nodes.forEach(function (n) {
      if (!isApproach(n)) return;
      var b = { root: n.id, members: [n.id], start: n.id, forkFrom: null, end: null, endKind: 'open', note: '' };
      // an approach that modifies / refines / derives from another approach forks from that branch
      edgesOut(n.id).forEach(function (x) {
        if (b.forkFrom == null && (x.type === 'modifies' || x.type === 'refines' || x.type === 'derived_from') && byRoot[x.dst]) {
          b.forkFrom = x.dst;
          b.forkType = x.type;
        }
      });
      byRoot[n.id] = b;
      branchOf[n.id] = b;
      branches.push(b);
    });
    // work (tool edits, test runs) goes on the branch it implements / tests
    nodes.forEach(function (n) {
      if (!isWork(n)) return;
      var tgt = null;
      edgesOut(n.id).forEach(function (x) {
        if (!tgt && (x.type === 'implements' || x.type === 'supports' || x.type === 'objects_to') && byRoot[x.dst]) tgt = byRoot[x.dst];
      });
      if (tgt) { tgt.members.push(n.id); branchOf[n.id] = tgt; }
    });
    var rowOf = {};
    nodes.forEach(function (n, i) { rowOf[n.id] = i; });
    branches.forEach(function (b) {
      var n = M.byId[b.root];
      var lastMember = b.members.reduce(function (acc, id) { return rowOf[id] > rowOf[acc] ? id : acc; }, b.root);
      if (isDead(n)) {
        var why = edgesIn(n.id).filter(function (x) { return NEG_TYPES[x.type] && !isWork(M.byId[x.src]); });
        var fails = b.members.filter(function (id) { var m = M.byId[id]; return m.ibis === 'argument' && /^tests fail/.test(m.label || ''); });
        b.endKind = 'dead';
        if (why.length) {
          b.end = why[0].src;
          b.note = (n.status === 'rejected' ? '✗ rejected at #' : '✗ discarded at #') + why[0].src + ': ' + short(nodeTitle(M.byId[why[0].src]), 70);
        } else if (fails.length) {
          b.end = lastMember;
          b.note = '✗ discarded · failure #' + fails[fails.length - 1] + ': ' + short(M.byId[fails[fails.length - 1]].label, 60);
        } else {
          b.end = lastMember;
          b.note = '✗ discarded: ' + short(nodeTitle(n), 80);
        }
        if (rowOf[b.end] < rowOf[lastMember]) b.end = lastMember;
        return;
      }
      var merges = edgesIn(n.id).filter(function (x) {
        var s = M.byId[x.src];
        return ADOPT_TYPES[x.type] && s && !isWork(s) && !isApproach(s);
      }).sort(function (a, c) { return rowOf[a.src] - rowOf[c.src]; });
      var mods = edgesIn(n.id).filter(function (x) { return x.type === 'modifies' && byRoot[x.src] && byRoot[x.src].forkFrom === n.id; });
      if (merges.length && rowOf[merges[0].src] > rowOf[lastMember]) {
        b.end = merges[0].src;
        b.endKind = 'merge';
        b.note = '↳ adopted at #' + merges[0].src + ' (' + merges[0].type.replace(/_/g, ' ') + ')';
      } else if (merges.length) {
        b.end = lastMember;
        b.endKind = 'merge-late';
        b.note = '↳ adopted at #' + merges[0].src;
      } else if (mods.length) {
        b.end = mods[0].src;
        b.endKind = 'modified';
        b.note = '↳ modified by the human at #' + mods[0].src;
      } else {
        b.end = lastMember;
        b.endKind = 'open';
        b.note = 'open';
      }
    });
    // columns: main line is 0; branches take the lowest free column. An approach that the human
    // modified continues in the column of the approach it modifies (the line changes owner).
    var colEnd = [Infinity];
    branches.sort(function (a, c) { return rowOf[a.start] - rowOf[c.start]; });
    branches.forEach(function (b) {
      var s = rowOf[b.start], e = rowOf[b.end];
      var parent = b.forkFrom ? byRoot[b.forkFrom] : null;
      if (parent && parent.endKind === 'modified' && parent.end === b.root && parent.col != null) {
        b.col = parent.col;
        b.continues = true;
        colEnd[b.col] = Math.max(colEnd[b.col], e);
        return;
      }
      var c = 1;
      while (colEnd[c] != null && colEnd[c] >= s) c++;
      colEnd[c] = e;
      b.col = c;
    });
    return { nodes: nodes, rowOf: rowOf, branches: branches, branchOf: branchOf, byRoot: byRoot, cols: colEnd.length };
  }

  function renderBranches() {
    var limit = S.seq == null ? Infinity : S.seq;
    if (branchesVersion === modelVersion && branchesSeq === limit) { markBranchSelection(); return; }
    branchesVersion = modelVersion;
    branchesSeq = limit;
    var B = branchModel(limit);
    var ROW = 26, COL = 18, X0 = 14, Y0 = 14;
    var gw = X0 + B.cols * COL + 10;
    var textX = gw + 6;
    var H = Y0 + B.nodes.length * ROW + 20;
    var W = textX + 1500;
    var C = { human: css('--human'), ai: css('--ai'), edge: css('--edge'), ok: css('--ok'), danger: css('--danger'),
      muted: css('--muted'), panel: css('--panel'), lineage: css('--lineage'), text: css('--text') };
    function cx(col) { return X0 + col * COL; }
    function cyy(row) { return Y0 + row * ROW; }
    var parts = [];
    parts.push('<svg xmlns="http://www.w3.org/2000/svg" width="' + W + '" height="' + H + '" role="img" aria-label="Branches: approaches as branches, adoption as merges, discards as dead ends">');
    // main line
    if (B.nodes.length) {
      parts.push('<line x1="' + cx(0) + '" y1="' + cyy(0) + '" x2="' + cx(0) + '" y2="' + cyy(B.nodes.length - 1) + '" stroke="' + C.text + '" stroke-width="3" stroke-linecap="round"/>');
    }
    B.branches.forEach(function (b) {
      var x = cx(b.col), s = B.rowOf[b.start], e = B.rowOf[b.end];
      var color = M.byId[b.root].author === 'human' ? C.human : C.ai;
      var dead = b.endKind === 'dead';
      var dash = dead ? ' stroke-dasharray="5 4"' : '';
      // fork: from the parent branch (or the main line) at the row above the start
      var fromCol = b.forkFrom && B.byRoot[b.forkFrom] ? B.byRoot[b.forkFrom].col : 0;
      var forkRow = Math.max(0, s - 1);
      if (b.forkFrom && B.rowOf[b.forkFrom] != null) forkRow = Math.min(s - 1, Math.max(B.rowOf[b.forkFrom], s - 1));
      var fx = cx(fromCol), fy = cyy(Math.max(0, forkRow));
      if (!b.continues) {
        parts.push('<path d="M' + fx + ' ' + fy + ' C' + fx + ' ' + (fy + ROW * 0.7) + ' ' + x + ' ' + (cyy(s) - ROW * 0.7) + ' ' + x + ' ' + cyy(s) +
          '" fill="none" stroke="' + color + '" stroke-width="2"' + dash + '/>');
      }
      var tb = b.endKind === 'modified' ? B.byRoot[b.end] : null;
      var sameCol = tb && tb.col === b.col;
      var endY = b.endKind === 'merge' || (b.endKind === 'modified' && !sameCol) ? cyy(e) - ROW * 0.8 : cyy(e);
      if (endY > cyy(s)) parts.push('<line x1="' + x + '" y1="' + cyy(s) + '" x2="' + x + '" y2="' + endY + '" stroke="' + color + '" stroke-width="2"' + dash + '/>');
      if (b.endKind === 'merge') {
        parts.push('<path d="M' + x + ' ' + endY + ' C' + x + ' ' + (cyy(e) - ROW * 0.2) + ' ' + cx(0) + ' ' + (cyy(e) - ROW * 0.5) + ' ' + cx(0) + ' ' + cyy(e) +
          '" fill="none" stroke="' + C.ok + '" stroke-width="2"/>');
      } else if (b.endKind === 'modified' && !sameCol) {
        var tx = tb ? cx(tb.col) : cx(0);
        parts.push('<path d="M' + x + ' ' + endY + ' C' + x + ' ' + (cyy(e) - ROW * 0.2) + ' ' + tx + ' ' + (cyy(e) - ROW * 0.5) + ' ' + tx + ' ' + cyy(e) +
          '" fill="none" stroke="' + C.lineage + '" stroke-width="2"/>');
      } else if (dead) {
        var dy = cyy(e) + 9;
        parts.push('<line x1="' + x + '" y1="' + cyy(e) + '" x2="' + x + '" y2="' + dy + '" stroke="' + C.danger + '" stroke-width="2" stroke-dasharray="3 3"/>');
        parts.push('<path d="M' + (x - 5) + ' ' + (dy - 1) + ' L' + (x + 5) + ' ' + (dy + 9) + ' M' + (x + 5) + ' ' + (dy - 1) + ' L' + (x - 5) + ' ' + (dy + 9) +
          '" stroke="' + C.danger + '" stroke-width="2.4" stroke-linecap="round"/>');
      }
    });
    // commits (rows)
    var noteAt = {};
    B.branches.forEach(function (b) {
      var key = b.endKind === 'dead' || b.endKind === 'open' || b.endKind === 'merge-late' ? b.end : b.start;
      (noteAt[key] = noteAt[key] || []).push(b);
    });
    B.nodes.forEach(function (n, i) {
      var b = B.branchOf[n.id];
      var col = b ? b.col : 0;
      var x = cx(col), y = cyy(i);
      var human = n.author === 'human';
      var color = human ? C.human : C.ai;
      var dead = isDead(n);
      var fill = dead ? C.panel : color;
      var failed = n.ibis === 'argument' && /^tests fail/.test(n.label || '');
      parts.push('<g class="row' + (S.sel === n.id ? ' sel' : '') + '" data-id="' + esc(n.id) + '" tabindex="-1">');
      parts.push('<rect class="hl" x="0" y="' + (y - ROW / 2) + '" width="' + W + '" height="' + ROW + '"/>');
      if (human) {
        parts.push('<circle cx="' + x + '" cy="' + y + '" r="6.5" fill="' + fill + '" stroke="' + color + '" stroke-width="2"' + (dead ? ' stroke-dasharray="3 2"' : '') + '/>');
      } else {
        parts.push('<rect x="' + (x - 6) + '" y="' + (y - 6) + '" width="12" height="12" rx="3" fill="' + (failed ? C.danger : fill) + '" stroke="' + (failed ? C.danger : color) + '" stroke-width="2"' + (dead ? ' stroke-dasharray="3 2"' : '') + '/>');
      }
      var label = '#' + n.id + '  ' + short(nodeTitle(n) || n.label, 80);
      var t = '<text x="' + textX + '" y="' + (y + 4) + '"><tspan' + (dead ? ' text-decoration="line-through"' : '') + '>' + esc(label) + '</tspan>';
      (noteAt[n.id] || []).forEach(function (bb) {
        if (!bb.note || (bb.endKind === 'open' && bb.root !== n.id)) return;
        var cls = bb.endKind === 'dead' ? 'reason' : bb.endKind === 'open' ? 'open' : 'merge';
        t += '<tspan class="' + cls + '" dx="14">' + esc((bb.root !== n.id ? '#' + bb.root + ' ' : '') + bb.note) + '</tspan>';
      });
      parts.push(t + '</text></g>');
    });
    parts.push('</svg>');
    var box = $('branches');
    var st = box.scrollTop, sl = box.scrollLeft;
    box.innerHTML = parts.join('');
    box.scrollTop = st;
    box.scrollLeft = sl;
    perf.branches = { rows: B.nodes.length, branches: B.branches.length };
    window.__authorshipBranches = B;
  }

  function markBranchSelection() {
    var box = $('branches');
    box.querySelectorAll('.row.sel').forEach(function (r) { r.classList.remove('sel'); });
    if (!S.sel) return;
    var r = box.querySelector('.row[data-id="' + (window.CSS && CSS.escape ? CSS.escape(S.sel) : S.sel) + '"]');
    if (r) r.classList.add('sel');
  }

  function initBranches() {
    $('branches').addEventListener('click', function (ev) {
      var g = ev.target.closest ? ev.target.closest('.row') : null;
      if (g) selectNode(g.getAttribute('data-id'), false);
    });
  }

  // ------------------------------------------------------------------------------------------
  // Review

  function describeLabel(label) {
    var m = /^edge:([^:]+):([a-z_]+):([^:]+)$/.exec(label || '');
    if (m) return 'Relation: #' + m[1] + ' ' + m[2].replace(/_/g, ' ') + ' #' + m[3];
    m = /^milestone:(.+)$/.exec(label || '');
    if (m) return 'Milestone: ' + m[1].replace(/_/g, ' ');
    return label;
  }

  function renderReview() {
    var secret = getSecret();
    $('readonly-hint').hidden = !!secret;
    if (reviewVersion === modelVersion) return;
    reviewVersion = modelVersion;
    var list = $('review');
    var prevResults = {};
    list.querySelectorAll('li[data-key]').forEach(function (li) {
      var r = li.querySelector('.result');
      if (r && r.textContent) prevResults[li.getAttribute('data-key')] = { text: r.textContent, cls: r.className };
    });
    list.textContent = '';
    var items = G.review || [];
    if (!items.length) list.appendChild(el('li', { class: 'empty' }, 'Nothing to review.'));
    items.forEach(function (it) {
      var key = it.target_seq + '|' + it.label;
      var li = el('li', { 'data-key': key, 'data-kind': it.kind });
      li.appendChild(el('div', { class: 'r-title' }, describeLabel(it.label) + ' on #' + it.target_seq));
      li.appendChild(el('div', { class: 'r-meta' }, it.label + (it.score != null ? ' · score ' + Number(it.score).toFixed(2) : '') +
        (it.annotation_id ? ' · ' + it.annotation_id.slice(0, 16) : '')));
      var n = M.byId[String(it.target_seq)];
      var txt = it.text || (n ? n.label : (M.entries[it.target_seq] || {}).preview);
      if (txt) li.appendChild(el('p', { class: 'r-text' }, short(txt, 300)));
      var actions = el('div', { class: 'actions' });
      var bA = el('button', { type: 'button', class: 'accept', 'data-decision': 'accept' }, 'Accept');
      var bR = el('button', { type: 'button', class: 'reject', 'data-decision': 'reject' }, 'Reject');
      var bE = el('button', { type: 'button', class: 'edit', 'data-decision': 'edit', 'aria-expanded': 'false' }, 'Edit…');
      [bA, bR, bE].forEach(function (b) {
        b.disabled = !secret;
        if (!secret) b.title = 'Open the viewer from your terminal: python3 <plugin>/scripts/viewer.py open';
        actions.appendChild(b);
      });
      li.appendChild(actions);
      var editRow = el('div', { class: 'edit-row' });
      editRow.hidden = true;
      var inputId = 'edit-' + key.replace(/[^a-zA-Z0-9]/g, '_');
      var lab = el('label', { for: inputId, class: 'meta-line' }, 'Edited label (e.g. milestone:<type> or edge:<src>:<type>:<dst>)');
      var input = el('input', { type: 'text', id: inputId, value: it.label, spellcheck: 'false' });
      var save = el('button', { type: 'button', class: 'save' }, 'Save edit');
      var cancel = el('button', { type: 'button' }, 'Cancel');
      editRow.appendChild(lab);
      editRow.appendChild(input);
      editRow.appendChild(save);
      editRow.appendChild(cancel);
      li.appendChild(editRow);
      var result = el('div', { class: 'result', role: 'status' });
      if (prevResults[key]) { result.textContent = prevResults[key].text; result.className = prevResults[key].cls; }
      li.appendChild(result);
      bA.addEventListener('click', function () { sendConfirm(it, 'accept', null, li); });
      bR.addEventListener('click', function () { sendConfirm(it, 'reject', null, li); });
      bE.addEventListener('click', function () {
        editRow.hidden = !editRow.hidden;
        bE.setAttribute('aria-expanded', editRow.hidden ? 'false' : 'true');
        if (!editRow.hidden) input.focus();
      });
      cancel.addEventListener('click', function () { editRow.hidden = true; bE.setAttribute('aria-expanded', 'false'); bE.focus(); });
      save.addEventListener('click', function () {
        var v = input.value.trim();
        if (!v) { result.textContent = 'The edited label cannot be empty.'; result.className = 'result err'; return; }
        sendConfirm(it, 'edit', v, li);
      });
      save.disabled = !secret;
      list.appendChild(li);
    });
    renderConfirms();
  }

  function renderConfirms() {
    var ol = $('confirms');
    ol.textContent = '';
    var cs = G.entries.filter(function (e) { return e.event === 'Confirm'; }).slice(-30).reverse();
    if (!cs.length) ol.appendChild(el('li', { class: 'empty' }, 'No confirmations yet.'));
    cs.forEach(function (e) {
      ol.appendChild(el('li', { 'data-seq': String(e.seq) }, '#' + e.seq + ' · ' + e.decision + ' · ' + (e.label || '') +
        ' → #' + e.target_seq + ' · ' + e.ts));
    });
  }

  function sendConfirm(item, decision, edited, li) {
    var secret = getSecret();
    var result = li.querySelector('.result');
    if (!secret) {
      result.textContent = 'Read-only: open the viewer from your terminal: python3 <plugin>/scripts/viewer.py open';
      result.className = 'result err';
      return;
    }
    li.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    result.textContent = 'Recording…';
    result.className = 'result';
    fetch('/api/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Authorship-Secret': secret },
      // The page's own Referrer-Policy is no-referrer, which would make the browser send "Origin: null".
      // same-origin keeps the real Origin on this same-origin request, as the server requires.
      referrerPolicy: 'same-origin',
      cache: 'no-store',
      body: JSON.stringify({ target_seq: item.target_seq, annotation_id: item.annotation_id || null, decision: decision,
        label: item.label, edited_label: decision === 'edit' ? edited : null })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, body: j }; });
    }).then(function (res) {
      if (res.status === 200) {
        result.textContent = 'Recorded as #' + res.body.seq + ' (' + decision + ', hash ' + res.body.hash + ')';
        result.className = 'result ok';
        li.classList.add('done');
        li.setAttribute('data-confirm-seq', String(res.body.seq));
        var rec = $('review-recorded');
        var line = el('p', { class: 'result ok', 'data-confirm-seq': String(res.body.seq) },
          'Recorded as #' + res.body.seq + ': ' + decision + ' ' + (decision === 'edit' ? edited : item.label) + ' on #' + item.target_seq);
        rec.insertBefore(line, rec.firstChild);
        fetchGraph();
      } else {
        result.textContent = res.status === 403
          ? 'Refused (403). The session secret is missing or stale; reopen the viewer from your terminal: python3 <plugin>/scripts/viewer.py open'
          : 'Error ' + res.status + ': ' + (res.body.error || 'request failed');
        result.className = 'result err';
        li.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
      }
    }).catch(function (err) {
      result.textContent = 'Network error: ' + err.message;
      result.className = 'result err';
      li.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
    });
  }

  // ------------------------------------------------------------------------------------------
  // Perf hooks for tests: a scripted pan measured with requestAnimationFrame

  perf.panTest = function (ms, dx) {
    ms = ms || 1000;
    dx = dx == null ? -6 : dx;
    return new Promise(function (resolve) {
      if (!cy) { resolve({ fps: 0, frames: 0 }); return; }
      var t0 = null, frames = 0, last = null, worst = 0, dir = 1, n = 0;
      function tick(t) {
        if (t0 == null) { t0 = t; last = t; }
        else { frames++; worst = Math.max(worst, t - last); last = t; }
        if (++n % 60 === 0) dir = -dir;
        cy.panBy({ x: dx * dir, y: 0 });
        if (t - t0 < ms) requestAnimationFrame(tick);
        else resolve({ fps: frames * 1000 / (t - t0), frames: frames, worstFrameMs: Math.round(worst), zoom: cy.zoom() });
      }
      requestAnimationFrame(tick);
    });
  };

  window.__authorship = {
    state: S,
    model: function () { return M; },
    cy: function () { return cy; },
    refresh: fetchGraph,
    lineage: function (root, conf) { var L = lineage(String(root), !!conf, null); return L.ids.slice().sort(); }
  };

  // ------------------------------------------------------------------------------------------
  // Boot

  function boot() {
    readHash();
    initTabs();
    initReplay();
    initGraphTools();
    initBranches();
    var skip = document.querySelector('.skip');
    if (skip) skip.addEventListener('click', function (ev) { ev.preventDefault(); $('main').tabIndex = -1; $('main').focus(); });
    window.addEventListener('hashchange', function () {
      readHash();
      if (M) { applyLimits(); render(); }
    });
    $('readonly-hint').hidden = !!getSecret();
    fetchGraph();
    setInterval(function () { if (!document.hidden) fetchGraph(); }, POLL_MS);
    window.addEventListener('resize', function () { if (cy) { cy.resize(); updateLaneLabels(); } });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
