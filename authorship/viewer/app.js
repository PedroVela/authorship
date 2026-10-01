/* Authorship record viewer. Plain ES2019, no build step, no network beyond this origin.
 *
 * Four views, in plain language:
 *   Overview  what you invented: claims, the elements they rest on, who contributed each
 *   Timeline  what happened, in order, grouped by stage
 *   Map       how the ideas connect (problems, ideas, decisions and claims, Claude's work), with a time slider
 *   Review    labels the classifier was unsure of, for the human to answer
 * State lives in the URL hash (#view=map&focus=13&upto=40). The only write is POST /api/confirm.
 * All text from the ledger is inserted with textContent, never as HTML.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------------------------------
  // Secret: move #k=<secret> into sessionStorage and strip it from the URL before anything else.
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
  // Constants and state
  var POLL_MS = 4000;
  var MAP_MAX = 5000;           // above this many nodes the Map is off (Overview, Timeline and Review still work)
  var BIG = 1500;               // above this, cytoscape performance options
  var TL_PAGE = 400;            // timeline rows rendered per page
  var VIEWS = ['overview', 'timeline', 'map', 'review'];
  var LANES = ['Problems', 'Ideas and options', 'Decisions and claims', "Claude's work and tests"];
  var LANE_OF = { issue: 0, position: 1, remark: 1, response: 1, decision: 2, claim: 2, action: 3, argument: 3 };
  var LINEAGE = { derived_from: 1, modifies: 1, refines: 1, responds_to: 1 };
  var KIND_OF_TAG = { '#problem': 'problem', '#idea': 'idea', '#hypothesis': 'hypothesis', '#decision': 'decision',
    '#claim': 'claim', '#discard': 'discard' };
  var SAY_KIND = { problem: 'stated the problem', idea: 'proposed an idea', hypothesis: 'stated a hypothesis',
    decision: 'decided', claim: 'stated a claim', discard: 'discarded an approach' };
  var MS_NAME = { conception_candidate: 'conception', conception: 'conception', reduction_to_practice: 'proven by tests',
    decision_with_reason: 'decision', claim_candidate: 'claim', discard_with_reason: 'discarded approach',
    problem_fixed: 'problem', ai_origin_element: 'from Claude', maturity_jump: 'more definite' };
  var MS_LONG = {
    conception_candidate: 'a conception moment: you introduced a new technical element',
    ai_origin_element: 'an AI-origin element: the idea came from Claude',
    maturity_jump: 'a maturity jump: the idea became more definite',
    discard_with_reason: 'a discarded approach, with the reason it fails',
    problem_fixed: 'the statement of the technical problem',
    decision_with_reason: 'a decision between alternatives, with its reason',
    claim_candidate: 'a claim candidate: the invention stated as a whole',
    reduction_to_practice: 'evidence that the idea works (tests pass after failing)'
  };
  var REL_OUT = { derived_from: 'builds on', refines: 'extends', modifies: 'changes', responds_to: 'replies to',
    implements: 'implements', supports: 'shows working', objects_to: 'fails against', rejects: 'rejects',
    supersedes: 'replaces', discards: 'discards' };
  var REL_IN = { derived_from: 'is built on by', refines: 'is extended by', modifies: 'is changed by',
    responds_to: 'is answered by', implements: 'is implemented by', supports: 'is shown working by',
    objects_to: 'has failing tests at', rejects: 'is rejected by', supersedes: 'is replaced by', discards: 'is discarded by' };
  var EDGE_CLASS = { modifies: 'changed', refines: 'changed', rejects: 'against', objects_to: 'against', discards: 'against',
    implements: 'work', supports: 'work' };

  var S = { view: 'overview', focus: '', upto: 0, tl: 'key', q: '', open: null, sel: null };
  var G = null, M = null, etag = null, cy = null, cyKey = null, playing = null, tlShown = TL_PAGE;
  var perf = window.__authorshipPerf = { ready: false, layout: null, renders: 0, view: null };
  // For the scale test: pan the Map for `ms` milliseconds and report the frame rate.
  perf.panTest = function (ms) {
    return new Promise(function (resolve) {
      if (!cy) { resolve(null); return; }
      var frames = 0, worst = 0, t0 = performance.now(), last = t0, dir = 1;
      function frame(now) {
        frames++;
        worst = Math.max(worst, now - last);
        last = now;
        cy.panBy({ x: 6 * dir, y: 2 * dir });
        if (frames % 60 === 0) dir = -dir;
        if (now - t0 < ms) requestAnimationFrame(frame);
        else resolve({ fps: frames * 1000 / (now - t0), worstFrameMs: Math.round(worst), zoom: cy.zoom(), frames: frames });
      }
      requestAnimationFrame(frame);
    });
  };
  window.__authorship = { cy: function () { return cy; }, model: function () { return M; }, state: S };

  // ------------------------------------------------------------------------------------------
  // DOM helpers (text only)
  function $(id) { return document.getElementById(id); }
  function el(tag, attrs) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === 'text') n.textContent = v;
      else if (k === 'class') n.className = v;
      else if (k.slice(0, 2) === 'on') n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? '' : v);
    });
    function add(c) {
      if (c == null || c === false) return;
      if (Array.isArray(c)) { c.forEach(add); return; }
      n.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
    }
    for (var i = 2; i < arguments.length; i++) add(arguments[i]);
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }
  function short(s, n) { s = String(s || '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  function nodeSeq(id) { return parseInt(String(id).split('.')[0], 10); }
  function pct(x) { return Math.round((x || 0) * 100) + '%'; }
  function cite(id) {
    var e = M && M.entry[nodeSeq(id)];
    return el('span', { class: 'cite', title: e ? 'hash ' + e.hash : null }, '#' + id + (e ? ' · ' + e.hash.slice(0, 12) : ''));
  }
  function whoChip(who, label) {
    var cls = who === 'human' || who === 'you' ? 'you' : who === 'mixed' ? 'mixed' : 'ai';
    var txt = label || (cls === 'you' ? 'You' : cls === 'mixed' ? 'You, changing Claude' : 'Claude');
    return el('span', { class: 'who ' + cls }, el('i', { class: 'sym ' + cls, 'aria-hidden': 'true' }), txt);
  }

  // ------------------------------------------------------------------------------------------
  // URL hash state
  function readHash() {
    var p = new URLSearchParams(location.hash.replace(/^#/, ''));
    var v = p.get('view');
    S.view = VIEWS.indexOf(v) >= 0 ? v : 'overview';
    S.focus = p.get('focus') || '';
    S.upto = parseInt(p.get('upto') || '0', 10) || 0;
    S.tl = p.get('tl') === 'all' ? 'all' : 'key';
    S.q = p.get('q') || '';
  }
  function writeHash() {
    var p = new URLSearchParams();
    p.set('view', S.view);
    if (S.focus) p.set('focus', S.focus);
    if (S.upto && M && S.upto < M.maxSeq) p.set('upto', String(S.upto));
    if (S.tl !== 'key') p.set('tl', S.tl);
    if (S.q) p.set('q', S.q);
    history.replaceState(null, '', location.pathname + location.search + '#' + p.toString());
  }

  // ------------------------------------------------------------------------------------------
  // Model
  function buildModel(g) {
    var m = { entry: {}, node: {}, bySeq: {}, out: {}, inn: {}, maxSeq: 0, evidence: {}, entries: g.entries, nodes: g.nodes };
    g.entries.forEach(function (e) { m.entry[e.seq] = e; if (e.seq > m.maxSeq) m.maxSeq = e.seq; });
    g.nodes.forEach(function (n) {
      m.node[n.id] = n;
      (m.bySeq[n.seq] = m.bySeq[n.seq] || []).push(n);
    });
    g.edges.forEach(function (x) {
      (m.out[x.src] = m.out[x.src] || []).push(x);
      (m.inn[x.dst] = m.inn[x.dst] || []).push(x);
    });
    g.nodes.forEach(function (n) {
      (n.milestones || []).forEach(function (ms) {
        if (ms.type === 'reduction_to_practice' && ms.evidence_seq) m.evidence[ms.evidence_seq] = n.id;
      });
    });
    m.options = {};
    g.nodes.forEach(function (n) { if (n.option != null) (m.options[n.seq] = m.options[n.seq] || []).push(n); });
    return m;
  }
  function kindOf(e) {
    var tags = (e.tags || []).concat(e.auto_tags || []);
    for (var i = 0; i < tags.length; i++) if (KIND_OF_TAG[tags[i]]) return { kind: KIND_OF_TAG[tags[i]], auto: (e.tags || []).indexOf(tags[i]) < 0 };
    return null;
  }
  function mainNode(seq) { return M.node[String(seq)] || null; }
  function isDead(n) { return n && (n.status === 'discarded' || n.status === 'rejected'); }

  // One sentence per entry, in plain words.
  function describe(e) {
    var n = mainNode(e.seq);
    var r = { who: e.actor === 'human' ? 'you' : e.actor === 'ai' ? 'ai' : 'sys', say: '', text: e.preview || '', cls: '', key: false };
    var ev = e.event;
    if (e.actor === 'human' && (ev === 'UserPromptSubmit' || ev === 'ManualNote')) {
      var k = kindOf(e);
      r.say = k ? SAY_KIND[k.kind] : (ev === 'ManualNote' ? 'added a note' : 'asked');
      r.key = true;
      var outs = (M.out[String(e.seq)] || []);
      outs.forEach(function (x) {
        var dst = M.node[x.dst];
        if (!dst || x.source === 'annotation') return;  // a suggestion still waiting for you is not a fact
        if (x.type === 'modifies' && dst.author === 'ai') r.say += ', changing Claude’s #' + x.dst;
        if (x.type === 'rejects') r.say += ', rejecting #' + x.dst;
      });
    } else if (ev === 'Stop' || ev === 'SubagentStop') {
      var opts = M.options[e.seq] || [];
      var props = (M.bySeq[e.seq] || []).filter(function (x) { return /\.p\d+$/.test(x.id); });
      if (opts.length) {
        r.say = 'offered ' + opts.length + ' options'; r.key = true; r.options = opts;
        r.text = (e.preview || '').split(/\s\(?1[.)]\s/)[0];  // the options are listed below
      }
      else if (props.length) { r.say = 'proposed a mechanism of its own'; r.key = true; }
      else r.say = ev === 'SubagentStop' ? 'finished a sub-task' : 'replied';
      if (n && (n.milestones || []).some(function (ms) { return ms.type === 'ai_origin_element' && ms.confirmed !== -1; })) {
        r.key = true;
        if (!props.length) r.say = 'introduced a mechanism you did not ask for';
      }
      if (!opts.length && !props.length) r.cls = r.key ? '' : 'work';
    } else if (e.kind === 'tool') {
      r.cls = 'work';
      var f = e.file || '';
      if (n && n.ibis === 'argument') {
        var failed = /tests fail/.test(n.label || '');
        r.say = failed ? 'ran the tests: they fail' : 'ran the tests: they pass';
        r.text = e.command || '';
        if (M.evidence[e.seq]) {
          r.say = 'ran the tests: they pass after failing';
          r.cls = 'work evidence'; r.key = true;
          r.text = 'Evidence that idea #' + M.evidence[e.seq] + ' works. ' + (e.command || '');
        }
      } else if (e.tool === 'Write') { r.say = 'wrote ' + f; r.text = ''; }
      else if (e.tool === 'Edit' || e.tool === 'MultiEdit' || e.tool === 'NotebookEdit') { r.say = 'edited ' + f; r.text = ''; }
      else if (e.tool === 'Bash') { r.say = 'ran a command'; r.text = e.command || ''; }
      else { r.say = 'used ' + e.tool; r.text = f || ''; }
      if (e.outcome === 'failure' && !(n && n.ibis === 'argument')) r.say += ' (failed)';
    } else if (ev === 'Confirm') {
      r.who = 'you'; r.key = true;
      var verb = { accept: 'confirmed', reject: 'rejected', edit: 'changed' }[e.decision] || e.decision;
      r.say = verb + ' a label on #' + e.target_seq;
      r.text = plainLabel(e.label, e.target_seq) + (e.edited_label ? ' \u2192 ' + plainLabel(e.edited_label, e.target_seq) : '');
    } else if (ev === 'GuardBlock') {
      r.say = 'The guard stopped Claude'; r.text = e.reason || ''; r.key = true;
    } else if (ev === 'Anchor') {
      r.say = 'Sealing' ; r.text = 'Timestamp ' + (e.status || '') + ' for the record up to #' + (e.anchored_seq || '?');
    } else if (ev === 'SessionStart') { r.say = 'Session started'; r.cls = 'session'; r.text = ''; }
    else if (ev === 'SessionEnd') { r.say = 'Session ended'; r.cls = 'session'; r.text = ''; }
    else if (ev === 'SessionReconciled') { r.say = 'Recovered a session that ended abruptly'; r.cls = 'session'; r.text = ''; }
    else if (ev === 'PreCompact') { r.say = 'Claude’s context was compacted'; r.cls = 'session'; r.text = ''; }
    else { r.say = ev; }
    if (isDead(n)) r.cls += ' dead';
    return r;
  }

  // A label in plain words: "milestone:conception_candidate" on #4 -> "#4 is a conception moment ..."
  function plainLabel(label, target) {
    label = String(label || '');
    if (label.indexOf('milestone:') === 0) { var t = label.slice(10); return '#' + target + ' is ' + (MS_LONG[t] || t.replace(/_/g, ' ')); }
    var p = label.split(':');
    if (p[0] === 'edge' && p.length === 4) return '#' + p[1] + ' ' + (REL_OUT[p[2]] || p[2]) + ' #' + p[3];
    if (label.indexOf('maturity:') === 0) return '#' + target + ' is at maturity ' + label.slice(9).replace(/_/g, ' ');
    return label;
  }

  // Labels on a node, with their state: typed, confirmed or a rule (solid), automatic (dashed), waiting, rejected.
  // `kind` is the entry's kind, already shown on its own chip; milestones that only repeat it are skipped.
  var SAME_AS_KIND = { problem_fixed: 'problem', decision_with_reason: 'decision', claim_candidate: 'claim', discard_with_reason: 'discard' };
  function rank(ms) { return ms.confirmed === 1 || (ms.tier === 0 && ms.confirmed === 0) ? 3 : ms.confirmed === 2 ? 2 : ms.confirmed === -1 ? 1 : 0; }
  function labelChips(n, kind) {
    var out = [];
    if (!n) return out;
    var seenName = {};
    (n.milestones || []).slice().sort(function (a, b) { return rank(b) - rank(a); }).forEach(function (ms) {
      var name = MS_NAME[ms.type];
      if (!name || (kind && SAME_AS_KIND[ms.type] === kind && ms.confirmed !== -1)) return;
      if (seenName[name]) return;  // one chip per label, in its strongest state
      seenName[name] = 1;
      var cls = 'lbl', title;
      if (ms.confirmed === 1 || (ms.tier === 0 && ms.confirmed === 0)) { title = ms.tier === 0 ? 'declared by you, or found by a rule' : 'confirmed by you'; }
      else if (ms.confirmed === 2) { cls += ' auto'; title = 'set automatically by the classifier (' + pct(ms.score) + ' sure)'; }
      else if (ms.confirmed === -1) { cls += ' bad'; title = 'rejected by you'; name = name + ' (rejected)'; }
      else { cls += ' wait'; title = 'waiting for your answer in Review'; name = name + '?'; }
      if (ms.type === 'reduction_to_practice') cls += ' good';
      out.push(el('span', { class: cls, title: title }, name));
    });
    if (n.status === 'discarded' && kind !== 'discard') out.push(el('span', { class: 'lbl bad' }, 'discarded'));
    if (n.status === 'rejected') out.push(el('span', { class: 'lbl bad' }, 'rejected'));
    if (n.status === 'modified') out.push(el('span', { class: 'lbl' }, 'changed by you'));
    return out;
  }

  // ------------------------------------------------------------------------------------------
  // Header and chain status
  function renderHeader() {
    $('project').textContent = G.project || '';
    var c = G.chain, b = $('chain-badge');
    if (c.ok) {
      b.className = 'badge ok';
      b.textContent = '✓ Record intact · ' + c.entries + ' entries · ' + (c.sealed_upto ? 'sealed to #' + c.sealed_upto : 'not sealed yet');
      $('broken-banner').hidden = true;
    } else {
      b.className = 'badge bad';
      b.textContent = '✗ Record broken at #' + c.broken_at;
      var bb = $('broken-banner');
      bb.textContent = 'Entry #' + c.broken_at + ' was changed after it was written (' + (c.reason || 'verification failed') +
        '). Do not edit the ledger to fix it; tell your attorney.';
      bb.hidden = false;
    }
    var n = G.review.length, rc = $('review-count');
    rc.textContent = String(n);
    rc.hidden = !n;
    rc.setAttribute('aria-label', n + ' waiting');
    $('sync').textContent = 'updated ' + new Date().toLocaleTimeString();
    var big = G.nodes.length > G.limits.stages_only_above;
    var nt = $('notice');
    nt.hidden = !big;
    if (big) nt.textContent = 'This record has ' + G.nodes.length + ' entries and links, too many to draw: the Map is off. Overview, Timeline and Review work as usual.';
    $('tab-map').disabled = big;
  }

  // ------------------------------------------------------------------------------------------
  // Who labels the entries (shown on Overview and Review)
  function renderClassifier(hostId) {
    var c = G.classifier || {}, host = clear($(hostId)), cls = 'cls-strip';
    var how = el('button', { class: 'link-btn', type: 'button', onclick: function () { var d = $('setup'); if (d.showModal) d.showModal(); else d.setAttribute('open', ''); } }, 'How to change it');
    var msg;
    if (c.state === 'off') { cls += ' off'; msg = [el('strong', null, 'Automatic labels are off.'), ' Only tags you type and the deterministic rules label entries.']; }
    else if (c.state === 'no-backend') { cls += ' err'; msg = [el('strong', null, 'Nothing is labeling entries.'), ' No Jev key is set and the claude command was not found.']; }
    else if (c.state === 'error' && !c.backend) { cls += ' err'; msg = [el('strong', null, 'The classifier failed.'), ' ' + (c.error || '') + ' It retries on the next change.']; }
    else if (c.backend === 'jev') {
      cls += ' third';
      msg = [el('strong', null, 'Labeled automatically by Jev'), c.model ? ' (' + c.model + ')' : '', '. Entry text is sent to ' + c.sends_to + '.'];
    } else if (c.backend === 'claude-cli') {
      msg = [el('strong', null, 'Labeled automatically by Claude'), c.model ? ' (' + c.model + ')' : '', ', through your Claude Code login. No new party receives your text.'];
    } else if (c.backend) {
      msg = [el('strong', null, 'Labeled automatically by ' + (c.label || c.backend)), c.model ? ' (' + c.model + ')' : '', '.'];
    } else { cls += ' off'; msg = [el('strong', null, 'The classifier has not run yet.'), ' It starts with the next Claude Code session.']; }
    if (c.state === 'error' && c.backend) { cls += ' err'; msg.push(' The last run failed: ' + (c.error || 'see .authorship/errors.log') + '.'); }
    host.className = cls;
    host.appendChild(el('span', null, msg));
    if (c.labeled != null) host.appendChild(el('span', { class: 'muted' }, c.labeled + ' entries labeled so far.'));
    host.appendChild(how);
  }

  // ------------------------------------------------------------------------------------------
  // Overview
  function renderOverview() {
    renderClassifier('cls-overview');
    var c = G.chain, inv = G.inventions || [];
    var tot = { human: 0, mixed: 0, ai: 0 };
    inv.forEach(function (x) { tot.human += x.counts.human; tot.mixed += x.counts.mixed; tot.ai += x.counts.ai; });
    var all = tot.human + tot.mixed + tot.ai;
    var k = clear($('kpis'));
    function tile(cls, label, value, note, extra) {
      return el('div', { class: 'kpi ' + cls }, el('div', { class: 'k-label' }, label), el('div', { class: 'k-value' }, value),
        el('div', { class: 'k-note' }, note), extra || null);
    }
    k.appendChild(c.ok ? tile('good', 'Record', 'Intact', c.entries + ' entries, none changed since written')
                       : tile('bad', 'Record', 'Broken at #' + c.broken_at, c.reason || 'an entry was changed'));
    k.appendChild(tile(c.sealed_upto ? 'good' : '', 'External timestamp', c.sealed_upto ? 'Sealed to #' + c.sealed_upto : 'Not sealed yet',
      c.sealed_upto ? c.unsealed + ' newer entries not sealed' : 'run  authorship seal  in your terminal'));
    k.appendChild(tile('', 'Elements from you', all ? (tot.human + tot.mixed) + ' of ' + all : '—',
      all ? (tot.mixed ? tot.mixed + ' of them change something Claude proposed' : 'across ' + inv.length + ' claim' + (inv.length === 1 ? '' : 's')) : 'no claim yet'));
    var waiting = G.review.length;
    k.appendChild(tile(waiting ? 'attn' : 'good', 'Waiting for you', waiting ? String(waiting) : 'Nothing',
      waiting ? 'labels to check' : 'all labels decided or automatic',
      waiting ? el('button', { class: 'link-btn', type: 'button', onclick: function () { go('review'); } }, 'Open Review') : null));

    var box = clear($('inventions'));
    if (!inv.length) {
      box.appendChild(el('div', { class: 'empty' },
        el('strong', null, 'No claim yet. '),
        'When you describe the invention as a whole ("a method that…", "a system that…"), it appears here with everything it rests on. Until then, the Timeline shows the record as it grows.'));
    }
    inv.forEach(function (x) { box.appendChild(claimCard(x)); });

    var against = [];
    inv.forEach(function (x) { x.elements.forEach(function (e) { if (e.origin === 'ai') against.push({ node: e.node, text: e.text, changed: e.changed_by, claim: x.id }); }); });
    G.nodes.forEach(function (n) {
      (n.milestones || []).forEach(function (ms) {
        if (ms.type === 'ai_origin_element' && ms.confirmed !== -1 && ms.confirmed !== 0 && !against.some(function (a) { return a.node === n.id; }))
          against.push({ node: n.id, text: n.label, changed: [], flag: true });
      });
    });
    var ab = clear($('against'));
    if (against.length) {
      ab.appendChild(el('div', { class: 'card against' },
        el('h2', null, 'What came from Claude'),
        el('p', { class: 'muted' }, 'Stated plainly, because an honest record carries more weight. Elements Claude proposed, and replies where it introduced something you did not ask for:'),
        el('ul', null, against.map(function (a) {
          return el('li', null, cite(a.node), ' ', short(a.text, 160),
            a.changed && a.changed.length ? el('span', { class: 'muted' }, ' — changed by you at #' + a.changed.join(', #')) : null);
        }))));
    }
    var st = clear($('stages-summary'));
    if (G.stages.length) {
      st.appendChild(el('h2', { class: 'section' }, 'Stages of the work'));
      st.appendChild(el('div', { class: 'stages-line' }, G.stages.map(function (s, i) {
        return [i ? el('span', { class: 'muted', 'aria-hidden': 'true' }, '→') : null,
          el('span', { class: 'stage-pill' }, s.name + '  #' + s.first_seq + '–#' + s.last_seq)];
      })));
    }
  }

  function claimCard(x) {
    var n = x.counts, total = n.human + n.mixed + n.ai;
    var card = el('article', { class: 'card claim', 'data-claim': x.id });
    card.appendChild(el('div', { class: 'claim-kicker' }, 'Claim ', cite(x.id)));
    card.appendChild(el('p', { class: 'claim-text' }, x.text));
    if (total) {
      var bar = el('div', { class: 'bar', role: 'img', 'aria-label': n.human + ' from you, ' + n.mixed + ' from you changing Claude, ' + n.ai + ' from Claude' });
      [['you', n.human], ['mixed', n.mixed], ['ai', n.ai]].forEach(function (p) {
        if (p[1]) bar.appendChild(el('span', { class: p[0], style: 'flex:' + p[1] }));
      });
      card.appendChild(el('div', { class: 'share' },
        el('div', { class: 'share-title' }, 'It rests on ' + total + ' element' + (total === 1 ? '' : 's') + ':'),
        bar,
        el('div', { class: 'share-legend' },
          el('span', null, el('i', { class: 'sym you', 'aria-hidden': 'true' }), ' From you ', el('b', null, String(n.human))),
          el('span', null, el('i', { class: 'sym mixed', 'aria-hidden': 'true' }), ' You, changing Claude ', el('b', null, String(n.mixed))),
          el('span', null, el('i', { class: 'sym ai', 'aria-hidden': 'true' }), ' From Claude ', el('b', null, String(n.ai))))));
      var order = { human: 0, mixed: 1, ai: 2 };
      var els = x.elements.slice().sort(function (a, b) { return order[a.origin] - order[b.origin] || a.seq - b.seq; });
      card.appendChild(el('ul', { class: 'elements' }, els.map(function (e) {
        var extra = [];
        if (e.builds_on_ai.length) extra.push(el('span', { class: 'lbl' }, 'changes Claude’s #' + e.builds_on_ai.join(', #')));
        if (e.changed_by.length) extra.push(el('span', { class: 'lbl' }, 'changed by you at #' + e.changed_by.join(', #')));
        e.evidence.forEach(function (s) { extra.push(el('span', { class: 'lbl good' }, 'tests prove it at #' + s)); });
        return el('li', null, whoChip(e.origin === 'human' ? 'you' : e.origin),
          el('div', { class: 'el-text' }, e.text, extra.length ? el('div', { class: 'el-extra' }, extra) : null),
          el('a', { href: '#view=map&focus=' + encodeURIComponent(x.id), onclick: function (ev) { ev.preventDefault(); S.focus = x.id; S.sel = e.node; go('map'); } }, cite(e.node)));
      })));
    } else {
      card.appendChild(el('p', { class: 'muted' }, 'No element is linked to this claim yet. Links appear as the classifier reads the record.'));
    }
    card.appendChild(el('p', { class: 'links-note' }, 'Links used: ' + x.links + '. ',
      el('a', { href: '#view=map&focus=' + encodeURIComponent(x.id), onclick: function (ev) { ev.preventDefault(); S.focus = x.id; go('map'); } }, 'See it on the Map')));
    return card;
  }

  // ------------------------------------------------------------------------------------------
  // Timeline
  function timelineRows() {
    var rows = [], q = S.q.toLowerCase(), group = null;
    M.entries.forEach(function (e) {
      var d = describe(e);
      if (q) {
        var hay = (d.say + ' ' + (e.preview || '') + ' ' + (e.command || '') + ' ' + (e.file || '') + ' #' + e.seq).toLowerCase();
        if (hay.indexOf(q) < 0) return;
      }
      if (S.tl === 'key' && !d.key) {
        if (/work/.test(d.cls) && !q) {  // fold routine work between key moments into one row
          if (!group) { group = { group: true, seqs: [], stage: e.stage, files: {}, fail: false }; rows.push(group); }
          group.seqs.push(e.seq);
          if (e.file) group.files[e.file] = 1;
          if (/fail/.test(d.say)) group.fail = true;
        }
        return;
      }
      group = null;
      rows.push({ e: e, d: d });
    });
    return rows;
  }

  function renderTimeline() {
    var host = clear($('timeline'));
    var rows = timelineRows();
    if (!rows.length) { host.appendChild(el('div', { class: 'empty' }, S.q ? 'Nothing matches “' + S.q + '”.' : 'The record is empty.')); return; }
    var stageOf = function (r) { return r.group ? r.stage : r.e.stage; };
    var cur = undefined, list = null, shown = 0;
    for (var i = 0; i < rows.length && shown < tlShown; i++) {
      var r = rows[i], st = stageOf(r) || null;
      if (r.e && /session/.test(r.d.cls)) st = cur === undefined ? null : cur;
      if (st !== cur || !list) {
        cur = st;
        var sec = el('section', { class: 'tl-stage' });
        var info = st && G.stages.filter(function (s) { return s.name === st; })[0];
        sec.appendChild(el('div', { class: 'tl-stage-head' }, el('h2', null, st || 'Before any stage'),
          info ? el('span', { class: 'muted' }, '#' + info.first_seq + '–#' + info.last_seq) : null));
        list = el('ol', { class: 'tl-list' });
        sec.appendChild(list);
        host.appendChild(sec);
      }
      list.appendChild(r.group ? groupRow(r) : entryRow(r.e, r.d));
      shown++;
    }
    if (rows.length > shown) {
      host.appendChild(el('div', { class: 'more-row' }, el('button', { class: 'btn', type: 'button', onclick: function () { tlShown += TL_PAGE; renderTimeline(); } },
        'Show ' + Math.min(TL_PAGE, rows.length - shown) + ' more (' + (rows.length - shown) + ' left)')));
    }
  }

  function entryRow(e, d) {
    if (/session/.test(d.cls)) return el('li', { class: 'tl-item session', 'data-seq': String(e.seq) }, '#' + e.seq + '  ' + d.say + '  ' + (e.ts || '').slice(0, 16).replace('T', ' '));
    var n = mainNode(e.seq);
    var li = el('li', { class: 'tl-item ' + d.cls, 'data-seq': String(e.seq) });
    var k = kindOf(e);
    var labels = labelChips(n, k && k.kind);
    if (k && k.auto) labels.unshift(el('span', { class: 'lbl auto', title: 'set automatically by the classifier' }, k.kind));
    else if (k) labels.unshift(el('span', { class: 'lbl', title: 'tag typed by you' }, k.kind));
    var main = el('div', { class: 'tl-main' },
      el('div', { class: 'tl-say' }, d.who === 'sys' ? null : [whoChip(d.who === 'you' ? 'you' : 'ai'), ' '], d.say),
      d.text ? el('div', { class: 'tl-text' }, d.text) : null);
    if (d.options) {
      main.appendChild(el('ul', { class: 'tl-options' }, d.options.map(function (o) {
        var st = o.status === 'rejected' ? 'rejected' : o.status === 'modified' ? 'changed by you' : o.status === 'adopted' ? 'adopted' : '';
        return el('li', null, el('span', { class: 'cite' }, '#' + o.id), short(o.label, 140), st ? el('span', { class: 'lbl' + (o.status === 'rejected' ? ' bad' : '') }, st) : null);
      })));
    }
    var btn = el('button', { class: 'tl-row', type: 'button', 'aria-expanded': S.open === e.seq ? 'true' : 'false',
      onclick: function () { S.open = S.open === e.seq ? null : e.seq; renderTimeline(); } },
      el('span', { class: 'tl-seq' }, '#' + e.seq), main,
      el('span', { class: 'tl-labels' }, labels));
    li.appendChild(btn);
    if (S.open === e.seq) { var det = el('div', { class: 'tl-detail' }); li.appendChild(det); fillDetail(det, String(e.seq), false); }
    return li;
  }

  function groupRow(r) {
    var files = Object.keys(r.files);
    var say = 'worked: ' + r.seqs.length + ' step' + (r.seqs.length === 1 ? '' : 's') + (files.length ? ' on ' + files.slice(0, 3).join(', ') + (files.length > 3 ? '…' : '') : '');
    return el('li', { class: 'tl-item work' },
      el('button', { class: 'tl-row', type: 'button', title: 'Show every step', onclick: function () { S.tl = 'all'; writeHash(); syncControls(); renderTimeline(); } },
        el('span', { class: 'tl-seq' }, '#' + r.seqs[0] + (r.seqs.length > 1 ? '–' + r.seqs[r.seqs.length - 1] : '')),
        el('div', { class: 'tl-main' }, el('div', { class: 'tl-say' }, whoChip('ai'), ' ', say),
          el('div', { class: 'tl-text' }, r.fail ? 'Some steps failed. Choose “Everything” to see each one.' : 'Choose “Everything” to see each step.')),
        el('span', { class: 'tl-labels' })));
  }

  // ------------------------------------------------------------------------------------------
  // Detail of one entry or node (Timeline row, Map side panel)
  function fillDetail(box, id, inMap) {
    clear(box);
    var seq = nodeSeq(id), e = M.entry[seq], n = M.node[id];
    if (!e) { box.appendChild(el('p', { class: 'muted' }, 'Entry #' + id + ' is not in the record.')); return; }
    var d = describe(e);
    var body = el('div', { class: 'detail-body' });
    var title = n && n.option != null ? 'Option ' + n.option + ' of Claude’s reply #' + seq : (d.who === 'sys' ? d.say : null);
    body.appendChild(el('h3', null, title || [whoChip(d.who === 'you' ? 'you' : 'ai'), ' ', d.say]));
    var facts = el('dl', { class: 'facts' });
    function fact(k, v) { if (v) { facts.appendChild(el('dt', null, k)); facts.appendChild(el('dd', null, v)); } }
    fact('Entry', '#' + id);
    fact('When', (e.ts || '').replace('T', ' ').replace('Z', ' UTC'));
    fact('Stage', e.stage);
    fact('Hash', e.hash.slice(0, 16) + '…');
    fact('File', e.file);
    fact('Command', e.command);
    fact('Error', e.error);
    body.appendChild(facts);
    var kk = kindOf(e);
    var chips = labelChips(n, kk && kk.kind);
    if (kk) chips.unshift(el('span', { class: 'lbl' + (kk.auto ? ' auto' : ''), title: kk.auto ? 'set automatically by the classifier' : 'tag typed by you' }, kk.kind));
    if (chips.length) body.appendChild(el('div', { class: 'el-extra' }, chips));
    var textBox = el('pre', { class: 'text' }, n && n.option != null ? n.label : (e.preview || ''));
    if (!(n && n.option != null) && (e.text_len || 0) > 0) body.appendChild(textBox);
    var rels = el('ul', { class: 'rel' });
    function how(x) {
      return x.source === 'auto' ? el('span', { class: 'muted' }, ' (automatic)') :
        x.source === 'annotation' ? el('span', { class: 'muted' }, ' (suggested; waiting for you in Review)') : null;
    }
    (M.out[id] || []).forEach(function (x) { rels.appendChild(el('li', null, 'This ', REL_OUT[x.type] || x.type, ' ', relLink(x.dst, inMap), how(x))); });
    (M.inn[id] || []).forEach(function (x) { rels.appendChild(el('li', null, 'This ', REL_IN[x.type] || x.type, ' ', relLink(x.src, inMap), how(x))); });
    if (rels.childNodes.length) { body.appendChild(el('h3', null, 'Links')); body.appendChild(rels); }
    box.appendChild(body);
    if (!(n && n.option != null) && (e.text_len || 0) > 0) {
      fetch('/api/entry/' + seq, { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (j && j.text != null) textBox.textContent = j.text;
      }).catch(function () {});
    }
    if (e.kind === 'tool' && e.input_blob && /Edit|Write/.test(e.tool || '')) {
      var pre = el('pre', { class: 'diff', 'aria-label': 'Code change' }, 'Loading the change…');
      body.appendChild(el('h3', null, 'Code change'));
      body.appendChild(pre);
      fetch('/api/blob/' + e.input_blob, { cache: 'no-store' }).then(function (r) { return r.text(); }).then(function (t) {
        var inp = {};
        try { inp = JSON.parse(t); } catch (err) { /* keep empty */ }
        clear(pre);
        var oldS = inp.old_string || '', newS = inp.new_string != null ? inp.new_string : (inp.content || '');
        if (inp.edits && inp.edits.length) { oldS = inp.edits.map(function (x) { return x.old_string; }).join('\n'); newS = inp.edits.map(function (x) { return x.new_string; }).join('\n'); }
        diffLines(oldS, newS).forEach(function (l) { pre.appendChild(el('div', { class: l[0] === '+' ? 'add' : l[0] === '-' ? 'del' : '' }, l)); });
      }).catch(function () { pre.textContent = 'The change could not be loaded.'; });
    }
  }
  function relLink(id, inMap) {
    var n = M.node[id];
    var label = '#' + id + (n ? ' (' + (n.author === 'human' ? 'you' : 'Claude') + '): ' + short(n.label, 70) : '');
    return el('a', { href: '#', onclick: function (ev) {
      ev.preventDefault();
      if (inMap) { S.sel = id; selectInMap(id); } else { S.open = nodeSeq(id); renderTimeline(); var t = document.querySelector('.tl-item[data-seq="' + nodeSeq(id) + '"]'); if (t) t.scrollIntoView({ block: 'center' }); }
    } }, label);
  }
  function diffLines(a, b) {
    var A = String(a).split('\n'), B = String(b).split('\n');
    if (!a) return B.map(function (l) { return '+ ' + l; });
    var n = A.length, m = B.length, L = [], i, j;
    if (n * m > 400000) return A.map(function (l) { return '- ' + l; }).concat(B.map(function (l) { return '+ ' + l; }));
    for (i = 0; i <= n; i++) { L.push(new Array(m + 1).fill(0)); }
    for (i = n - 1; i >= 0; i--) for (j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    var out = []; i = 0; j = 0;
    while (i < n && j < m) {
      if (A[i] === B[j]) { out.push('  ' + A[i]); i++; j++; }
      else if (L[i + 1][j] >= L[i][j + 1]) { out.push('- ' + A[i]); i++; }
      else { out.push('+ ' + B[j]); j++; }
    }
    while (i < n) out.push('- ' + A[i++]);
    while (j < m) out.push('+ ' + B[j++]);
    return out;
  }

  // ------------------------------------------------------------------------------------------
  // Map
  function focusOptions() {
    var sel = clear($('map-focus'));
    (G.inventions || []).forEach(function (x) { sel.appendChild(el('option', { value: x.id }, 'Claim #' + x.id + ': ' + short(x.text, 60))); });
    sel.appendChild(el('option', { value: 'key' }, 'Key entries of the whole record'));
    sel.appendChild(el('option', { value: 'all' }, 'Everything, including every reply and tool call'));
    if (!S.focus) S.focus = (G.inventions && G.inventions.length) ? G.inventions[0].id : 'key';
    sel.value = S.focus;
    if (sel.value !== S.focus) { S.focus = 'key'; sel.value = 'key'; }
  }

  function lineageSet(root) {
    var curated = G.edges.some(function (x) { return x.source === 'confirmed' || x.source === 'auto'; });
    var ok = function (x) { return LINEAGE[x.type] && (!curated || x.source === 'confirmed' || x.source === 'auto'); };
    var seen = {}, stack = [root];
    seen[root] = 1;
    while (stack.length) {
      var id = stack.pop();
      (M.out[id] || []).forEach(function (x) { if (ok(x) && !seen[x.dst]) { seen[x.dst] = 1; stack.push(x.dst); } });
    }
    return { set: seen, ok: ok };
  }

  function mapSelection() {
    var nodes = [], ids = {}, edgeOk;
    if (S.focus === 'all') {
      M.nodes.forEach(function (n) { ids[n.id] = 1; });
      edgeOk = function () { return true; };
    } else if (S.focus === 'key') {
      M.nodes.forEach(function (n) {
        if (n.ibis === 'response' && !(n.milestones || []).some(function (ms) { return ms.type === 'ai_origin_element'; })) return;
        if (n.ibis === 'action') return;
        ids[n.id] = 1;
      });
      edgeOk = function (x) { return x.type !== 'responds_to' || x.source !== 'rule'; };
    } else {
      var lin = lineageSet(S.focus);
      Object.keys(lin.set).forEach(function (id) { ids[id] = 1; });
      Object.keys(lin.set).forEach(function (id) {  // the tests that show an element working, or failing against it
        (M.inn[id] || []).forEach(function (x) { if ((x.type === 'supports' || x.type === 'objects_to' || x.type === 'rejects') && M.node[x.src]) ids[x.src] = 1; });
      });
      edgeOk = function (x) { return lin.ok(x) || x.type === 'supports' || x.type === 'objects_to' || x.type === 'rejects' || x.type === 'discards'; };
    }
    var upto = S.upto || M.maxSeq;
    Object.keys(ids).forEach(function (id) { var n = M.node[id]; if (n && n.seq <= upto) nodes.push(n); });
    nodes.sort(function (a, b) { return a.seq - b.seq || (a.id < b.id ? -1 : 1); });
    var keep = {};
    nodes.forEach(function (n) { keep[n.id] = 1; });
    var edges = G.edges.filter(function (x) { return keep[x.src] && keep[x.dst] && edgeOk(x); });
    return { nodes: nodes, edges: edges };
  }

  function mapElements(selN, selE) {
    var cols = {}, colN = 0, slot = {}, laneDepth = [1, 1, 1, 1];
    selN.forEach(function (n) {
      if (!(n.seq in cols)) cols[n.seq] = colN++;
      var lane = LANE_OF[n.ibis] != null ? LANE_OF[n.ibis] : 1;
      var key = cols[n.seq] + ':' + lane;
      slot[n.id] = slot[key] = (slot[key] || 0) + 1;
      laneDepth[lane] = Math.max(laneDepth[lane], slot[key]);
    });
    var COLW = selN.length > BIG ? 70 : 170, ROWH = 84, top = [], y = 0;
    for (var l = 0; l < 4; l++) { top.push(y); y += laneDepth[l] * ROWH + 36; }
    var left = -40, right = 60 + Math.max(0, colN - 1) * COLW + 100, width = right - left;
    mapElements.size = { w: width, h: y };
    var els = [];
    for (l = 0; l < 4; l++) {
      var h = laneDepth[l] * ROWH + 26;
      els.push({ group: 'nodes', data: { id: '__lane' + l, w: width, h: h },
        position: { x: left + width / 2, y: top[l] + h / 2 }, classes: 'lane', selectable: false, grabbable: false, locked: true });
      els.push({ group: 'nodes', data: { id: '__lanelabel' + l, label: LANES[l] },
        position: { x: left + 8, y: top[l] + 12 }, classes: 'lanelabel', selectable: false, grabbable: false, locked: true });
    }
    selN.forEach(function (n) {
      var lane = LANE_OF[n.ibis] != null ? LANE_OF[n.ibis] : 1;
      var cls = [n.author === 'human' ? 'you' : 'ai'];
      if (isDead(n)) cls.push('dead');
      if (n.ibis === 'claim') cls.push('claim');
      var tag = n.author === 'human' ? 'You' : 'Claude';
      els.push({ group: 'nodes', data: { id: n.id, label: '#' + n.id + ' ' + tag + '\n' + short(n.label, 46) },
        position: { x: 60 + cols[n.seq] * COLW, y: top[lane] + (slot[n.id] - 1) * ROWH + 40 }, classes: cls.join(' ') });
    });
    selE.forEach(function (x, i) {
      els.push({ group: 'edges', data: { id: 'e' + i, source: x.src, target: x.dst, rel: REL_OUT[x.type] || x.type },
        classes: (EDGE_CLASS[x.type] || 'built') + (x.source === 'auto' ? ' auto' : '') });
    });
    return els;
  }

  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function renderMap(force) {
    if (G.nodes.length > G.limits.stages_only_above) return;
    if (typeof window.cytoscape !== 'function') { setTimeout(function () { renderMap(force); }, 50); return; }
    focusOptions();
    var slider = $('map-slider');
    slider.max = String(M.maxSeq);
    if (!S.upto || S.upto > M.maxSeq) S.upto = M.maxSeq;
    slider.value = String(S.upto);
    $('map-slider-out').textContent = '#' + S.upto + (S.upto === M.maxSeq ? ' (latest)' : ' of ' + M.maxSeq);
    var key = [G.etag, S.focus, S.upto].join('|');
    if (cy && key === cyKey && !force) return;
    cyKey = key;
    var t0 = performance.now();
    var sel = mapSelection();
    var els = mapElements(sel.nodes, sel.edges);
    var big = sel.nodes.length > BIG;
    var you = cssVar('--you'), ai = cssVar('--ai'), ink = cssVar('--ink'), ink2 = cssVar('--ink-2'), surf = cssVar('--surface'),
      surf2 = cssVar('--surface-2'), muted = cssVar('--muted'), crit = cssVar('--critical'), good = cssVar('--good');
    if (cy) cy.destroy();
    // size the canvas to the drawing, so a small lineage is not lost in empty space
    var box = $('map'), avail = box.parentNode.clientWidth || 900, sz = mapElements.size;
    var scale = Math.min(1.3, avail / Math.max(1, sz.w));
    box.style.height = Math.max(320, Math.min(680, Math.round(sz.h * Math.max(scale, 0.35) + 60))) + 'px';
    cy = window.cytoscape({
      container: $('map'), elements: els, layout: { name: 'preset' },
      minZoom: 0.05, maxZoom: 3, wheelSensitivity: 0.3, boxSelectionEnabled: false,
      hideEdgesOnViewport: big, textureOnViewport: big, pixelRatio: big ? 1 : 'auto',
      style: [
        { selector: 'node', style: { 'width': 24, 'height': 24, 'label': 'data(label)', 'font-size': 12, 'color': ink2,
          'text-wrap': 'wrap', 'text-max-width': 132, 'text-background-color': surf, 'text-background-opacity': 0.85, 'text-background-padding': 2, 'text-valign': 'bottom', 'text-margin-y': 5, 'min-zoomed-font-size': big ? 9 : 0,
          'border-width': 2, 'border-color': surf } },
        { selector: 'node.you', style: { 'shape': 'ellipse', 'background-color': you } },
        { selector: 'node.ai', style: { 'shape': 'round-rectangle', 'background-color': ai } },
        { selector: 'node.claim', style: { 'width': 30, 'height': 30, 'border-width': 3, 'border-color': ink, 'font-weight': 'bold', 'color': ink } },
        { selector: 'node.dead', style: { 'background-opacity': 0.25, 'border-style': 'dashed', 'border-color': muted, 'border-width': 2 } },
        { selector: 'node.lane', style: { 'shape': 'rectangle', 'width': 'data(w)', 'height': 'data(h)', 'background-color': surf2,
          'background-opacity': 0.7, 'border-width': 0, 'label': '', 'events': 'no', 'z-index': 0 } },
        { selector: 'node.lanelabel', style: { 'width': 1, 'height': 1, 'background-opacity': 0, 'border-width': 0, 'label': 'data(label)',
          'text-valign': 'center', 'text-halign': 'right', 'font-size': 12, 'font-weight': 'bold', 'color': muted,
          'text-background-opacity': 0, 'events': 'no', 'text-wrap': 'none', 'min-zoomed-font-size': 0 } },
        { selector: 'edge', style: { 'width': 2, 'line-color': muted, 'target-arrow-color': muted, 'target-arrow-shape': 'triangle',
          'arrow-scale': 1, 'curve-style': big ? 'haystack' : 'bezier', 'opacity': 0.9 } },
        { selector: 'edge.changed', style: { 'line-color': '#7a5cd6', 'target-arrow-color': '#7a5cd6', 'width': 2.5 } },
        { selector: 'edge.against', style: { 'line-color': crit, 'target-arrow-color': crit, 'line-style': 'dashed' } },
        { selector: 'edge.work', style: { 'line-color': good, 'target-arrow-color': good } },
        { selector: 'node:selected', style: { 'border-color': cssVar('--focus'), 'border-width': 4 } },
        { selector: '.faded', style: { 'opacity': 0.15 } }
      ]
    });
    cy.on('tap', 'node', function (ev) { if (!ev.target.hasClass('lane') && !ev.target.hasClass('lanelabel')) { S.sel = ev.target.id(); showSel(); } });
    var tip = $('map-tip');
    function showTip(ev, textFn) {
      var p = ev.renderedPosition || ev.target.renderedMidpoint();
      tip.textContent = textFn();
      tip.style.left = Math.round(p.x + 14) + 'px';
      tip.style.top = Math.round(p.y + 10) + 'px';
      tip.hidden = false;
    }
    cy.on('mouseover', 'node', function (ev) {
      var t = ev.target;
      if (t.hasClass('lane') || t.hasClass('lanelabel')) return;
      var n = M.node[t.id()];
      showTip(ev, function () { return '#' + t.id() + ' · ' + (n.author === 'human' ? 'You' : 'Claude') + ': ' + short(n.label, 160); });
    });
    cy.on('mouseover', 'edge', function (ev) {
      var x = ev.target.data();
      showTip(ev, function () { return '#' + x.source + ' ' + x.rel + ' #' + x.target + (ev.target.hasClass('auto') ? ' (automatic)' : ''); });
    });
    cy.on('mouseout', 'node, edge', function () { tip.hidden = true; });
    cy.on('viewport', function () { tip.hidden = true; });
    cy.on('tap', function (ev) { if (ev.target === cy) { S.sel = null; cy.elements().removeClass('faded'); fillLegendDetail(); } });
    cy.fit(undefined, 36);
    if (cy.zoom() > 1.3) { cy.zoom(1.3); cy.center(); }
    if (S.sel && cy.getElementById(S.sel).length) showSel(); else fillLegendDetail();
    perf.layout = performance.now() - t0;
    perf.ready = true;
    perf.renders++;
    perf.nodes = sel.nodes.length;
  }
  function selectInMap(id) { if (cy && cy.getElementById(id).length) { S.sel = id; showSel(); cy.animate({ center: { eles: cy.getElementById(id) } }, { duration: 250 }); } }
  function showSel() {
    if (!cy) return;
    cy.elements().unselect();
    var n = cy.getElementById(S.sel);
    if (!n.length) return;
    n.select();
    var hood = n.closedNeighborhood();
    cy.elements().not(hood).not('.lane').not('.lanelabel').addClass('faded');
    hood.removeClass('faded');
    fillDetail($('map-detail'), S.sel, true);
  }
  function fillLegendDetail() {
    var d = clear($('map-detail'));
    var x = (G.inventions || []).filter(function (i) { return i.id === S.focus; })[0];
    if (x) {
      d.appendChild(el('h3', null, 'Claim #' + x.id));
      d.appendChild(el('p', null, x.text));
      d.appendChild(el('p', { class: 'muted' }, 'Shown: the claim, every element it is built from (links: ' + x.links + '), and the tests that show them working or failing. Select a shape to read it.'));
    } else {
      d.appendChild(el('p', { class: 'muted' }, 'Select a shape to read its entry and its links.'));
    }
  }

  function renderLegend() {
    var lg = clear($('map-legend'));
    lg.appendChild(el('li', null, el('i', { class: 'sym you', 'aria-hidden': 'true' }), 'You'));
    lg.appendChild(el('li', null, el('i', { class: 'sym ai', 'aria-hidden': 'true' }), 'Claude'));
    lg.appendChild(el('li', null, el('i', { class: 'sym dead', 'aria-hidden': 'true' }), 'Discarded or rejected'));
    lg.appendChild(el('li', null, el('span', { class: 'line' }), 'Builds on'));
    lg.appendChild(el('li', null, el('span', { class: 'line changed' }), 'Changes'));
    lg.appendChild(el('li', null, el('span', { class: 'line against' }), 'Rejects, or tests fail'));
    lg.appendChild(el('li', null, el('span', { class: 'line work' }), 'Implements, or tests pass'));
  }

  function togglePlay() {
    if (playing) { clearInterval(playing); playing = null; $('map-play').textContent = 'Play'; return; }
    if (S.upto >= M.maxSeq) S.upto = 1;
    $('map-play').textContent = 'Pause';
    var step = Math.max(1, Math.round(M.maxSeq / 60));
    playing = setInterval(function () {
      S.upto = Math.min(M.maxSeq, S.upto + step);
      writeHash();
      renderMap();
      if (S.upto >= M.maxSeq) togglePlay();
    }, 250);
  }

  // ------------------------------------------------------------------------------------------
  // Review
  var MS_TYPES = ['conception_candidate', 'decision_with_reason', 'claim_candidate', 'problem_fixed', 'discard_with_reason',
    'maturity_jump', 'ai_origin_element'];
  var EDGE_TYPES = ['derived_from', 'refines', 'modifies', 'rejects', 'responds_to', 'supersedes', 'discards'];

  function question(item) {
    var label = item.label || '';
    if (label.indexOf('milestone:') === 0) {
      var t = label.split(':')[1];
      return 'Is #' + item.target_seq + ' ' + (MS_LONG[t] || t.replace(/_/g, ' ')) + '?';
    }
    if (label.indexOf('edge:') === 0) return 'Is it right that ' + plainLabel(label, item.target_seq) + '?';
    return item.question || label;
  }

  function renderReview() {
    renderClassifier('cls-review');
    var can = !!getSecret();
    $('review-locked').hidden = can;
    var pend = clear($('review-pending'));
    $('review-n').textContent = G.review.length ? '(' + G.review.length + ')' : '';
    if (!G.review.length) pend.appendChild(el('div', { class: 'empty' }, 'Nothing is waiting. The classifier was sure enough about everything, or you have answered it all.'));
    G.review.forEach(function (item) { pend.appendChild(reviewCard(item, can, false)); });
    var autoItems = (G.review_all || []).filter(function (i) { return i.automatic; });
    $('review-auto-n').textContent = '(' + autoItems.length + ')';
    var ab = clear($('review-auto'));
    if (!autoItems.length) ab.appendChild(el('p', { class: 'muted' }, 'Nothing has been counted automatically yet.'));
    autoItems.forEach(function (item) { ab.appendChild(reviewCard(item, can, true)); });
    var done = clear($('review-done'));
    var conf = M.entries.filter(function (e) { return e.event === 'Confirm'; }).reverse();
    if (!conf.length) done.appendChild(el('p', { class: 'muted' }, 'No answers yet.'));
    conf.slice(0, 50).forEach(function (e) {
      var verb = { accept: 'Yes', reject: 'No', edit: 'Changed' }[e.decision] || e.decision;
      done.appendChild(el('div', { class: 'done-row' }, el('span', { class: 'cite' }, '#' + e.seq),
        el('strong', null, verb), plainLabel(e.label, e.target_seq) + (e.edited_label ? ' \u2192 ' + plainLabel(e.edited_label, e.target_seq) : ''),
        el('span', { class: 'muted' }, (e.ts || '').slice(0, 16).replace('T', ' '))));
    });
  }

  function reviewCard(item, can, isAuto) {
    var n = M.node[String(item.target_seq)];
    var card = el('div', { class: 'card q-card', 'data-label': item.label, 'data-target': String(item.target_seq) });
    card.appendChild(el('div', { class: 'q-head' }, el('span', { class: 'q-text' }, question(item)),
      item.score != null ? el('span', { class: 'conf' }, 'classifier ' + pct(item.score) + ' sure') : null,
      isAuto ? el('span', { class: 'lbl auto' }, 'counted automatically') : null));
    if (n) card.appendChild(el('div', { class: 'q-entry' }, cite(n.id), ' ', short(n.label, 220)));
    var parts = item.label.split(':');
    if (parts[0] === 'edge' && M.node[parts[3]]) card.appendChild(el('div', { class: 'q-entry' }, cite(parts[3]), ' ', short(M.node[parts[3]].label, 220)));
    var result = el('span', { class: 'muted', role: 'status' });
    var editSel = el('select', { 'aria-label': 'Change to' });
    if (parts[0] === 'milestone') MS_TYPES.forEach(function (t) { if ('milestone:' + t !== item.label) editSel.appendChild(el('option', { value: 'milestone:' + t }, MS_NAME[t])); });
    else EDGE_TYPES.forEach(function (t) { if (t !== parts[2]) editSel.appendChild(el('option', { value: 'edge:' + parts[1] + ':' + t + ':' + parts[3] }, REL_OUT[t])); });
    var editBox = el('span', { class: 'q-actions', hidden: true }, editSel,
      el('button', { class: 'btn', type: 'button', disabled: !can, onclick: function () { send(item, 'edit', editSel.value, result, card); } }, 'Save change'));
    var actions = el('div', { class: 'q-actions' },
      isAuto ? null : el('button', { class: 'btn yes', type: 'button', disabled: !can, onclick: function () { send(item, 'accept', null, result, card); } }, 'Yes'),
      el('button', { class: 'btn no', type: 'button', disabled: !can, onclick: function () { send(item, 'reject', null, result, card); } }, isAuto ? 'Not right' : 'No'),
      el('button', { class: 'btn', type: 'button', disabled: !can, onclick: function () { editBox.hidden = !editBox.hidden; } }, 'Change…'),
      editBox, result);
    card.appendChild(actions);
    return card;
  }

  function send(item, decision, edited, result, card) {
    var secret = getSecret();
    if (!secret) { result.textContent = 'Answers are disabled on this page.'; return; }
    card.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    result.textContent = 'Recording…';
    fetch('/api/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Authorship-Secret': secret },
      // The page's Referrer-Policy is no-referrer, which would send "Origin: null"; same-origin keeps the real Origin.
      referrerPolicy: 'same-origin',
      cache: 'no-store',
      body: JSON.stringify({ target_seq: item.target_seq, annotation_id: item.annotation_id || null, decision: decision,
        label: item.label, edited_label: decision === 'edit' ? edited : null })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, body: j }; });
    }).then(function (res) {
      if (res.status === 200) { result.textContent = 'Recorded as #' + res.body.seq + '.'; etag = null; setTimeout(refresh, 300); }
      else { result.textContent = 'Not recorded (' + res.status + '): ' + (res.body.error || 'refused'); card.querySelectorAll('button').forEach(function (b) { b.disabled = false; }); }
    }).catch(function () { result.textContent = 'Not recorded: the viewer is not reachable.'; });
  }

  // ------------------------------------------------------------------------------------------
  // Navigation, polling, startup
  function go(view) { S.view = view; writeHash(); show(); }
  function show() {
    VIEWS.forEach(function (v) {
      var on = v === S.view;
      var tab = $('tab-' + v);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
      tab.tabIndex = on ? 0 : -1;
      $('panel-' + v).hidden = !on;
    });
    if (!G) return;
    perf.view = S.view;
    if (S.view === 'overview') renderOverview();
    else if (S.view === 'timeline') renderTimeline();
    else if (S.view === 'map') { if (G.nodes.length > G.limits.stages_only_above) { S.view = 'overview'; return show(); } renderMap(); }
    else if (S.view === 'review') renderReview();
  }
  function syncControls() {
    document.querySelectorAll('#tl-filter button').forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-f') === S.tl ? 'true' : 'false'); });
    if ($('tl-search').value !== S.q) $('tl-search').value = S.q;
  }

  function refresh() {
    var headers = etag ? { 'If-None-Match': etag } : {};
    return fetch('/api/graph', { headers: headers, cache: 'no-store' }).then(function (r) {
      if (r.status === 304) return null;
      if (!r.ok) throw new Error('HTTP ' + r.status);
      etag = r.headers.get('ETag');
      return r.json();
    }).then(function (g) {
      if (!g) return;
      G = g;
      M = buildModel(g);
      renderHeader();
      renderLegend();
      if (cy) cyKey = null;
      show();
    }).catch(function (err) { $('sync').textContent = 'cannot reach the viewer (' + err.message + ')'; });
  }

  function start() {
    readHash();
    syncControls();
    document.querySelectorAll('#tabs [role=tab]').forEach(function (t) {
      t.addEventListener('click', function () { if (!t.disabled) go(t.getAttribute('data-view')); });
      t.addEventListener('keydown', function (ev) {
        var i = VIEWS.indexOf(t.getAttribute('data-view'));
        if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
          ev.preventDefault();
          var j = (i + (ev.key === 'ArrowRight' ? 1 : VIEWS.length - 1)) % VIEWS.length;
          if ($('tab-' + VIEWS[j]).disabled) j = (j + (ev.key === 'ArrowRight' ? 1 : VIEWS.length - 1)) % VIEWS.length;
          go(VIEWS[j]);
          $('tab-' + VIEWS[j]).focus();
        }
      });
    });
    document.querySelectorAll('#tl-filter button').forEach(function (b) {
      b.addEventListener('click', function () { S.tl = b.getAttribute('data-f'); tlShown = TL_PAGE; writeHash(); syncControls(); renderTimeline(); });
    });
    var qt = null;
    $('tl-search').addEventListener('input', function () {
      clearTimeout(qt);
      qt = setTimeout(function () { S.q = $('tl-search').value.trim(); tlShown = TL_PAGE; writeHash(); renderTimeline(); }, 200);
    });
    $('map-focus').addEventListener('change', function () { S.focus = $('map-focus').value; S.sel = null; writeHash(); renderMap(); });
    $('map-slider').addEventListener('input', function () { S.upto = parseInt($('map-slider').value, 10); writeHash(); renderMap(); });
    $('map-play').addEventListener('click', togglePlay);
    $('map-fit').addEventListener('click', function () { if (cy) cy.fit(undefined, 30); });
    $('help-btn').addEventListener('click', function () { var d = $('help'); if (d.showModal) d.showModal(); else d.setAttribute('open', ''); });
    window.addEventListener('hashchange', function () { readHash(); syncControls(); show(); });
    refresh();
    setInterval(refresh, POLL_MS);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
