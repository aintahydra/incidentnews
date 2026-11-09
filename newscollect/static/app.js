(function () {
  var refreshers = []; // side-pane redraws run when new data arrives (live updates below)
  var section = document.body.dataset.section === "intel" ? "intel" : "incident"; // 보안 사고 | 인텔·권고 tab
  var ITEM = section === "intel" ? "글" : "사고"; // what one card is, in notes and the new-items button
  // --- theme buttons (initial theme is set by the inline script in <head>) ---
  var root = document.documentElement;
  function paint() {
    document.querySelectorAll("[data-theme-choice]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.themeChoice === root.dataset.theme));
    });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-theme-choice]");
    if (!b) return;
    root.dataset.theme = b.dataset.themeChoice;
    try { localStorage.setItem("theme", b.dataset.themeChoice); } catch (err) {}
    paint();
  });
  paint();

  // --- sticky header: the pane sticks just below it, whatever height the header wraps to ---
  var header = document.querySelector("header");
  var setHeaderH = function () { root.style.setProperty("--header-h", header.offsetHeight + "px"); };
  setHeaderH();
  window.addEventListener("resize", setHeaderH);

  // --- side pane toggle (initial state is set by the inline script in <head>) ---
  var paneBtn = document.querySelector(".pane-toggle");
  if (paneBtn) {
    var setPane = function (open, remember) {
      root.dataset.pane = open ? "open" : "closed";
      paneBtn.setAttribute("aria-expanded", String(open));
      if (remember && window.innerWidth >= 900) { try { localStorage.setItem("pane", root.dataset.pane); } catch (err) {} }
      // the cloud and the chart measure their box: lay them out again now that it has a size
      if (open) window.dispatchEvent(new Event("resize"));
    };
    setPane(root.dataset.pane !== "closed", false);
    paneBtn.addEventListener("click", function () { setPane(root.dataset.pane === "closed", true); });
    var narrowClose = function () { setPane(false, false); paneBtn.focus(); };
    document.querySelector(".pane-close").addEventListener("click", narrowClose);
    document.addEventListener("click", function (e) { // narrow screens: a tap outside the drawer closes it
      if (window.innerWidth < 900 && root.dataset.pane === "open" && !e.target.closest(".side, .pane-toggle")) setPane(false, false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && window.innerWidth < 900 && root.dataset.pane === "open") narrowClose();
    });
  }

  // --- regenerate summary ---
  document.addEventListener("click", async function (e) {
    var b = e.target.closest(".regen");
    if (!b) return;
    var id = b.dataset.id, label = b.textContent;
    var msg = b.parentNode.querySelector(".regen-msg");
    b.disabled = true; b.textContent = "생성 중…";
    msg.className = "regen-msg"; msg.textContent = "원문을 다시 읽고 요약을 쓰는 중입니다 (최대 1~2분)";
    try {
      var r = await fetch("/incidents/" + id + "/regenerate", { method: "POST" });
      var result = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(result.detail || "오류 " + r.status);
      // swap in the freshly rendered card (list or detail layout) right where the old one was
      var view = document.body.dataset.page === "detail" ? "detail" : "list";
      var res = await fetch("/incidents/" + id + "/card?view=" + view, { cache: "no-store" });
      if (!res.ok) throw new Error("갱신된 내용을 불러오지 못했습니다 (" + res.status + ")");
      document.getElementById("inc-" + id).outerHTML = await res.text();
      var card = document.getElementById("inc-" + id);
      var done = card.querySelector(".regen-msg");
      done.className = "regen-msg ok";
      done.textContent = result.method === "extract"
        ? "AI 요약이 실패해 기사 본문 첫 부분으로 대체했습니다" : "✓ 요약을 새로 생성했습니다";
      card.classList.add("flash");
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch (err) {
      b.disabled = false; b.textContent = label;
      msg.className = "regen-msg err"; msg.textContent = err.message;
    }
  });
  // --- tag cloud (list page only) ---
  var box = document.querySelector(".cloud-box");
  if (box) {
    var note = document.querySelector(".cloud-note"), cache = {}, current = null, seq = 0;
    var NAMES = { day: "오늘", week: "최근 7일", month: "최근 30일", year: "최근 1년" };

    var layout = function (data) {
      box.innerHTML = "";
      var words = data.tags;
      if (!words.length) { box.classList.add("empty-cloud"); return; }
      box.classList.remove("empty-cloud");
      var W = box.clientWidth, H = box.clientHeight;
      if (!W || !H) return; // pane hidden: laid out again when it opens
      var max = words[0].count, min = words[words.length - 1].count;
      var big = Math.min(46, Math.max(26, W / 14)), small = 13, placed = [];
      words.forEach(function (w) {
        var t = max === min ? 0.6 : Math.log(w.count / min) / Math.log(max / min);
        var a = document.createElement("a");
        a.href = "/?" + (section === "intel" ? "section=intel&" : "") + "q=" + encodeURIComponent(w.tag);
        a.textContent = "#" + w.tag;
        a.title = "#" + w.tag + " · 사고 " + w.count + "건";
        a.style.fontSize = (small + (big - small) * t).toFixed(1) + "px";
        a.className = "w" + (t > 0.8 ? 3 : t > 0.4 ? 2 : 1); // red only near the top
        box.appendChild(a);
        var ww = a.offsetWidth, hh = a.offsetHeight;
        // Archimedean spiral out from the centre, stretched to the box's aspect ratio
        for (var step = 0; step < 1500; step++) {
          var ang = step * 0.35, r = 8.8 * Math.sqrt(step);
          var x = W / 2 + r * Math.cos(ang) * (W / H) * 0.6 - ww / 2;
          var y = H / 2 + r * Math.sin(ang) * 0.6 - hh / 2;
          if (x < 0 || y < 0 || x + ww > W || y + hh > H) continue;
          var hit = placed.some(function (p) {
            return x < p.x + p.w + 4 && x + ww + 4 > p.x && y < p.y + p.h && y + hh > p.y;
          });
          if (!hit) {
            a.style.left = x + "px"; a.style.top = y + "px";
            placed.push({ x: x, y: y, w: ww, h: hh });
            return;
          }
        }
        a.remove(); // no room left
      });
    };

    var show = async function (period) {
      current = period;
      document.querySelectorAll("[data-period]").forEach(function (b) {
        b.setAttribute("aria-pressed", String(b.dataset.period === period));
      });
      try { localStorage.setItem("cloudPeriod", period); } catch (err) {}
      var my = ++seq;
      try {
        if (!cache[period]) {
          note.textContent = "불러오는 중…";
          var r = await fetch("/api/tagcloud?period=" + period + "&section=" + section, { cache: "no-store" });
          if (!r.ok) throw new Error("태그를 불러오지 못했습니다 (" + r.status + ")");
          cache[period] = await r.json();
        }
        if (my !== seq) return; // a newer click won
        var d = cache[period];
        layout(d);
        note.textContent = d.incidents
          ? NAMES[period] + " " + ITEM + " " + d.incidents + "건의 태그 · 누르면 검색합니다"
          : NAMES[period] + " 동안 수집된 " + (section === "intel" ? "인텔·권고가" : "사고가") + " 없습니다";
      } catch (err) {
        if (my === seq) { box.innerHTML = ""; note.textContent = err.message; }
      }
    };

    document.addEventListener("click", function (e) {
      var b = e.target.closest("[data-period]");
      if (b) show(b.dataset.period);
    });
    var timer;
    window.addEventListener("resize", function () {
      clearTimeout(timer);
      timer = setTimeout(function () { if (cache[current]) layout(cache[current]); }, 200);
    });
    refreshers.push(function () { cache = {}; show(current); });
    var saved = null;
    try { saved = localStorage.getItem("cloudPeriod"); } catch (err) {}
    show(NAMES[saved] ? saved : "week");
  }
  // --- collected-article column chart (list page only) ---
  var chart = document.querySelector(".trend-chart");
  if (chart) {
    var tip = chart.querySelector(".trend-tip"), tnote = document.querySelector(".trend-note");
    var tbody = document.querySelector(".trend-table tbody"), tcache = {}, tcur = null, tseq = 0;
    var SVGNS = "http://www.w3.org/2000/svg";
    var fmt = function (n) { return n.toLocaleString("ko-KR"); };
    var label = function (unit, p) {
      if (unit === "day") return +p.slice(5, 7) + "/" + +p.slice(8, 10);
      if (unit === "month") return p.slice(2, 4) + "." + p.slice(5, 7);
      return p;
    };
    var longLabel = function (unit, p) {
      if (unit === "day") return p.slice(0, 4) + "년 " + +p.slice(5, 7) + "월 " + +p.slice(8, 10) + "일";
      if (unit === "month") return p.slice(0, 4) + "년 " + +p.slice(5, 7) + "월";
      return p + "년";
    };
    var niceStep = function (max) { // ~4 gridlines at 1/2/5 x 10^k
      var raw = Math.max(max, 1) / 4, mag = Math.pow(10, Math.floor(Math.log10(raw)));
      var step = [1, 2, 5, 10].map(function (m) { return m * mag; }).find(function (s) { return s >= raw; });
      return Math.max(1, step);
    };
    var el = function (name, attrs, parent) {
      var e = document.createElementNS(SVGNS, name);
      for (var k in attrs) e.setAttribute(k, attrs[k]);
      if (parent) parent.appendChild(e);
      return e;
    };

    var draw = function (d) {
      var old = chart.querySelector("svg");
      if (old) old.remove();
      tip.hidden = true;
      var data = d.series, n = data.length;
      var W = chart.clientWidth, H = chart.clientHeight;
      if (!W || !H) return; // pane hidden: drawn again when it opens
      var max = Math.max.apply(null, data.map(function (x) { return x.count; }));
      var step = niceStep(max), top = Math.ceil(Math.max(max, 1) / step) * step;
      var M = { l: 8 + String(fmt(top)).length * 7, r: 6, t: 16, b: 22 };
      var pw = W - M.l - M.r, ph = H - M.t - M.b, base = M.t + ph;
      var svg = el("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img",
        "aria-label": "수집 기사 수 막대 그래프, 합계 " + fmt(d.total) + "건" });
      for (var v = 0; v <= top; v += step) { // hairline grid + clean tick labels
        var y = base - (v / top) * ph;
        el("line", { x1: M.l, x2: W - M.r, y1: y, y2: y, class: v ? "grid" : "axis" }, svg);
        el("text", { x: M.l - 6, y: y + 4, class: "tick", "text-anchor": "end" }, svg).textContent = fmt(v);
      }
      var slot = pw / n, bw = Math.max(Math.min(2, slot), Math.min(24, slot * 0.7, slot - 2)); // long spans: bars thinner than 2px
      var every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(pw / 46))));
      var maxIdx = data.findIndex(function (x) { return x.count === max; });
      data.forEach(function (x, i) {
        var cx = M.l + slot * (i + 0.5), h = (x.count / top) * ph, x0 = cx - bw / 2;
        if (h > 0) { // 4px rounded data-end, square at the baseline
          var r = Math.min(4, h, bw / 2), y0 = base - h;
          el("path", { class: "bar", d: "M" + x0 + "," + base + "V" + (y0 + r) + "Q" + x0 + "," + y0 + " " + (x0 + r) + "," + y0 +
            "H" + (x0 + bw - r) + "Q" + (x0 + bw) + "," + y0 + " " + (x0 + bw) + "," + (y0 + r) + "V" + base + "Z" }, svg);
        }
        if (i === maxIdx && max > 0) // one selective direct label: the peak
          el("text", { x: cx, y: base - h - 4, class: "val", "text-anchor": "middle" }, svg).textContent = fmt(x.count);
        if ((n - 1 - i) % every === 0) // label from the latest period backwards
          el("text", { x: cx, y: H - 6, class: "tick", "text-anchor": "middle" }, svg).textContent = label(d.unit, x.period);
        var hit = el("rect", { x: M.l + slot * i, y: M.t, width: slot, height: ph, class: "hit", tabindex: 0,
          "aria-label": longLabel(d.unit, x.period) + " " + fmt(x.count) + "건" }, svg);
        var show = function () {
          tip.textContent = longLabel(d.unit, x.period) + " · " + fmt(x.count) + "건";
          tip.hidden = false;
          var tw = tip.offsetWidth;
          tip.style.left = Math.min(Math.max(0, cx - tw / 2), W - tw) + "px";
          tip.style.top = Math.max(0, base - h - 34) + "px";
          hit.classList.add("on");
        };
        var hide = function () { tip.hidden = true; hit.classList.remove("on"); };
        hit.addEventListener("mouseenter", show); hit.addEventListener("focus", show);
        hit.addEventListener("mouseleave", hide); hit.addEventListener("blur", hide);
      });
      chart.insertBefore(svg, tip);
    };

    var fill = function (d) {
      // average over the periods that have data (server: stats.average), with the range it covers
      var a = d.average, avgText = "";
      if (a && d.unit !== "year") {
        var range = a.from === a.to ? label(d.unit, a.from) : label(d.unit, a.from) + "~" + label(d.unit, a.to);
        avgText = " · " + (d.unit === "day" ? "하루 평균 " + a.value.toFixed(1) : "월평균 " + fmt(Math.round(a.value)))
          + "건(" + range + ")";
      }
      tnote.textContent = (d.unit === "day" ? "최근 " + d.series.length + "일 " : d.unit === "month" ? "최근 12개월 " : "전체 ")
        + fmt(d.total) + "건" + avgText + " · 기사 발행일 기준";
      tbody.innerHTML = "";
      d.series.slice().reverse().forEach(function (x) {
        var tr = document.createElement("tr");
        tr.innerHTML = "<td></td><td></td>";
        tr.children[0].textContent = longLabel(d.unit, x.period);
        tr.children[1].textContent = fmt(x.count);
        tbody.appendChild(tr);
      });
    };

    var showUnit = async function (unit) {
      tcur = unit;
      document.querySelectorAll("[data-unit]").forEach(function (b) {
        b.setAttribute("aria-pressed", String(b.dataset.unit === unit));
      });
      try { localStorage.setItem("trendUnit", unit); } catch (err) {}
      var my = ++tseq;
      try {
        if (!tcache[unit]) {
          tnote.textContent = "불러오는 중…";
          var r = await fetch("/api/article-stats?unit=" + unit + "&section=" + section, { cache: "no-store" });
          if (!r.ok) throw new Error("통계를 불러오지 못했습니다 (" + r.status + ")");
          tcache[unit] = await r.json();
        }
        if (my !== tseq) return;
        draw(tcache[unit]); fill(tcache[unit]);
      } catch (err) {
        if (my === tseq) tnote.textContent = err.message;
      }
    };

    document.addEventListener("click", function (e) {
      var b = e.target.closest("[data-unit]");
      if (b) showUnit(b.dataset.unit);
    });
    var ttimer;
    window.addEventListener("resize", function () {
      clearTimeout(ttimer);
      ttimer = setTimeout(function () { if (tcache[tcur]) draw(tcache[tcur]); }, 200);
    });
    refreshers.push(function () { tcache = {}; showUnit(tcur); });
    var su = null;
    try { su = localStorage.getItem("trendUnit"); } catch (err) {}
    showUnit(["day", "month", "year"].indexOf(su) >= 0 ? su : "day");
  }

  // --- live updates (list page): poll /api/version, then swap in the freshly rendered list ---
  var live = document.getElementById("live");
  var refreshSec = live ? Number(live.dataset.refresh) : 0; // [server].refresh_seconds; 0 = off
  if (live && refreshSec > 0) {
    var POLL_MS = refreshSec * 1000, banner = document.querySelector(".new-banner"), baseTitle = document.title;
    var known = { incidents: live.dataset.incidents, articles: live.dataset.articles }, pending = null;
    var ids = function (rootEl) {
      return Array.prototype.map.call(rootEl.querySelectorAll("article.incident"), function (a) { return a.id; });
    };
    var busy = function () { // never replace cards while a summary is regenerating or the reader is selecting text
      return document.querySelector(".regen:disabled") || String(getSelection()).length > 0;
    };
    var apply = function () {
      if (!pending) return;
      var main = document.querySelector("main"), before = new Set(ids(main));
      main.innerHTML = pending.html;
      known.incidents = pending.version;
      pending = null;
      banner = main.querySelector(".new-banner");
      banner.addEventListener("click", onBanner);
      ids(main).forEach(function (id) {
        if (!before.has(id)) document.getElementById(id).classList.add("flash", "fresh");
      });
      document.title = baseTitle;
    };
    var onBanner = function () { apply(); window.scrollTo({ top: 0, behavior: "smooth" }); };
    banner.addEventListener("click", onBanner);
    var check = async function () {
      try {
        var r = await fetch("/api/version", { cache: "no-store" });
        if (!r.ok) return;
        var v = await r.json();
        if (String(v.articles) !== known.articles) { // new articles: the side-pane charts change
          known.articles = String(v.articles);
          refreshers.forEach(function (f) { f(); });
        }
        if (v.incidents === known.incidents || (pending && pending.version === v.incidents)) return;
        var page = await fetch(location.href, { cache: "no-store" });
        if (!page.ok) return;
        var doc = new DOMParser().parseFromString(await page.text(), "text/html");
        var fresh = doc.querySelector("main"), have = new Set(ids(document.querySelector("main")));
        var added = ids(fresh).filter(function (id) { return !have.has(id); }).length;
        fresh.querySelector("#live").dataset.incidents = v.incidents;
        pending = { html: fresh.innerHTML, version: v.incidents };
        if (!document.hidden && window.scrollY < 120 && !busy()) { apply(); return; } // reader at the top: just show it
        banner.textContent = added ? "↑ 새 " + ITEM + " " + added + "건 보기" : "↻ 업데이트된 " + ITEM + " 보기";
        banner.hidden = false;
        if (added) document.title = "(" + added + ") " + baseTitle;
      } catch (err) { /* offline or server restarting: try again next round */ }
    };
    setInterval(function () { if (!document.hidden) check(); }, POLL_MS);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });
    window.addEventListener("scroll", function () { // scrolled back to the top: show what was waiting
      if (pending && window.scrollY < 120 && !busy()) apply();
    }, { passive: true });
  }
})();
