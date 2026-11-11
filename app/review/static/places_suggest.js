// Places search suggestions and kind chips (SPEC-11, D-235). As you type, the word
// under the caret is completed from /places/suggest: the chips before a colon, the
// kinds after kind:, your own lists after list:, your cities and countries after
// city: and country:. ↑/↓ move, Enter or Tab picks, Esc closes; a click picks too.
// The chips under the box toggle kind:<kind>. Picking edits the box and fires
// `input`, so the htmx search (index.html) runs as if it was typed. Suggestions are
// drawn with textContent only: a list or city name is never treated as HTML.
(function () {
  'use strict';

  var input, list, status, form;
  var items = [], span = null, active = -1, timer = null, controller = null;
  // While a chip's value is being typed (`city:l`), the search waits: searching for a
  // city called "l" would empty the results, or the map, under the person mid-word.
  // It runs when they pick, type a space, press Enter or leave the box.
  var holding = false;

  function chipBeingTyped() {
    var caret = input.selectionStart == null ? input.value.length : input.selectionStart;
    var before = input.value.slice(0, caret);
    var next = input.value.charAt(caret);              // '' at the end of the box
    return /(^|\s)(kind|list|city|country):("[^"]*|\S*)$/i.test(before) &&
           (next === '' || /\s/.test(next));
  }

  function release() {
    if (!holding) return;
    holding = false;
    search();
  }

  function setActive(i) {
    active = i;
    var opts = list.querySelectorAll('[role=option]');
    for (var j = 0; j < opts.length; j++) {
      opts[j].setAttribute('aria-selected', j === i ? 'true' : 'false');
    }
    if (i >= 0 && opts[i]) {
      input.setAttribute('aria-activedescendant', opts[i].id);
      opts[i].scrollIntoView({ block: 'nearest' });
    } else {
      input.removeAttribute('aria-activedescendant');
    }
  }

  function close() {
    items = []; span = null;
    list.hidden = true;
    list.textContent = '';
    input.setAttribute('aria-expanded', 'false');
    setActive(-1);
  }

  function render(data) {
    items = data.items || [];
    span = { start: data.start, end: data.end };
    list.textContent = '';
    if (!items.length) { close(); return; }
    items.forEach(function (it, i) {
      var li = document.createElement('li');
      li.id = 'pl-sg-' + i;
      li.setAttribute('role', 'option');
      li.className = 'pl-sg';
      var label = document.createElement('span');
      label.className = 'pl-sg-label';
      label.textContent = it.label;
      var hint = document.createElement('span');
      hint.className = 'pl-sg-insert';
      hint.textContent = it.insert;
      li.appendChild(label);
      li.appendChild(hint);
      if (typeof it.count === 'number') {
        var n = document.createElement('span');
        n.className = 'pl-sg-count';
        n.textContent = String(it.count);
        n.setAttribute('aria-label', it.count + (it.count === 1 ? ' place' : ' places'));
        li.appendChild(n);
      }
      // mousedown, not click: keep focus in the box so the caret stays put.
      li.addEventListener('mousedown', function (e) { e.preventDefault(); pick(i); });
      list.appendChild(li);
    });
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    setActive(-1);
    status.textContent = items.length + (items.length === 1 ? ' suggestion' : ' suggestions') +
      '. Use the arrow keys to choose.';
  }

  function fetchNow() {
    if (controller) controller.abort();
    var mine = controller = new AbortController();
    var url = '/places/suggest?q=' + encodeURIComponent(input.value) +
              '&cursor=' + (input.selectionStart == null ? input.value.length : input.selectionStart);
    fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' },
                 signal: mine.signal })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (data) {
        if (mine !== controller || document.activeElement !== input) return;
        render(data || {});
      })
      .catch(function () { /* aborted or offline: the box still searches */ });
  }

  function update() {
    clearTimeout(timer);
    timer = setTimeout(fetchNow, 120);
  }

  function search() {
    // The htmx trigger listens for `input` on the box (index.html).
    input.dispatchEvent(new Event('input', { bubbles: true }));
    syncKinds();
  }

  function pick(i) {
    var it = items[i];
    if (!it || !span) return;
    holding = !it.complete;              // `kind:` still wants its value
    var v = input.value;
    var before = v.slice(0, span.start), after = v.slice(span.end);
    var add = it.insert + (it.complete && !/^\s/.test(after) ? ' ' : '');
    input.value = before + add + after;
    var caret = (before + add).length;
    input.setSelectionRange(caret, caret);
    close();
    search();
    if (!it.complete) fetchNow();      // kind: wants its value: offer the kinds now
  }

  // ------------------------------------------------------------------ kind chips

  var KIND = /(^|\s)kind:("[^"]*"?|\S*)/gi;

  function currentKind() {
    var m, last = '';
    KIND.lastIndex = 0;
    while ((m = KIND.exec(input.value))) last = m[2].replace(/"/g, '').toLowerCase();
    return last;
  }

  function syncKinds() {
    var k = currentKind();
    document.querySelectorAll('.pl-kind').forEach(function (b) {
      b.setAttribute('aria-pressed', b.dataset.kind === k ? 'true' : 'false');
    });
  }

  function toggleKind(word) {
    var on = currentKind() === word;
    var v = input.value.replace(KIND, ' ').replace(/\s+/g, ' ').trim();
    if (!on) v = (v ? v + ' ' : '') + 'kind:' + word;
    input.value = v;
    holding = false;
    close();
    search();
  }

  // ------------------------------------------------------------------ wiring

  function init() {
    input = document.getElementById('pl-q');
    list = document.getElementById('pl-suggest');
    status = document.getElementById('pl-suggest-status');
    form = document.getElementById('pl-search');
    if (!input || !list || input.dataset.suggest) return;
    input.dataset.suggest = '1';

    input.addEventListener('input', function (e) {
      if (e.isTrusted) { holding = chipBeingTyped(); update(); }
      syncKinds();
    });
    // htmx asks before each search; a held one is skipped (see `holding`).
    form.addEventListener('htmx:beforeRequest', function (e) {
      if (holding) e.preventDefault();
    });
    input.addEventListener('focus', function () { if (!input.value.trim()) update(); });
    input.addEventListener('click', update);           // the caret moved to another word
    input.addEventListener('blur', function () { setTimeout(function () { close(); release(); }, 120); });
    input.addEventListener('keydown', function (e) {
      var open = !list.hidden && items.length;
      if (e.key === 'ArrowDown') {
        if (!open) { fetchNow(); e.preventDefault(); return; }
        setActive((active + 1) % items.length); e.preventDefault();
      } else if (e.key === 'ArrowUp' && open) {
        setActive(active <= 0 ? items.length - 1 : active - 1); e.preventDefault();
      } else if ((e.key === 'Enter' || e.key === 'Tab') && open && active >= 0) {
        pick(active); e.preventDefault();
      } else if (e.key === 'Tab' && open && items.length === 1 && !e.shiftKey) {
        pick(0); e.preventDefault();
      } else if (e.key === 'Enter') {
        holding = false;                                    // the form's submit searches
        close();
      } else if (e.key === 'Escape' && open) {
        close(); release(); e.preventDefault(); e.stopPropagation();   // don't clear the box
      } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'Home' ||
                 e.key === 'End') {
        setTimeout(update, 0);                              // the caret moves word
      }
    });

    document.querySelectorAll('.pl-kind').forEach(function (b) {
      b.addEventListener('click', function () { toggleKind(b.dataset.kind); });
    });
    syncKinds();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
