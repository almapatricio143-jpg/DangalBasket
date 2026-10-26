const passwordField = document.getElementById("password");
const confirmationField = document.getElementById("confirm_password");
const certificateField = document.getElementById("certificate");
const certificateHint = document.getElementById("certificate-hint");
const certificateHintText = certificateHint.textContent;

function validatePasswordConfirmation() {
  const mismatch = confirmationField.value && passwordField.value !== confirmationField.value;
  confirmationField.setCustomValidity(mismatch ? "Passwords do not match." : "");
}

function updateCertificateSelection() {
  const count = certificateField.files.length;
  certificateField.setCustomValidity(
    count > 5 ? "Choose no more than 5 pictures." : "",
  );
  certificateHint.textContent =
    count > 0 && count <= 5
      ? `${count} picture${count === 1 ? "" : "s"} selected. ${certificateHintText}`
      : certificateHintText;
}

passwordField.addEventListener("input", validatePasswordConfirmation);
confirmationField.addEventListener("input", validatePasswordConfirmation);
certificateField.addEventListener("change", updateCertificateSelection);
