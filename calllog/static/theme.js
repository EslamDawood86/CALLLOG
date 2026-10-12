/* Theme switch: Auto (follow the device) -> Light -> Dark -> Auto.
   Loaded in <head> (see base.html) so the saved choice is applied before the first paint: no flash of the wrong theme.
   The choice is stored in this browser only (localStorage). "Auto" removes data-theme from <html> and the CSS
   follows prefers-color-scheme as before; "light" / "dark" force that theme (app.css, ops.css, ops-sla.css).
   No inline script or style (strict CSP) and text is set with textContent only. */
(function () {
  "use strict";
  var KEY = "nbe-theme";
  var MODES = ["auto", "light", "dark"];
  var ICONS = { auto: "◐", light: "☀", dark: "☾" };
  var NAMES = { auto: "Auto", light: "Light", dark: "Dark" };
  var HINTS = { auto: "follows your device", light: "always light", dark: "always dark" };
  var root = document.documentElement;
  var current = "auto";

  function read() {
    try {
      var v = window.localStorage.getItem(KEY);
      return MODES.indexOf(v) > -1 ? v : "auto";
    } catch (e) { return "auto"; }                    // storage blocked: keep working for this page view
  }
  function save(mode) {
    try {
      if (mode === "auto") window.localStorage.removeItem(KEY); else window.localStorage.setItem(KEY, mode);
    } catch (e) { /* not saved - still applied */ }
  }
  function apply(mode) {
    current = mode;
    if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode);
    else root.removeAttribute("data-theme");
  }
  function paint(button) {
    var ico = button.querySelector(".theme-ico");
    var label = button.querySelector(".theme-label");
    if (ico) ico.textContent = ICONS[current];
    if (label) label.textContent = NAMES[current];
    var next = MODES[(MODES.indexOf(current) + 1) % MODES.length];
    var text = "Theme: " + NAMES[current] + " (" + HINTS[current] + "). Click to switch to " + NAMES[next] + ".";
    button.setAttribute("title", text);
    button.setAttribute("aria-label", text);
  }
  function paintAll() {
    var buttons = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < buttons.length; i++) paint(buttons[i]);
  }
  function wire() {
    var buttons = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", function () {
        var mode = MODES[(MODES.indexOf(current) + 1) % MODES.length];
        apply(mode); save(mode); paintAll();
      });
    }
    paintAll();
  }

  apply(read());
  window.addEventListener("storage", function (event) {      // another tab changed it: follow
    if (event.key === KEY || event.key === null) { apply(read()); paintAll(); }
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire); else wire();
})();
