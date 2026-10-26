function initializeConversation() {
  const page = document.querySelector(".conversation-page:not([data-conversation-ready])");
  if (!page) {
    return;
  }
  page.dataset.conversationReady = "true";

  const thread = page.querySelector(".message-thread");
  const form = page.querySelector("[data-message-composer]");
  const textarea = form && form.querySelector("textarea");
  const submitControl = form && form.querySelector(".message-send");
  const sendStatus = form && form.querySelector("[data-message-send-status]");

  if (thread) {
    thread.scrollTop = thread.scrollHeight;
  }
  if (!form || !textarea || !submitControl) {
    return;
  }

  textarea.addEventListener("keydown", (event) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.altKey &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.isComposing
    ) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  form.addEventListener("submit", (event) => {
    if (form.dataset.sending === "true") {
      event.preventDefault();
      return;
    }
    if (!textarea.value.trim()) {
      event.preventDefault();
      if (sendStatus) {
        sendStatus.textContent = "Write a message before sending.";
      }
      textarea.focus();
      return;
    }

    form.dataset.sending = "true";
    form.setAttribute("aria-busy", "true");
    submitControl.disabled = true;
    if (submitControl instanceof HTMLInputElement) {
      submitControl.value = "Sending…";
    } else {
      submitControl.textContent = "Sending…";
    }
    if (sendStatus) {
      sendStatus.textContent = "Sending your message…";
    }
  });
}

function observeConversationNavigation() {
  initializeConversation();
  const content = document.querySelector("#page-content");
  if (content) {
    new MutationObserver(initializeConversation).observe(content, {
      childList: true,
      subtree: true,
    });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", observeConversationNavigation, { once: true });
} else {
  observeConversationNavigation();
}
