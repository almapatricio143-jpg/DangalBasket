function initializeSupportChat() {
  const page = document.querySelector(".support-chat-page:not([data-support-chat-ready])");
  if (!page) {
    return;
  }
  page.dataset.supportChatReady = "true";

  const thread = page.querySelector(".support-chat-thread");
  const form = page.querySelector("[data-support-composer]");
  const textarea = form && form.querySelector("textarea");
  const submitButton = form && form.querySelector(".support-message-send");
  const sendStatus = form && form.querySelector("[data-message-send-status]");

  if (thread) {
    thread.scrollTop = thread.scrollHeight;
  }
  if (!form || !textarea || !submitButton) {
    return;
  }

  const resizeTextarea = () => {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  };
  const updateSubmitState = () => {
    submitButton.disabled = form.dataset.sending === "true" || !textarea.value.trim();
  };

  textarea.addEventListener("input", () => {
    resizeTextarea();
    updateSubmitState();
  });
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
      if (textarea.value.trim()) {
        form.requestSubmit();
      }
    }
  });

  resizeTextarea();
  updateSubmitState();
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
    submitButton.disabled = true;
    submitButton.value = "Sending…";
    if (sendStatus) {
      sendStatus.textContent = "Sending your message…";
    }
  });
}

function observeSupportChatNavigation() {
  initializeSupportChat();
  const content = document.querySelector("#page-content");
  if (content) {
    new MutationObserver(initializeSupportChat).observe(content, {
      childList: true,
      subtree: true,
    });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", observeSupportChatNavigation, { once: true });
} else {
  observeSupportChatNavigation();
}
