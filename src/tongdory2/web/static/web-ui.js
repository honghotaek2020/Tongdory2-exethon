// Light dismissal must distinguish backdrop clicks from a dialog's own padding.
(() => {
  if (!('closedBy' in HTMLDialogElement.prototype)) {
    const outside = (event, dialog) => {
      const rect = dialog.getBoundingClientRect();
      return event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom;
    };
    document.querySelectorAll('dialog[closedby="any"]').forEach(dialog => {
      let pressedOutside = false;
      dialog.addEventListener('pointerdown', event => {
        pressedOutside = event.isPrimary && event.button === 0 && outside(event, dialog);
      });
      dialog.addEventListener('pointerup', event => {
        if (pressedOutside && event.isPrimary && event.button === 0 && outside(event, dialog)) dialog.close();
        pressedOutside = false;
      });
      dialog.addEventListener('pointercancel', () => { pressedOutside = false; });
    });
  }
  const past = document.getElementById('soft-past-bar');
  if (!past) return;
  const key = 'past-dismiss:' + location.pathname;
  const openPast = () => {
    try { if (sessionStorage.getItem(key) === '1') return; } catch (_) {}
    // This notice opens on navigation rather than from a button. Use the page
    // heading as its origin so native focus restoration lands in the content.
    document.getElementById('main-heading').focus({preventScroll: true});
    past.showModal();
  };
  past.addEventListener('close', () => {
    try { sessionStorage.setItem(key, '1'); } catch (_) {}
  });
  document.getElementById('dismiss-past-bar').addEventListener('click', () => past.close());
  const notice = document.getElementById('notice-modal');
  if (notice && notice.open) notice.addEventListener('close', openPast, {once: true});
  else openPast();
})();
