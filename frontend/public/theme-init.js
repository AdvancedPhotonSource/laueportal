// Applies the stored theme before the first paint, so dark-mode users see no light flash.
// Loaded as a blocking classic script from index.html (no inline script, so a strict CSP works).
// Mirrors readStoredTheme/resolveTheme/applyTheme in src/lib/theme.ts: keep them in sync.
;(function () {
  var theme = null
  try {
    theme = window.localStorage.getItem("laueportal:theme")
  } catch (e) {
    // storage unavailable: follow the OS
  }
  var dark =
    theme === "dark" ||
    (theme !== "light" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches)
  document.documentElement.classList.toggle("dark", dark)
  document.documentElement.style.colorScheme = dark ? "dark" : "light"
  document.documentElement.dataset.agThemeMode = dark ? "dark" : "light"
})()
