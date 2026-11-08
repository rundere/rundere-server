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

/* A native <dialog> inside a page's form (the Discovery profile's cuisines,
   YONDER-328): [data-open-dialog=id] opens it, [data-close-dialog] closes it.
   Closing refreshes the page's summary of what was picked; the form's own
   Save keeps it. */
(function () {
  "use strict";
  document.addEventListener("click", function (e) {
    var open = e.target.closest && e.target.closest("[data-open-dialog]");
    if (open) {
      var d = document.getElementById(open.getAttribute("data-open-dialog"));
      if (d && d.showModal) d.showModal();
      return;
    }
    var close = e.target.closest && e.target.closest("[data-close-dialog]");
    if (close) { var dlg = close.closest("dialog"); if (dlg) dlg.close(); }
  });

  function picked(dlg, value) {
    return [].map.call(dlg.querySelectorAll('input[type=radio][value="' + value + '"]:checked'),
                       function (i) { return i.getAttribute("data-label"); });
  }
  function line(el, title, names) {
    el.textContent = "";
    if (!names.length) return;
    var b = document.createElement("b"); b.textContent = title + " ";
    el.appendChild(b); el.appendChild(document.createTextNode(names.join(", ")));
  }
  document.addEventListener("close", function (e) {
    var dlg = e.target;
    if (!dlg || dlg.id !== "cuisine-dialog") return;
    var box = document.querySelector("[data-cuisine-summary]");
    if (!box) return;
    var likes = picked(dlg, "like"), dislikes = picked(dlg, "dislike");
    line(box.querySelector("[data-cuisine-likes]"), "Likes:", likes);
    line(box.querySelector("[data-cuisine-dislikes]"), "Doesn't like:", dislikes);
    box.querySelector("[data-cuisine-none]").hidden = !!(likes.length || dislikes.length);
  }, true);                                  // "close" doesn't bubble: capture it
})();
