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

/* Post New Work: only works when the archive is running on this computer (notao3.py serve). */
(function () {
  "use strict";
  var form = document.getElementById("nao-post");
  if (!form) return;
  var root = document.body.getAttribute("data-root") || "./";
  var offline = document.getElementById("nao-post-offline");
  var status = document.getElementById("nao-post-status");
  fetch(root + "api/ping").then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function () { form.hidden = false; })
    .catch(function () { offline.hidden = false; });

  function readFile(f) {
    return new Promise(function (resolve, reject) {
      var rd = new FileReader();
      rd.onload = function () { resolve({ name: f.name, data: String(rd.result).split(",")[1] || "" }); };
      rd.onerror = function () { reject(rd.error); };
      rd.readAsDataURL(f);
    });
  }
  var val = function (id) { return (document.getElementById(id).value || "").trim(); };

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var files = Array.prototype.slice.call(document.getElementById("nao-files").files);
    var urls = val("nao-urls").split(/\s+/).filter(Boolean);
    if (!files.length && !urls.length) { status.textContent = "Add a file or a link first."; return; }
    var btn = form.querySelector("input[type=submit]");
    btn.disabled = true;
    status.textContent = "Posting" + (urls.length ? " (downloading links)" : "") + " and rebuilding the archive...";
    Promise.all(files.map(readFile)).then(function (data) {
      return fetch(root + "api/post", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ files: data, urls: urls, fandom: val("nao-fandom-in"), rating: val("nao-rating"),
          tags: val("nao-tags"), title: val("nao-title"), author: val("nao-author"), summary: val("nao-summary") })
      });
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        btn.disabled = false;
        if (!res.ok) { status.textContent = "Couldn't post: " + (res.j.error || "unknown error"); return; }
        console.info("[notao3] posted", res.j);
        var works = res.j.works || [];
        if (works.length === 1 && !(res.j.errors || []).length) { location.href = root + works[0].url; return; }
        status.innerHTML = "";
        works.forEach(function (w) {
          var a = document.createElement("a"); a.href = root + w.url; a.textContent = w.title;
          var p = document.createElement("p"); p.textContent = "Posted: "; p.appendChild(a); status.appendChild(p);
        });
        (res.j.errors || []).forEach(function (e) { var p = document.createElement("p"); p.textContent = e; status.appendChild(p); });
      })
      .catch(function (e) { btn.disabled = false; status.textContent = "Couldn't post: " + e; });
  });
})();
