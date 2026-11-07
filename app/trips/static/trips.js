/* Behaviours every trips page shares (YONDER-303: a static file, so the CSP
   can be script-src 'self'). */
(function () {
  "use strict";
  // Start → end: setting a start fills an empty end with the same date (and time),
  // and pulls an end that is now before the start up to it. Works for timeline
  // activities (when/endWhen) and trips (start/end). Delegated, so it
  // also covers modals HTMX swaps in later.
  document.addEventListener("change", function (e) {
    var pairs = {when: "endWhen", start: "end"};
    var el = e.target, endName = el && pairs[el.name];
    if (!endName || !el.form || !el.value) return;
    var end = el.form.querySelector('[name="' + endName + '"]');
    if (!end || end.type !== el.type) return;
    if (!end.value || end.value < el.value) end.value = el.value;
    end.min = el.value;
  });

  // Travel profile: offer the browser's own zone in one click (it's only a
  // suggestion until saved).
  window.Rundere.onLoad("#tp-tz-detect", function (btn) {
    var z; try { z = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return; }
    var sel = document.getElementById("tp-tz");
    if (!z || !sel || sel.value === z) return;
    var opt = [].find.call(sel.options, function (o) { return o.value === z; });
    if (!opt) return;
    btn.textContent = "Use this device's: " + opt.textContent;
    btn.hidden = false;
    btn.addEventListener("click", function () { sel.value = z; btn.hidden = true; });
  });
})();
