const profileContent = document.querySelector("#page-content");

function hasUnsavedProfileChanges() {
  const root = document.querySelector(".profile-settings");
  const form = root && root.querySelector("[data-profile-details-form]");
  const note = root && root.querySelector("[data-profile-unsaved]");
  return Boolean(form && form.dataset.saving !== "true" && note && !note.hidden);
}

window.addEventListener("beforeunload", (event) => {
  if (hasUnsavedProfileChanges()) {
    event.preventDefault();
    event.returnValue = "";
  }
});

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-app-nav]");
  if (link && hasUnsavedProfileChanges() && !window.confirm("You have unsaved profile changes. Leave this page?")) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-app-search]");
  if (form && hasUnsavedProfileChanges() && !window.confirm("You have unsaved profile changes. Leave this page?")) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}, true);

function initializeProfileSettings() {
  const root = document.querySelector(".profile-settings:not([data-profile-settings-ready])");
  if (!root) {
    return;
  }
  root.dataset.profileSettingsReady = "true";

  const nameInput = root.querySelector("[data-profile-name]");
  const bioInput = root.querySelector("[data-profile-bio]");
  const previewName = root.querySelector("[data-profile-preview-name]");
  const previewBio = root.querySelector("[data-profile-preview-bio]");
  const previewImage = root.querySelector("[data-profile-preview-image]");
  const previewInitial = root.querySelector("[data-profile-preview-initial]");
  const bioCount = root.querySelector("#profile-bio-count");
  const detailsForm = root.querySelector("[data-profile-details-form]");
  const unsavedNote = root.querySelector("[data-profile-unsaved]");
  const photoInput = root.querySelector("[data-profile-photo-input]");
  const photoSelection = root.querySelector("[data-profile-photo-selection]");
  const photoErrors = root.querySelector("#profile-photo-errors");
  const clearPhotoButton = root.querySelector("[data-profile-photo-clear]");
  const saveButton = detailsForm && detailsForm.querySelector(".profile-save-button");
  const photoState = root.querySelector("[data-profile-photo-state]");
  const initialName = nameInput ? nameInput.value.trim() : "";
  const initialBio = bioInput ? bioInput.value.trim() : "";
  const initialImageSrc = previewImage ? previewImage.getAttribute("src") : null;
  const initialImageHidden = previewImage ? previewImage.hidden : true;
  const initialInitialHidden = previewInitial ? previewInitial.hidden : true;
  let previewObjectUrl = null;
  let selectedPhotoIsValid = false;

  const restorePhotoPreview = () => {
    if (previewObjectUrl) {
      URL.revokeObjectURL(previewObjectUrl);
      previewObjectUrl = null;
    }
    if (previewImage) {
      if (initialImageSrc) {
        previewImage.src = initialImageSrc;
      } else {
        previewImage.removeAttribute("src");
      }
      previewImage.hidden = initialImageHidden;
    }
    if (previewInitial) {
      previewInitial.hidden = initialInitialHidden;
    }
  };

  const hasChanges = () => Boolean(
    (nameInput && nameInput.value.trim() !== initialName) ||
    (bioInput && bioInput.value.trim() !== initialBio) ||
    selectedPhotoIsValid
  );

  const updateSaveState = () => {
    const hasInvalidPhoto = Boolean(photoInput && photoInput.files.length && !selectedPhotoIsValid);
    if (saveButton) {
      saveButton.disabled = !hasChanges() || hasInvalidPhoto;
    }
    if (unsavedNote) {
      unsavedNote.hidden = !hasChanges();
    }
  };

  const updatePreview = () => {
    if (previewName && nameInput) {
      previewName.textContent = nameInput.value.trim() || "Your name";
    }
    if (previewInitial && nameInput) {
      previewInitial.textContent = (Array.from(nameInput.value.trim())[0] || "?").toLocaleUpperCase();
    }
    if (previewBio && bioInput) {
      previewBio.textContent = bioInput.value;
      previewBio.hidden = !bioInput.value;
    }
    if (bioCount && bioInput) {
      const length = Array.from(bioInput.value).length;
      bioCount.textContent = `${length} / 300`;
      bioCount.classList.toggle("is-limit", length === 300);
    }
    updateSaveState();
  };

  if (nameInput) {
    nameInput.addEventListener("input", updatePreview);
  }
  if (bioInput) {
    bioInput.addEventListener("input", () => {
      const characters = Array.from(bioInput.value);
      if (characters.length > 300) {
        const cursor = Array.from(bioInput.value.slice(0, bioInput.selectionStart)).length;
        bioInput.value = characters.slice(0, 300).join("");
        const nextCursor = Math.min(cursor, 300);
        bioInput.setSelectionRange(nextCursor, nextCursor);
      }
      updatePreview();
    });
  }

  if (photoInput) {
    photoInput.addEventListener("change", () => {
      restorePhotoPreview();
      selectedPhotoIsValid = false;
      photoInput.setCustomValidity("");
      photoInput.removeAttribute("aria-invalid");
      photoErrors.replaceChildren();
      photoState.textContent = "";
      clearPhotoButton.hidden = !photoInput.files.length;
      const file = photoInput.files && photoInput.files[0];
      if (!file) {
        photoSelection.textContent = "No file selected";
        updateSaveState();
        return;
      }

      const allowedTypes = ["image/jpeg", "image/png", "image/webp"];
      let error = "";
      if (!allowedTypes.includes(file.type)) {
        error = "Choose a JPG, PNG, or WEBP image.";
      } else if (file.size > Number(photoInput.dataset.maxBytes)) {
        error = `Profile photos must be ${photoInput.dataset.maxBytesLabel} or smaller.`;
      }
      photoSelection.textContent = file.name;
      if (error) {
        photoInput.setCustomValidity(error);
        photoInput.setAttribute("aria-invalid", "true");
        photoErrors.textContent = error;
        updateSaveState();
        return;
      }

      selectedPhotoIsValid = true;
      previewObjectUrl = URL.createObjectURL(file);
      previewImage.src = previewObjectUrl;
      previewImage.hidden = false;
      if (previewInitial) {
        previewInitial.hidden = true;
      }
      updateSaveState();
    });
  }

  if (clearPhotoButton && photoInput) {
    clearPhotoButton.addEventListener("click", () => {
      photoInput.value = "";
      photoInput.dispatchEvent(new Event("change", { bubbles: true }));
      photoInput.focus();
    });
  }

  if (detailsForm) {
    detailsForm.addEventListener("submit", (event) => {
      if (!hasChanges() || (photoInput && photoInput.files.length && !selectedPhotoIsValid)) {
        event.preventDefault();
        return;
      }
      detailsForm.dataset.saving = "true";
      if (saveButton) {
        saveButton.disabled = true;
        saveButton.textContent = "Saving…";
      }
      if (photoState) {
        photoState.textContent = "Saving your profile…";
      }
    });
  }

  const removeForm = root.querySelector("[data-profile-photo-remove]");
  if (removeForm) {
    removeForm.addEventListener("submit", (event) => {
      const message = hasChanges()
        ? "Remove your current profile photo? Your unsaved profile changes will not be saved."
        : "Remove your current profile photo?";
      if (!window.confirm(message)) {
        event.preventDefault();
        return;
      }
      removeForm.dataset.saving = "true";
      if (detailsForm) {
        detailsForm.dataset.saving = "true";
      }
    });
  }

  const toast = profileContent && profileContent.querySelector(".profile-toast");
  if (toast) {
    window.setTimeout(() => toast.classList.add("is-dismissed"), 4500);
  }
  updatePreview();
}

if (profileContent) {
  new MutationObserver(initializeProfileSettings).observe(profileContent, {
    childList: true,
    subtree: true,
  });
}
initializeProfileSettings();
