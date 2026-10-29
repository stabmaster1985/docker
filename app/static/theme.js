// Läuft im <head>, damit beim Laden nichts aufblitzt.
(function () {
  try {
    var t = localStorage.getItem("theme");
    if (t === "hell" || t === "dunkel") document.documentElement.dataset.theme = t;
  } catch (e) { /* Speicher gesperrt: Systemeinstellung gilt */ }
})();
