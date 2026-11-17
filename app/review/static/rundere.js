/* Small behaviours the pages need, as plain listeners (YONDER-293): htmx's
   eval features (hx-on, hx-vals "js:", trigger filters) are switched off so the
   Content-Security-Policy needs no 'unsafe-eval'.

   Since YONDER-303 the CSP is script-src 'self': no inline <script> blocks and
   no on*= attributes (tests/test_csp.py fails on either). Pages say what they
   need with data- attributes instead, handled here by one delegated listener
   per event type, so they also work in anything htmx swaps in later:

     data-action="name"         click runs Rundere.action("name")'s handler
     data-action-change="name"  the same for change; -input for input. Several
                                space-separated names run in order
     data-confirm="Question?"   a form (or button) asks first; Cancel stops it
     data-no-submit             a form that never submits (live search boxes)
     data-no-click              a link that does nothing when clicked (bookmarklets)
     data-submit-on-change      a select that submits its form when changed
     data-mark-manual           a select remembers it was picked by hand
     data-enter-commits         Enter in an input fires change instead of submitting
     data-remove-on-backdrop    a click on the element itself (not its content) removes it
     data-dismiss="#sel"        a click removes #sel
     data-clear="#sel"          a click empties #sel; with data-backdrop, only a click
                                on the element itself
     data-remove-on-error       an <img> that fails to load is removed

   Code for content htmx swaps in goes in Rundere.onLoad(selector, fn): it runs
   for matching elements in the first page and in every swap. htmx drops every
   <script> from swapped content (allowScriptTags is off), JSON blocks too, so a
   partial passes data in a single-quoted attribute: data-x='{{ value|tojson }}'. */
(function () {
  "use strict";
  var actions = {}, loaders = [];

  window.Rundere = {
    // Register a handler for data-action / data-action-change / -input.
    action: function (name, fn) { actions[name] = fn; },
    // fn(el) for every element matching selector, now and after each swap.
    onLoad: function (selector, fn) { loaders.push([selector, fn]); }
  };

  function run(kind, e) {
    var attr = kind === "click" ? "data-action" : "data-action-" + kind;
    var el = e.target.closest && e.target.closest("[" + attr + "]");
    if (!el) return;
    // Several names run in order: data-action-change="more-currencies rate-label".
    el.getAttribute(attr).split(/\s+/).forEach(function (name) {
      var fn = actions[name];
      if (fn) fn(el, e);
    });
  }
  ["click", "change", "input"].forEach(function (kind) {
    document.addEventListener(kind, function (e) { run(kind, e); });
  });

  function load(root) {
    if (!root || !root.querySelectorAll) return;
    loaders.forEach(function (l) {
      if (root.matches && root.matches(l[0])) l[1](root);
      root.querySelectorAll(l[0]).forEach(function (el) { l[1](el); });
    });
  }
  // htmx fires htmx:load for the first page and for every swapped-in element.
  // The sign-in pages (authcard.html) have no htmx: run once when the page is read.
  document.addEventListener("htmx:load", function (e) { load(e.detail && e.detail.elt); });
  document.addEventListener("DOMContentLoaded", function () {
    if (!window.htmx) load(document.body);
  });

  // Capture phase, so a Cancel stops the submit before htmx's own listener on
  // the form sees it.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.hasAttribute("data-no-submit")) { e.preventDefault(); return; }
    var q = form.getAttribute("data-confirm");
    if (q && !window.confirm(q)) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);

  document.addEventListener("click", function (e) {
    var t = e.target, el;
    if (!t.closest) return;
    if ((el = t.closest("button[data-confirm]")) && !el.form) {
      if (!window.confirm(el.getAttribute("data-confirm"))) {
        e.preventDefault(); e.stopImmediatePropagation(); return;
      }
    }
    if (t.closest("[data-no-click]")) { e.preventDefault(); return; }
    if (t.hasAttribute("data-remove-on-backdrop")) { t.remove(); return; }
    if ((el = t.closest("[data-dismiss]"))) {
      var gone = document.querySelector(el.getAttribute("data-dismiss"));
      if (gone) gone.remove();
      return;
    }
    if ((el = t.closest("[data-clear]")) && (!el.hasAttribute("data-backdrop") || el === t)) {
      var box = document.querySelector(el.getAttribute("data-clear"));
      if (box) box.innerHTML = "";
    }
  }, true);

  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t.hasAttribute && t.hasAttribute("data-mark-manual")) t.dataset.manual = "1";
    if (t.hasAttribute && t.hasAttribute("data-submit-on-change") && t.form) t.form.submit();
  });

  document.addEventListener("keydown", function (e) {
    var t = e.target;
    if (e.key === "Enter" && t.hasAttribute && t.hasAttribute("data-enter-commits")) {
      e.preventDefault();
      t.dispatchEvent(new Event("change", {bubbles: true}));
    }
  });

  // error doesn't bubble, so listen in the capture phase.
  document.addEventListener("error", function (e) {
    var t = e.target;
    if (t.tagName === "IMG" && t.hasAttribute("data-remove-on-error")) t.remove();
  }, true);

  // Sheets (review pages): Escape or a click on the backdrop closes the one in
  // #modal-root, and it takes focus when it opens, for keyboard and
  // screen-reader users.
  function closeSheet() { var r = document.getElementById("modal-root"); if (r) r.innerHTML = ""; }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && document.querySelector("#modal-root .rd-sheet")) closeSheet();
  });
  document.addEventListener("click", function (e) {
    if (e.target.classList && e.target.classList.contains("rd-sheet-backdrop")) closeSheet();
    if (e.target.closest && e.target.closest("[data-sheet-close]")) closeSheet();
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail.target && e.detail.target.id === "modal-root") {
      var s = document.querySelector("#modal-root .rd-sheet"); if (s) s.focus();
    }
  });
  // Auto-hide the result toast a few seconds after it is swapped in OOB.
  var toastTimer;
  document.addEventListener("htmx:oobAfterSwap", function (e) {
    var t = e.detail && e.detail.target;
    if (!t || t.id !== "toast") return;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove("show"); }, 7000);
  });

  // <form data-reset-on-success>: clear the form after a successful htmx post.
  document.addEventListener("htmx:afterRequest", function (e) {
    var form = e.detail && e.detail.elt;
    if (form && form.matches && form.matches("form[data-reset-on-success]")
        && e.detail.successful) {
      form.reset();
    }
  });
  // data-picked-from="#select": send `picked` = that select's value, but only
  // once the person chose it by hand (data-manual, YONDER-188).
  document.addEventListener("htmx:configRequest", function (e) {
    var elt = e.detail && e.detail.elt;
    var sel = elt && elt.getAttribute && elt.getAttribute("data-picked-from");
    if (!sel) return;
    var s = document.querySelector(sel);
    e.detail.parameters.picked = s && s.dataset.manual ? s.value : "";
  });
})();
