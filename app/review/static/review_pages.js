/* Review-side pages (YONDER-303): the code their templates used to carry
   inline. Each part runs only where its elements are, through
   Rundere.onLoad / Rundere.action (app/review/static/rundere.js). */
(function () {
  "use strict";
  var R = window.Rundere;

  // Inbox: a capture's strip opens and closes its detail row below it.
  R.action("strip-toggle", function (el) {
    el.classList.toggle("open");
    var d = document.getElementById(el.dataset.detail);
    if (d) d.hidden = !d.hidden;
  });

  // [data-copy="id"] buttons copy that element's text (capture_setup.html).
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-copy]"); if (!b) return;
    var el = document.getElementById(b.getAttribute("data-copy")); if (!el) return;
    var done = function () { var t = b.textContent; b.textContent = "Copied"; setTimeout(function () { b.textContent = t; }, 1500); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(el.textContent).then(done, function () {});
    }
  });

  // _about_body.html: plays the recorded pronunciation (design/audio: AAC for
  // Safari/Chrome/Edge, Opus for browsers without AAC). If neither plays, the
  // browser's voice says it.
  R.onLoad("#rd-say", function (b) {
    var a = document.getElementById("rd-say-audio");
    var canSpeak = "speechSynthesis" in window;
    var canPlay = !!(a && a.canPlayType &&
      (a.canPlayType("audio/mp4") || a.canPlayType("audio/webm; codecs=opus")));
    if (!(canPlay || canSpeak)) return;
    function speak() {
      if (!canSpeak) return;
      var u = new SpeechSynthesisUtterance("Run there");
      u.lang = "en-US"; u.rate = 0.9;
      window.speechSynthesis.cancel(); window.speechSynthesis.speak(u);
    }
    b.hidden = false;
    b.addEventListener("click", function () {
      if (!canPlay) return speak();
      a.currentTime = 0;
      var p = a.play();
      if (p && p.catch) p.catch(speak);
    });
    if (a) a.addEventListener("error", function () { canPlay = false; });
  });

  // _capture_sheet.html: Escape closes the capture sheet; focus its content.
  R.onLoad("#capsheet", function (s) {
    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape" && document.getElementById("capsheet")) {
        s.remove(); document.removeEventListener("keydown", esc);
      }
    });
    var t = document.getElementById("capsheet-content"); if (t) t.focus();
  });

  // _home_lookup_result.html: fill the home form from the lookup.
  R.onLoad(".hp-found", function (block) {
    var found = JSON.parse(block.dataset.found || "{}");
    function set(id, v) {
      var e = document.getElementById(id);
      if (e && v !== null && v !== undefined && v !== "") e.value = v;
    }
    set("hp-lat", found.lat);
    set("hp-lon", found.lon);
    set("hp-country", found.country);
    set("hp-iata", found.iata);
    var lbl = document.getElementById("hp-label");
    if (lbl && !lbl.value) lbl.value = found.label;
  });

  // _journal_panel.html: the zone a note is written in (D-224).
  R.onLoad("#jnl-tz", function (el) {
    try { el.value = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}
  });

  // _settings_nav.html. Phones: the sidebar is a horizontal strip; bring the
  // current page into view (scrolls the strip only, never the page).
  R.onLoad(".yn-set-nav", function (n) {
    var a = n.querySelector("a.on");
    if (a && n.scrollWidth > n.clientWidth) n.scrollLeft = a.offsetLeft - n.offsetLeft - 8;
  });

  // capture.html: Capture | Recent captures. #recent opens the second tab, so it
  // can be linked to.
  R.onLoad(".cap-ltabs", function (nav) {
    var tabs = nav.querySelectorAll(".cap-tab"),
        panes = document.querySelectorAll(".cap-left .cap-pane");
    function show(name) {
      tabs.forEach(function (t) {
        var on = t.dataset.pane === name;
        t.classList.toggle("on", on); t.setAttribute("aria-selected", on ? "true" : "false");
      });
      panes.forEach(function (p) { p.hidden = p.dataset.pane !== name; });
    }
    tabs.forEach(function (t) {
      t.addEventListener("click", function () {
        show(t.dataset.pane);
        history.replaceState(null, "", t.dataset.pane === "recent" ? "#recent" : location.pathname);
      });
    });
    if (location.hash === "#recent") show("recent");
  });

  // capture.html: a directions link can take a travel date (a route carries
  // none): show those fields while the box holds one. The date is optional
  // (D-210): without it the route waits in the Inbox.
  R.onLoad("#dir-fields", function (dir) {
    var box = document.getElementById("content"), date = document.getElementById("travel_date");
    var isDirections = function (v) {
      v = (v || "").trim();
      return /^https?:\/\/(www\.)?google\.[a-z.]+\/maps\/dir\//i.test(v) ||
             /^https?:\/\/maps\.apple\.com\/.*(saddr|daddr)=/i.test(v);
    };
    // A Google share link (maps.app.goo.gl) is a place OR directions; only the
    // server can tell after expanding it, so show the date fields but never
    // require them (YONDER-200).
    var isShort = function (v) {
      v = (v || "").trim();
      return /^https?:\/\/maps\.app\.goo\.gl\//i.test(v) || /^https?:\/\/goo\.gl\/maps\//i.test(v);
    };
    function sync() {
      var on = isDirections(box.value), maybe = isShort(box.value);
      dir.hidden = !(on || maybe);
      document.getElementById("dir-hint").textContent = maybe
        ? "If this is a directions link, add a travel date if you know it. Without one it waits in your Inbox for a date. A place link needs nothing here: it is saved to your places."
        : "This looks like a directions link. Add a travel date if you know it; without one it waits in your Inbox for a date.";
    }
    box.addEventListener("input", sync); sync();

    // Timezone: default to this browser's own zone and offer the full list.
    try {
      var tz = document.getElementById("timezone");
      tz.value = tz.value || Intl.DateTimeFormat().resolvedOptions().timeZone || "";
      var list = document.getElementById("tz-list");
      (Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : []).forEach(function (z) {
        var o = document.createElement("option"); o.value = z; list.appendChild(o);
      });
    } catch (e) { /* a plain text box still works */ }

    // Picking a trip pre-fills an empty travel date with the trip's start.
    var trip = document.getElementById("trip_id");
    var starts = JSON.parse(document.getElementById("cap-trip-starts").textContent || "[]");
    trip.addEventListener("change", function () {
      var s = starts[trip.selectedIndex - 1];
      if (s && !date.value) date.value = s;
    });
    date.addEventListener("change", function () {
      var end = document.getElementById("end_date");
      end.min = date.value; if (end.value && end.value < date.value) end.value = date.value;
    });
  });

  // capture_handoff.html: the bookmarklet's tab sends the page here.
  R.onLoad("#hand-form", function (form) {
    var MAX = parseInt(form.dataset.maxBytes, 10) || 0,
        status = document.getElementById("hand-status"), got = false;
    window.addEventListener("message", function (e) {
      // Only the tab that opened this one, and only our message shape. The page
      // HTML is untrusted data: it is put in a hidden field and posted, never
      // inserted into this page.
      if (e.source !== window.opener || got) return;
      var d = e.data;
      if (!d || d.type !== "rundere-page" || typeof d.html !== "string") return;
      if (d.html.length > MAX) { status.textContent = "That page is too large to capture."; got = true; return; }
      got = true;
      document.getElementById("hand-html").value = d.html;
      document.getElementById("hand-url").value = typeof d.url === "string" ? d.url.slice(0, 2000) : "";
      var from = "";
      try { from = new URL(e.origin).host; } catch (err) { from = e.origin; }
      document.getElementById("hand-from").textContent = from || "an unknown page";
      status.textContent = "Got the page.";
      document.getElementById("hand-confirm").hidden = false;
      document.getElementById("hand-go").focus();
    });
    document.getElementById("hand-go").addEventListener("click", function () {
      document.getElementById("hand-confirm").hidden = true;
      status.textContent = "Capturing…";
      window.htmx.trigger(form, "submit");
    });
    if (window.opener) { window.opener.postMessage({ type: "rundere-ready" }, "*"); }
    setTimeout(function () {
      if (!got) { status.hidden = true; document.getElementById("hand-none").hidden = false; }
    }, 15000);
  });

  // dashboard.html: no time zone on the travel profile, and the server may run
  // on UTC: greet by the browser's clock instead (YONDER-218/219).
  R.onLoad("#dash-greeting[data-browser-clock]", function (el) {
    var h = new Date().getHours();
    var part = h < 12 ? "morning" : (h < 18 ? "afternoon" : "evening");
    var name = el.getAttribute("data-name");
    el.textContent = "Good " + part + (name ? ", " + name : "");
  });

  // places/_place_sheet.html: Escape closes the place sheet; focus it on open.
  R.onLoad("#pl-sheet-backdrop", function () {
    var host = document.getElementById("pl-sheet");
    if (!host) return;
    function esc(e) { if (e.key === "Escape") { host.innerHTML = ""; document.removeEventListener("keydown", esc); } }
    document.addEventListener("keydown", esc);
    var s = host.querySelector(".rd-sheet"); if (s && !host.contains(document.activeElement)) s.focus();
  });

  // places/index.html: "/" searches; j and k move between places; Enter opens
  // one (SPEC-11).
  document.addEventListener("keydown", function (e) {
    if (!document.getElementById("pl-q")) return;
    var t = e.target, typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
                                     || t.tagName === "SELECT" || t.isContentEditable);
    if (document.getElementById("pl-sheet-backdrop")) return;
    if (e.key === "/" && !typing) { e.preventDefault(); document.getElementById("pl-q").focus(); return; }
    if (typing || (e.key !== "j" && e.key !== "k")) return;
    var rows = Array.prototype.slice.call(document.querySelectorAll(".pl-row"));
    if (!rows.length) return;
    var i = rows.indexOf(document.activeElement);
    i = e.key === "j" ? Math.min(i + 1, rows.length - 1) : Math.max(i - 1, 0);
    rows[i].focus(); e.preventDefault();
  });
})();
