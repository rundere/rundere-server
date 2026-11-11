// What's new after an upgrade (SPEC-8 §14, D-234). The dialog is rendered open by
// the server; here it becomes a modal, and closing it any way (Got it, ×, Esc)
// records it as seen in the background, so the page stays where it is. Without
// this script the dialog shows in place and its form posts normally.
(function () {
  'use strict';

  function init() {
    var dlg = document.getElementById('whats-new');
    if (!dlg || dlg.dataset.ready) return;
    dlg.dataset.ready = '1';
    var form = dlg.querySelector('form');
    var sent = false;

    function seen() {
      if (sent) return;
      sent = true;
      fetch(form.action, {
        method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: { 'X-Requested-With': 'fetch' }, body: new FormData(form)
      }).catch(function () { /* shown again next time; nothing else to do */ });
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      dlg.close();
    });
    dlg.addEventListener('close', seen);          // Got it, ×, or Esc

    if (typeof dlg.showModal === 'function') {
      // Rendered open; reopen as a modal. Removing `open` fires no close event,
      // unlike dlg.close(), which would record it as seen at once.
      dlg.removeAttribute('open');
      dlg.showModal();
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
