document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;

  const openButton = target.closest("[data-dialog-open]");
  if (openButton) {
    const dialogId = openButton.getAttribute("data-dialog-open");
    const dialog = dialogId ? document.getElementById(dialogId) : null;
    if (dialog instanceof HTMLDialogElement) {
      dialog.showModal();
      dialog.querySelector("textarea")?.focus();
    }
    return;
  }

  const closeButton = target.closest("[data-dialog-close]");
  if (closeButton) {
    closeButton.closest("dialog")?.close();
  }
});

document.addEventListener(
  "submit",
  (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const submitter = event.submitter;
    const message =
      submitter instanceof HTMLElement
        ? submitter.getAttribute("data-confirm")
        : form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  },
  true,
);
