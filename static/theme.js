const themeToggle = document.querySelector("[data-theme-toggle]");
const themeLabel = document.querySelector("[data-theme-label]");

function updateThemeToggle(theme) {
  const lightMode = theme === "light";
  themeLabel.textContent = lightMode ? "Dark mode" : "Light mode";
  themeToggle.setAttribute(
    "aria-label",
    `Switch to ${lightMode ? "dark" : "light"} mode`
  );
  themeToggle.querySelector("[aria-hidden='true']").textContent = lightMode
    ? "☾"
    : "☼";
}

updateThemeToggle(document.documentElement.dataset.theme);

themeToggle.addEventListener("click", () => {
  const theme =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("dangalbasket-theme", theme);
  } catch {
    // Keep the selected theme for this page even when storage is unavailable.
  }
  updateThemeToggle(theme);
});
