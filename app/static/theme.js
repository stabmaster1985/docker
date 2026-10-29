// Läuft im <head>, damit beim Laden nichts aufblitzt.
(function () {
  try {
    var t = localStorage.getItem("theme");
    if (t === "hell" || t === "dunkel") document.documentElement.dataset.theme = t;
    if (localStorage.getItem("ports") === "0") document.documentElement.classList.add("ports-aus");
  } catch (e) { /* Speicher gesperrt: Systemeinstellung gilt */ }
})();
