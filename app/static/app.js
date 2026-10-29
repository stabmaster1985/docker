(function () {
  "use strict";

  function lese(key, fallback) {
    try { var v = localStorage.getItem(key); return v === null ? fallback : v; } catch (e) { return fallback; }
  }
  function schreibe(key, wert) {
    try { localStorage.setItem(key, wert); } catch (e) { /* egal */ }
  }

  // ---------------------------------------------------------------- Theme
  function dunkelAktiv() {
    var t = document.documentElement.dataset.theme;
    if (t) return t === "dunkel";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  document.addEventListener("click", function (e) {
    if (!e.target.closest("#theme-toggle")) return;
    var neu = dunkelAktiv() ? "hell" : "dunkel";
    document.documentElement.dataset.theme = neu;
    schreibe("theme", neu);
  });

  // ---------------------------------------------------------------- Toasts
  function toast(text, ok) {
    var box = document.getElementById("toasts");
    if (!box) return;
    var el = document.createElement("div");
    el.className = "toast " + (ok ? "toast-ok" : "toast-fehler");
    el.setAttribute("role", "status");
    el.textContent = text;
    box.appendChild(el);
  }
  function toastAusblenden(t) {
    t.addEventListener("click", function () { t.remove(); });
    setTimeout(function () { t.classList.add("weg"); }, 7000);
    setTimeout(function () { t.remove(); }, 7400);
  }
  var toastBox = document.getElementById("toasts");
  if (toastBox) {
    toastBox.querySelectorAll(".toast").forEach(toastAusblenden);
    new MutationObserver(function (mutations) {
      mutations.forEach(function (m) {
        m.addedNodes.forEach(function (n) {
          if (n.nodeType !== 1) return;
          var toasts = n.classList.contains("toast") ? [n] : n.querySelectorAll(".toast");
          toasts.forEach(toastAusblenden);
        });
      });
    }).observe(toastBox, { childList: true, subtree: true });
  }

  // ------------------------------------------------- Bestätigungsdialog
  var dialog = document.getElementById("bestaetigen");
  document.addEventListener("htmx:confirm", function (e) {
    if (!e.detail.question) return;
    e.preventDefault();
    if (!dialog || typeof dialog.showModal !== "function") {
      if (window.confirm(e.detail.question)) e.detail.issueRequest(true);
      return;
    }
    document.getElementById("bestaetigen-text").textContent = e.detail.question;
    dialog.returnValue = "";
    dialog.addEventListener("close", function zu() {
      dialog.removeEventListener("close", zu);
      if (dialog.returnValue === "ja") e.detail.issueRequest(true);
    });
    dialog.showModal();
    document.getElementById("bestaetigen-ja").focus();
  });

  // ------------------------------------------------- Fehler von Requests
  document.addEventListener("htmx:responseError", function (e) {
    var xhr = e.detail.xhr;
    if (xhr.status === 401) return; // HX-Redirect zur Anmeldung übernimmt
    toast("Fehler " + xhr.status + ": " + (xhr.responseText || "Anfrage fehlgeschlagen").slice(0, 200), false);
  });
  document.addEventListener("htmx:sendError", function (e) {
    // Abfragen während eines Dashboard-Updates dürfen still scheitern.
    if (e.detail.elt && e.detail.elt.closest("[data-leise]")) return;
    toast("Dashboard nicht erreichbar.", false);
  });

  // ------------------------------------------------- Nach Dashboard-Update neu laden
  document.addEventListener("htmx:afterSwap", function (e) {
    var v = e.detail.target && e.detail.target.querySelector("[data-laufende-version]");
    var seite = document.body.dataset.version;
    if (v && seite && v.dataset.laufendeVersion && v.dataset.laufendeVersion !== seite) {
      window.location.reload();
    }
  });

  // ------------------------------------------------- Laufende Requests
  var laufend = 0;
  document.addEventListener("htmx:beforeRequest", function () { laufend++; });
  document.addEventListener("htmx:afterRequest", function () { laufend = Math.max(0, laufend - 1); });

  // ------------------------------------------------- Auto-Aktualisierung
  var liste = document.getElementById("liste");
  var auto = document.getElementById("auto-refresh");
  if (liste && auto) {
    auto.checked = lese("autoRefresh", "1") === "1";
    auto.addEventListener("change", function () { schreibe("autoRefresh", auto.checked ? "1" : "0"); });
    var sek = parseInt(liste.dataset.intervall, 10) || 15;
    var zuletzt = Date.now();
    function faellig() {
      return auto.checked && !document.hidden && laufend === 0 && !(dialog && dialog.open);
    }
    setInterval(function () {
      if (faellig() && Date.now() - zuletzt >= sek * 1000) {
        zuletzt = Date.now();
        htmx.trigger(liste, "auto-refresh");
      }
    }, 1000);
    document.addEventListener("htmx:afterSwap", function (e) {
      if (e.detail.target === liste) zuletzt = Date.now();
    });
  }

  // ------------------------------------------------- Container-Suche
  var suche = document.getElementById("suche");
  function filtereContainer() {
    if (!suche) return;
    var q = suche.value.trim().toLowerCase();
    document.querySelectorAll("#liste tr[data-suche]").forEach(function (tr) {
      tr.hidden = q !== "" && tr.dataset.suche.indexOf(q) === -1;
    });
    document.querySelectorAll("#liste .gruppe").forEach(function (g) {
      var sichtbar = g.querySelector("tr[data-suche]:not([hidden])");
      g.hidden = q !== "" && !sichtbar;
    });
  }
  if (suche) suche.addEventListener("input", filtereContainer);

  // ------------------------------------------------- Gruppen auf/zu merken
  function zugeklappt() {
    try { return JSON.parse(lese("zu", "[]")); } catch (e) { return []; }
  }
  function gruppenAnwenden() {
    var zu = zugeklappt();
    document.querySelectorAll("details[data-gruppe]").forEach(function (d) {
      d.open = zu.indexOf(d.dataset.gruppe) === -1;
    });
  }
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (!d.matches || !d.matches("details[data-gruppe]")) return;
    var zu = zugeklappt().filter(function (n) { return n !== d.dataset.gruppe; });
    if (!d.open) zu.push(d.dataset.gruppe);
    schreibe("zu", JSON.stringify(zu));
  }, true);

  // ------------------------------------------------- Log-Filter
  var logFilter = document.getElementById("log-filter");
  var umbruch = document.getElementById("log-umbruch");
  function filtereLogs() {
    var pre = document.getElementById("log-ausgabe");
    var info = document.getElementById("log-treffer");
    if (!pre || !logFilter) { if (info) info.textContent = ""; return; }
    var q = logFilter.value.trim().toLowerCase();
    var zeilen = pre.querySelectorAll(".log-zeile");
    var treffer = 0;
    zeilen.forEach(function (z) {
      var passt = q === "" || z.textContent.toLowerCase().indexOf(q) !== -1;
      z.hidden = !passt;
      if (passt) treffer++;
    });
    if (info) info.textContent = q === "" ? zeilen.length + " Zeilen" : treffer + " von " + zeilen.length;
    pre.classList.toggle("kein-umbruch", umbruch && !umbruch.checked);
  }
  function logsNachUnten() {
    var pre = document.getElementById("log-ausgabe");
    if (pre) pre.scrollTop = pre.scrollHeight;
  }
  if (logFilter) logFilter.addEventListener("input", filtereLogs);
  if (umbruch) umbruch.addEventListener("change", filtereLogs);

  // ------------------------------------------------- Nach jedem Swap
  document.addEventListener("htmx:afterSettle", function () {
    gruppenAnwenden();
    filtereContainer();
    filtereLogs();
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail.target && e.detail.target.id === "logs") logsNachUnten();
  });

  gruppenAnwenden();
  filtereLogs();
  logsNachUnten();
})();
