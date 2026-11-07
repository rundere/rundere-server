/* Light/dark theme (D-201). Loaded in <head> without `defer`, so the saved
   theme is applied BEFORE first paint and there's no light/dark flash. A
   static file rather than an inline <script>, so the CSP can be
   script-src 'self' (YONDER-303). No saved choice means dark, whatever the OS
   prefers. */
(function () {
  "use strict";
  var t;
  try { t = localStorage.getItem("tx-theme"); } catch (e) {}
  document.documentElement.setAttribute("data-theme", t || "dark");

  function toggle() {
    var r = document.documentElement, c = r.getAttribute("data-theme");
    var n = c === "dark" ? "light" : (c === "light" ? "dark" :
      (window.matchMedia("(prefers-color-scheme:dark)").matches ? "light" : "dark"));
    r.setAttribute("data-theme", n);
    try { localStorage.setItem("tx-theme", n); } catch (e) {}
  }
  // <button data-theme-toggle>: the header's sun/moon button.
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-theme-toggle]")) toggle();
  });
})();
