/* The trip page: tabs, sections, Discovery, the filter, Costs and the
   currency calculator (YONDER-303: moved out of detail.html so the CSP can be
   script-src 'self'). The templates name these with data-action attributes;
   app/review/static/rundere.js dispatches them. The yn* functions stay global
   because one partial calls another's (ynDisc from ynTab, ynCity from the
   Discovery init). */
(function () {
  "use strict";
  // Costs: Plan | Actual (YONDER-221). The section's data-sub picks the panel and
  // the matching Add button; the server sets it too, so a refresh keeps it.
  function ynCostsSub(sub) {
    var sec = document.getElementById('costs-section');
    if (sec) sec.setAttribute('data-sub', sub);
  }
  // Currency pickers (YONDER-213) list only this trip's currencies; picking
  // "Other currency…" swaps in every other one from the <template> beside it.
  function ynMoreCurrencies(sel) {
    if (sel.value !== '__more') return;
    var tpl = document.getElementById(sel.id + '-all');
    sel.querySelector('option[value="__more"]').remove();
    if (tpl) {
      var group = document.createElement('optgroup');
      group.label = 'Other currencies';
      group.appendChild(tpl.content.cloneNode(true));
      sel.appendChild(group);
    }
    sel.selectedIndex = -1;
    sel.focus();
    if (sel.showPicker) { try { sel.showPicker(); } catch (e) {} }
  }
  // Collapse one section (the caret on its header).
  function ynSec(btn) {
    var s = btn.closest('.yn-sec');
    if (s) s.classList.toggle('collapsed');
  }
  // Per-section "Expand/Collapse all" for its sub-groups; also un-collapses the
  // section so the result is visible.
  function ynToggleAll(grp, open) {
    var host = document.querySelector('[data-grp="' + grp + '"]');
    var sec = (host && host.closest('.yn-sec')) || document;
    sec.querySelectorAll('details.bp-group[data-grp="' + grp + '"], details.yn-finding')
      .forEach(function (d) { d.open = open; });
    if (sec.classList && open) sec.classList.remove('collapsed');
  }
  // Expand/collapse every group/finding in the visible tab (all tabs while a
  // filter is active, since matches can span tabs).
  function ynAll(open) {
    var scope = document.body.classList.contains('yn-filtering')
      ? '.yn-tabpanel' : '.yn-tabpanel.active';
    document.querySelectorAll(scope + ' details.bp-group, ' + scope + ' details.yn-finding')
      .forEach(function (d) { d.open = open; });
  }
  // Section tabs. The active tab lives in the URL hash (#timeline), so reloads,
  // HTMX-driven redirects and shared links land on the same tab; the old
  // #sec-* jump anchors map onto their tab.
  function ynTab(name) {
    // "discovery/food" = the Discovery tab, open on its Food & Drink section.
    var parts = (name || '').split('/'), sub = parts[1] || '';
    name = parts[0];
    var tabs = document.querySelectorAll('.yn-tab');
    var known = Array.prototype.some.call(tabs, function (t) { return t.dataset.tab === name; });
    if (!known) name = 'timeline';
    tabs.forEach(function (t) {
      var on = t.dataset.tab === name;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.yn-tabpanel').forEach(function (p) {
      p.classList.toggle('active', p.dataset.tab === name);
    });
    if (name === 'discovery') { ynDisc(sub || ynDiscCurrent()); return; }
    if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  }
  // Discovery sections (4.5): one visible at a time; the rail marks the open one.
  function ynDiscCurrent() {
    var on = document.querySelector('.yn-disc-link.active');
    if (on) return on.dataset.sec;
    var h = location.hash.split('/');
    if (h[0] === '#discovery' && h[1]) return h[1];
    var first = document.querySelector('.yn-disc-link:not(.is-empty)');
    return first ? first.dataset.sec : 'weather';
  }
  function ynDisc(sec) {
    var links = document.querySelectorAll('.yn-disc-link');
    if (!links.length) return;
    var known = Array.prototype.some.call(links, function (l) { return l.dataset.sec === sec; });
    if (!known) sec = links[0].dataset.sec;
    links.forEach(function (l) {
      var on = l.dataset.sec === sec;
      l.classList.toggle('active', on);
      l.setAttribute('aria-selected', on ? 'true' : 'false');
      l.tabIndex = on ? 0 : -1;
    });
    document.querySelectorAll('.yn-dsec').forEach(function (s) {
      s.classList.toggle('active', s.dataset.sec === sec);
    });
    var tab = document.querySelector('.yn-tabpanel[data-tab="discovery"]');
    // Keep a deeper path (#discovery/food/JP:Kyoto) when the section is already right.
    if (tab && tab.classList.contains('active')
        && location.hash.split('/').slice(0, 2).join('/') !== '#discovery/' + sec)
      history.replaceState(null, '', '#discovery/' + sec);
  }
  // After an HTMX refresh of the Discovery tab, reopen the same section + city.
  function ynDiscInit() {
    var h = location.hash.split('/');
    ynDisc(h[0] === '#discovery' && h[1] ? h[1] : ynDiscCurrent());
    if (h[0] === '#discovery' && h[1] && h[2]) ynCity(h[1], decodeURIComponent(h[2]));
    else document.querySelectorAll('.yn-dsec').forEach(function (s) {
      var c = s.querySelector('.yn-city'); if (c) ynCity(s.dataset.sec, c.dataset.city, true);
    });
  }
  // City chips inside Food / Events: one city at a time, kept in the URL.
  function ynCity(sec, city, quiet) {
    var panel = document.querySelector('.yn-dsec[data-sec="' + sec + '"]');
    if (!panel) return;
    var found = false;
    panel.querySelectorAll('.yn-city').forEach(function (c) {
      var on = c.dataset.city === city; c.classList.toggle('active', on); found = found || on;
    });
    if (!found) { var first = panel.querySelector('.yn-city'); if (first) first.classList.add('active'); }
    panel.querySelectorAll('.yn-city-chip').forEach(function (b) {
      var on = b.dataset.city === city || (!found && b === panel.querySelector('.yn-city-chip'));
      b.classList.toggle('active', on); b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (!quiet && found) history.replaceState(null, '', '#discovery/' + sec + '/' + city);
  }
  // Flip a city between its tables and its sources.
  function ynSources(btn) {
    var city = btn.closest('.yn-city'), on = btn.getAttribute('aria-pressed') !== 'true';
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    city.querySelector('.yn-city-main').hidden = on;
    city.querySelector('.yn-city-sources').hidden = !on;
    btn.textContent = on ? 'Back to the list' : btn.dataset.label;
  }
  function ynShowHidden(btn) {
    var main = btn.closest('.yn-city-main'), on = !main.classList.contains('show-hidden');
    main.classList.toggle('show-hidden', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
  // Arrow keys move along the rail (tab pattern).
  document.addEventListener('keydown', function (e) {
    if (!e.target.classList || !e.target.classList.contains('yn-disc-link')) return;
    var dir = {ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1}[e.key];
    if (!dir) return;
    var links = Array.prototype.slice.call(document.querySelectorAll('.yn-disc-link'));
    var i = links.indexOf(e.target) + dir;
    var next = links[(i + links.length) % links.length];
    e.preventDefault(); ynDisc(next.dataset.sec); next.focus();
  });
  (function () {
    var h = location.hash.replace(/^#(sec-)?/, '');
    ynTab(h === 'travelers' ? 'travellers' : h);
    ynDiscInit();          // open section + city (the partial's own call ran before these existed)
  })();
  // Live filter: hide anything (a city finding, a day, a packing row, a traveller)
  // that doesn't contain the query; reveal + open matches; hide now-empty groups.
  function ynFilter(q) {
    q = (q || '').trim().toLowerCase();
    document.body.classList.toggle('yn-filtering', !!q);
    document.querySelectorAll('.yn-filterable').forEach(function (el) {
      var hit = !q || el.textContent.toLowerCase().indexOf(q) !== -1;
      el.classList.toggle('yn-hidden', !hit);
      if (hit && q) {
        var d = el.closest('details'); if (d) d.open = true;
        var s = el.closest('.yn-sec'); if (s) s.classList.remove('collapsed');
      }
    });
    document.querySelectorAll('details.bp-group, .yn-sec').forEach(function (c) {
      if (c.classList.contains('yn-filterable')) return;
      var vis = Array.prototype.some.call(
        c.querySelectorAll('.yn-filterable'),
        function (el) { return !el.classList.contains('yn-hidden'); });
      c.classList.toggle('yn-hidden', !!q && !vis);
    });
  }
  // Re-apply an active filter after an HTMX swap re-renders a section.
  document.body.addEventListener('htmx:afterSettle', function () {
    var box = document.querySelector('.yn-jump-filter');
    if (box && box.value) ynFilter(box.value);
  });

  // Expose what the partials' data-actions and the inits call by name.
  window.ynTab = ynTab; window.ynDisc = ynDisc; window.ynDiscInit = ynDiscInit;
  window.ynCity = ynCity; window.ynFilter = ynFilter;

  // "1 CAD = ? USD", and no rate at all when the expense is in the trip's own
  // currency (or the trip has none yet: the first expense sets it). YONDER-220.
  function ynRateLabel() {
    var f = document.getElementById("ex-rate-field");
    var cur = document.getElementById("ex-cur");
    if (!f || !cur) return;
    var trip = f.getAttribute("data-trip-cur"), c = cur.value;
    var need = trip && c && c !== "__more" && c !== trip;
    f.hidden = !need;
    if (need) document.getElementById("ex-rate-label").textContent =
      "Exchange rate · 1 " + c + " = ? " + trip;
  }

  // The currency calculator modal: rates come from the data-rates beside it.
  function calculator(rates) {
    var perPivot = JSON.parse(rates.dataset.rates || "{}");
    var amt = document.getElementById("yn-cc-amt");
    var from = document.getElementById("yn-cc-from");
    var to = document.getElementById("yn-cc-to");
    var out = document.getElementById("yn-cc-out");
    function conv() {
      var a = parseFloat(amt.value); if (isNaN(a)) a = 0;
      var f = perPivot[from.value], t = perPivot[to.value];
      if (!f || !t) { out.textContent = "—"; return; }
      var r = a / f * t;
      out.textContent = r.toLocaleString(undefined, {maximumFractionDigits: 2}) + " " + to.value;
    }
    [amt, from, to].forEach(function (el) {
      el.addEventListener("input", conv); el.addEventListener("change", conv);
    });
    document.getElementById("yn-cc-swap").addEventListener("click", function () {
      var tmp = from.value; from.value = to.value; to.value = tmp; conv();
    });
    conv();
  }

  var R = window.Rundere;
  R.action("tab", function (el) { ynTab(el.dataset.tab); });
  R.action("sec", function (el) { ynSec(el); });
  R.action("all-open", function () { ynAll(true); });
  R.action("all-close", function () { ynAll(false); });
  R.action("group-open", function (el) { ynToggleAll(el.dataset.group, true); });
  R.action("group-close", function (el) { ynToggleAll(el.dataset.group, false); });
  R.action("filter", function (el) { ynFilter(el.value); });
  R.action("costs-sub", function (el) { ynCostsSub(el.dataset.costsSub); });
  R.action("more-currencies", function (el) { ynMoreCurrencies(el); });
  R.action("rate-label", ynRateLabel);
  R.action("disc", function (el) { ynDisc(el.dataset.sec); });
  R.action("city", function (el) { ynCity(el.dataset.disc, el.dataset.city); });
  R.action("sources", function (el) { ynSources(el); });
  R.action("show-hidden", function (el) { ynShowHidden(el); });
  // After an HTMX refresh of the Discovery tab, reopen the same section + city.
  R.onLoad("#discovery-section", ynDiscInit);
  R.onLoad("#ex-rate-field", ynRateLabel);
  R.onLoad("#yn-cc-rates", calculator);
})();
