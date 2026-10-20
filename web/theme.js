// Sets the colour theme before first paint (loaded synchronously from <head>, so there is no flash). A stored choice
// ("light" or "dark") wins; otherwise no data-theme is set and style.css follows prefers-color-scheme.
// app.js owns the toggle button and writes the same key.
(function () {
  "use strict";
  try {
    var stored = localStorage.getItem("dash-theme");
    if (stored === "light" || stored === "dark") document.documentElement.setAttribute("data-theme", stored);
  } catch (e) {
    // blocked storage (private window): follow the system theme
  }
})();
