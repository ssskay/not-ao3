/* Kudos, Mark for Later, hits and browse filters. Everything stays in this browser (localStorage). */
(function () {
  "use strict";
  var KEY = { kudos: "notao3:kudos", later: "notao3:later", hits: "notao3:hits" };
  function load(k) { try { return JSON.parse(localStorage.getItem(k) || "{}") || {}; } catch (e) { return {}; } }
  function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { console.warn("[notao3] can't save", k, e); } }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  var kudos = load(KEY.kudos), later = load(KEY.later), hits = load(KEY.hits);

  // ---- work pages
  var work = document.querySelector(".work[data-work]");
  if (work) {
    var id = work.getAttribute("data-work");
    var seen = false;
    try { seen = sessionStorage.getItem("notao3:seen:" + id) === "1"; sessionStorage.setItem("notao3:seen:" + id, "1"); } catch (e) {}
    if (!seen) { hits[id] = (hits[id] || 0) + 1; save(KEY.hits, hits); }
    var paint = function () {
      $all(".nao-hits-count").forEach(function (el) { el.textContent = (hits[id] || 0).toLocaleString(); });
      $all(".nao-kudos-count").forEach(function (el) { el.textContent = kudos[id] ? "1" : "0"; });
      var msg = document.querySelector(".nao-kudos-msg");
      if (msg) { msg.hidden = !kudos[id]; }
      $all(".nao-later-btn").forEach(function (b) { b.textContent = later[id] ? "Mark as Read" : "Mark for Later"; });
    };
    paint();
    $all(".nao-kudos-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        var msg = document.querySelector(".nao-kudos-msg");
        if (kudos[id] && msg) { msg.textContent = "You have already left kudos here. :)"; }
        kudos[id] = Date.now(); save(KEY.kudos, kudos); paint();
        console.info("[notao3] kudos", id);
      });
    });
    $all(".nao-later-btn").forEach(function (b) {
      b.addEventListener("click", function (ev) {
        ev.preventDefault();
        if (later[id]) { delete later[id]; } else { later[id] = Date.now(); }
        save(KEY.later, later); paint();
        console.info("[notao3] mark for later", id, !!later[id]);
      });
    });
    $all("select.nao-jump").forEach(function (s) {
      s.addEventListener("change", function () { location.href = s.value; });
    });
  }

  // ---- blurbs anywhere: show local kudos/hits
  function decorate(root) {
    $all("li.blurb[data-id]", root).forEach(function (li) {
      var wid = li.getAttribute("data-id");
      if (kudos[wid]) { $all(".nao-kudos-stat", li).forEach(function (el) { el.hidden = false; }); }
      if (hits[wid]) {
        $all(".nao-hits-stat", li).forEach(function (el) { el.hidden = false; if (el.tagName === "DD") el.textContent = hits[wid].toLocaleString(); });
      }
    });
  }
  decorate(document);

  // ---- browse page filters
  var q = document.getElementById("nao-q");
  if (q) {
    var list = document.querySelector("ol.work.index");
    var items = $all("li.blurb", list);
    var fandom = document.getElementById("nao-fandom"), sort = document.getElementById("nao-sort");
    var count = document.querySelector(".nao-count");
    var params = new URLSearchParams(location.search);
    q.value = params.get("q") || "";
    var head = document.getElementById("site_search"); if (head) head.value = q.value;
    var apply = function () {
      var terms = q.value.toLowerCase().split(/\s+/).filter(Boolean), f = fandom.value, shown = 0;
      items.forEach(function (li) {
        var hay = li.getAttribute("data-search");
        var ok = terms.every(function (t) { return hay.indexOf(t) > -1; }) &&
          (!f || li.getAttribute("data-fandoms").split("|").indexOf(f) > -1);
        li.hidden = !ok; if (ok) shown++;
      });
      var key = sort.value;
      items.slice().sort(function (a, b) {
        if (key === "title") return a.getAttribute("data-title").localeCompare(b.getAttribute("data-title"));
        if (key === "words") return (+b.getAttribute("data-words")) - (+a.getAttribute("data-words"));
        return b.getAttribute("data-date").localeCompare(a.getAttribute("data-date"));
      }).forEach(function (li) { list.appendChild(li); });
      count.textContent = (terms.length || f) ? ("Showing " + shown + " of " + items.length + " works") : "";
    };
    [q, fandom, sort].forEach(function (el) { el.addEventListener("input", apply); el.addEventListener("change", apply); });
    apply();
  }

  // ---- Marked for Later page
  var tpl = document.getElementById("nao-blurbs");
  if (tpl) {
    var byId = {};
    $all("li.blurb", tpl.content).forEach(function (li) { byId[li.getAttribute("data-id")] = li; });
    var fill = function (store, listId, emptyId) {
      var ol = document.getElementById(listId), n = 0;
      Object.keys(store).sort(function (a, b) { return store[b] - store[a]; }).forEach(function (wid) {
        if (byId[wid]) { ol.appendChild(byId[wid].cloneNode(true)); n++; }
      });
      document.getElementById(emptyId).hidden = n > 0;
    };
    fill(later, "nao-later", "nao-later-empty");
    fill(kudos, "nao-kudos", "nao-kudos-empty");
    decorate(document);
  }
})();
