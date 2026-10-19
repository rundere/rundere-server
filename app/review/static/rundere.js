/* Small behaviours the pages need, as plain listeners (YONDER-293): htmx's
   eval features (hx-on, hx-vals "js:", trigger filters) are switched off so the
   Content-Security-Policy needs no 'unsafe-eval'. */
(function () {
  "use strict";
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
