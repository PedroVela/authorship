/* Authorship record viewer. Plain ES2019, no build step, no network beyond this origin.
 *
 * Three views, in plain language:
 *   What you invented  claims, the elements they rest on, who contributed each
 *   Timeline           what happened, in order, grouped by stage; open a row for its text and code change
 *   Review             labels the classifier was unsure of, for the human to answer
 * State lives in the URL hash (#view=timeline&tl=all&q=...&open=12). The only write is POST /api/confirm.
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
  var TL_PAGE = 400;            // timeline rows rendered per page
  var VIEWS = ['overview', 'timeline', 'review'];
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

  var S = { view: 'overview', tl: 'key', q: '', open: null };
  var G = null, M = null, etag = null, tlShown = TL_PAGE;
  var perf = window.__authorshipPerf = { ready: false, renders: 0, view: null };
  window.__authorship = { model: function () { return M; }, state: S };

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
    S.open = parseInt(p.get('open') || '', 10) || null;
    S.tl = p.get('tl') === 'all' ? 'all' : 'key';
    S.q = p.get('q') || '';
  }
  function writeHash() {
    var p = new URLSearchParams();
    p.set('view', S.view);
    if (S.view === 'timeline' && S.open) p.set('open', String(S.open));
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
    var sg = G.signatures || {};
    if (c.ok) {
      b.className = 'badge ok';
      b.textContent = '✓ Record intact · ' + c.entries + ' entries · ' + (c.sealed_upto ? 'sealed to #' + c.sealed_upto : 'not sealed yet') +
        (sg.signed ? ' · ' + sg.signed + ' of ' + sg.human + ' signed' : '');
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
    b.title = 'updated ' + new Date().toLocaleTimeString();
  }

  // ------------------------------------------------------------------------------------------
  // Who labels the entries: a strip under the tabs only when it needs attention (text going to another provider,
  // labels off or failing); the Help dialog always says it.
  function openDialog(id) { var d = $(id); if (d.showModal) d.showModal(); else d.setAttribute('open', ''); }
  function renderClassifier() {
    var c = G.classifier || {}, host = clear($('cls-strip')), cls = 'cls-strip';
    var how = el('button', { class: 'link-btn', type: 'button', onclick: function () { openDialog('setup'); } }, 'How to change it');
    var msg;
    if (c.state === 'off') { cls += ' off'; msg = [el('strong', null, 'Automatic labels are off.'), ' Only tags you type and the deterministic rules label entries.']; }
    else if (c.state === 'no-backend') { cls += ' err'; msg = [el('strong', null, 'Nothing is labeling entries.'), ' No backend is usable: no Jev or OpenRouter key, and the claude command was not found.']; }
    else if (c.state === 'error' && !c.backend) { cls += ' err'; msg = [el('strong', null, 'The classifier failed.'), ' ' + (c.error || '') + ' It retries on the next change.']; }
    else if (c.third_party) {
      cls += ' third';
      msg = [el('strong', null, 'Labeled automatically by ' + (c.label || c.backend)), c.model ? ' (' + c.model + ')' : '', '. Entry text is sent to ' + c.sends_to + '.'];
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
    host.hidden = !/third|err/.test(cls);  // "off" was the user's own choice: the Help dialog says it
    var hc = clear($('help-classifier'));
    msg.forEach(function (m) { hc.appendChild(typeof m === 'string' ? document.createTextNode(m) : m.cloneNode(true)); });
  }

  // ------------------------------------------------------------------------------------------
  // Overview
  function renderOverview() {
    var inv = G.inventions || [];
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
        el('p', { class: 'muted' }, 'Elements Claude proposed, and replies where it added something you did not ask for. Stated plainly: an honest record carries more weight.'),
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
          entryLink(e.node));
      })));
    } else {
      card.appendChild(el('p', { class: 'muted' }, 'No element is linked to this claim yet. Links appear as the classifier reads the record.'));
    }
    return card;
  }

  // #N, opening that entry in the Timeline
  function entryLink(id) {
    var seq = nodeSeq(id);
    return el('a', { href: '#view=timeline&tl=all&open=' + seq, onclick: function (ev) { ev.preventDefault(); openEntry(seq); } }, cite(id));
  }
  function openEntry(seq) {
    S.open = seq; S.tl = 'all'; S.q = ''; tlShown = Math.max(tlShown, seq + TL_PAGE);
    go('timeline'); syncControls();
    var t = document.querySelector('.tl-item[data-seq="' + seq + '"]');
    if (t) t.scrollIntoView({ block: 'center' });
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
      onclick: function () { S.open = S.open === e.seq ? null : e.seq; writeHash(); renderTimeline(); } },
      el('span', { class: 'tl-seq' }, '#' + e.seq), main,
      el('span', { class: 'tl-labels' }, labels));
    li.appendChild(btn);
    if (S.open === e.seq) { var det = el('div', { class: 'tl-detail' }); li.appendChild(det); fillDetail(det, String(e.seq)); }
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
  // Detail of one entry (an open Timeline row)
  function fillDetail(box, id) {
    clear(box);
    var seq = nodeSeq(id), e = M.entry[seq], n = M.node[id];
    if (!e) { box.appendChild(el('p', { class: 'muted' }, 'Entry #' + id + ' is not in the record.')); return; }
    var body = el('div', { class: 'detail-body' });  // the row above already says who did what, with its labels
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
    var textBox = el('pre', { class: 'text' }, n && n.option != null ? n.label : (e.preview || ''));
    if (!(n && n.option != null) && (e.text_len || 0) > 0) body.appendChild(textBox);
    var rels = el('ul', { class: 'rel' });
    function how(x) {
      return x.source === 'auto' ? el('span', { class: 'muted' }, ' (automatic)') :
        x.source === 'annotation' ? el('span', { class: 'muted' }, ' (suggested; waiting for you in Review)') : null;
    }
    (M.out[id] || []).forEach(function (x) { rels.appendChild(el('li', null, 'This ', REL_OUT[x.type] || x.type, ' ', relLink(x.dst), how(x))); });
    (M.inn[id] || []).forEach(function (x) { rels.appendChild(el('li', null, 'This ', REL_IN[x.type] || x.type, ' ', relLink(x.src), how(x))); });
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
  function relLink(id) {
    var n = M.node[id];
    var label = '#' + id + (n ? ' (' + (n.author === 'human' ? 'you' : 'Claude') + '): ' + short(n.label, 70) : '');
    return el('a', { href: '#view=timeline&tl=all&open=' + nodeSeq(id), onclick: function (ev) { ev.preventDefault(); openEntry(nodeSeq(id)); } }, label);
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
      renderClassifier();
      show();
      perf.ready = true;
      perf.renders++;
    }).catch(function (err) { $('chain-badge').title = 'cannot reach the viewer (' + err.message + ')'; });
  }

  function start() {
    readHash();
    syncControls();
    document.querySelectorAll('#tabs [role=tab]').forEach(function (t) {
      t.addEventListener('click', function () { go(t.getAttribute('data-view')); });
      t.addEventListener('keydown', function (ev) {
        var i = VIEWS.indexOf(t.getAttribute('data-view'));
        if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
          ev.preventDefault();
          var j = (i + (ev.key === 'ArrowRight' ? 1 : VIEWS.length - 1)) % VIEWS.length;
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
    $('help-btn').addEventListener('click', function () { openDialog('help'); });
    $('setup-btn').addEventListener('click', function () { $('help').close(); openDialog('setup'); });
    window.addEventListener('hashchange', function () { readHash(); syncControls(); show(); });
    refresh();
    setInterval(refresh, POLL_MS);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
