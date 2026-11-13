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

  // --- 더 보기: load the next cards in place (the link also works without JS: /?n=200 shows the first 200) ---
  document.addEventListener("click", async function (e) {
    var link = e.target.closest("a[data-more]");
    if (!link || e.ctrlKey || e.metaKey || e.shiftKey) return;
    e.preventDefault();
    if (link.getAttribute("aria-busy") === "true") return;
    var label = link.innerHTML;
    link.setAttribute("aria-busy", "true"); link.textContent = "불러오는 중…";
    try {
      var r = await fetch(link.href, { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      var fresh = new DOMParser().parseFromString(await r.text(), "text/html").querySelector("main");
      var main = document.querySelector("main"), wrap = link.closest(".more-wrap");
      var dayOf = function (sec) { return sec.querySelector("h2").textContent.trim(); };
      var mine = {};
      main.querySelectorAll("section.day").forEach(function (sec) { mine[dayOf(sec)] = sec; });
      var added = 0;
      fresh.querySelectorAll("section.day").forEach(function (sec) {
        var have = mine[dayOf(sec)];
        if (!have) { // a day not shown yet: the whole section
          var copy = document.importNode(sec, true);
          added += copy.querySelectorAll("article").length;
          main.insertBefore(copy, wrap);
          return;
        }
        sec.querySelectorAll("article").forEach(function (a) { // the rest of a day that was cut off
          if (!document.getElementById(a.id)) { have.appendChild(document.importNode(a, true)); added++; }
        });
      });
      var next = fresh.querySelector(".more-wrap");
      if (next) wrap.replaceWith(document.importNode(next, true)); else wrap.remove();
      // live updates re-render location.href: keep the larger list there
      history.replaceState(history.state, "", link.getAttribute("href"));
      if (!added && next) throw new Error("no new cards");
    } catch (err) {
      link.removeAttribute("aria-busy"); link.innerHTML = label;
      link.insertAdjacentHTML("afterend", '<span class="more-err" role="alert"> 불러오지 못했습니다. 다시 눌러 주세요.</span>');
    }
  });

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

  // --- 태그간 관계 그래프 page: tag co-occurrence network and pair table (/api/tag-graph) ---
  var graph = document.getElementById("tag-graph");
  if (graph) {
    var SVGNS = "http://www.w3.org/2000/svg";
    var gseq = 0, gcache = {}, gver = null;
    var esc = function (s) {
      return String(s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; });
    };
    var el = function (name, attrs, parent) {
      var e = document.createElementNS(SVGNS, name);
      for (var k in attrs) e.setAttribute(k, attrs[k]);
      if (parent) parent.appendChild(e);
      return e;
    };
    var fmtP = function (p) { return p < 0.001 ? p.toExponential(1) : p.toFixed(3); };
    var isSig = function (e) { return e.q < 0.05; };
    var tagHref = function (n) { return "/?" + (n.ni === 0 ? "section=intel&" : "") + "q=" + encodeURIComponent(n.id); };
    var cardHref = function (c) { return c.link; };

    // a small deterministic force layout: nodes repel, links pull to a rest length, gravity keeps them centred
    var place = function (nodes, links, W, H, rad) {
      var n = nodes.length;
      nodes.forEach(function (d, i) {
        var a = 2 * Math.PI * i / Math.max(n, 1);
        d.x = W / 2 + W * 0.3 * Math.cos(a); d.y = H / 2 + H * 0.3 * Math.sin(a); d.vx = 0; d.vy = 0;
      });
      var gx = 0.032 * H / (W + H), gy = 0.032 * W / (W + H); // pull harder along the short side of the box
      for (var it = 0, N = 450; it < N; it++) {
        var alpha = 1 - it / N;
        for (var i = 0; i < n; i++) for (var j = i + 1; j < n; j++) {
          var a = nodes[i], b = nodes[j], dx = b.x - a.x, dy = b.y - a.y, d2 = Math.max(dx * dx + dy * dy, 1), d = Math.sqrt(d2);
          var f = 3200 / d2, gap = rad(a) + rad(b) + 34; // charge, plus a hard push when labels would touch
          if (d < gap) f += (gap - d) * 0.5 / d;
          f *= alpha; a.vx -= dx * f / d; a.vy -= dy * f / d; b.vx += dx * f / d; b.vy += dy * f / d;
        }
        links.forEach(function (l) {
          var dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
          var f = (d - (W < 600 ? 50 : 70) - 30 / l.e.k) * 0.06 * alpha;
          l.s.vx += dx / d * f; l.s.vy += dy / d * f; l.t.vx -= dx / d * f; l.t.vy -= dy / d * f;
        });
        nodes.forEach(function (d) {
          d.vx += (W / 2 - d.x) * gx * alpha; d.vy += (H / 2 - d.y) * gy * alpha;
          d.x += d.vx; d.y += d.vy; d.vx *= 0.55; d.vy *= 0.55;
        });
      }
      // fit the result into the box (labels need room below each circle)
      var x0 = Math.min.apply(null, nodes.map(function (d) { return d.x; })), x1 = Math.max.apply(null, nodes.map(function (d) { return d.x; }));
      var y0 = Math.min.apply(null, nodes.map(function (d) { return d.y; })), y1 = Math.max.apply(null, nodes.map(function (d) { return d.y; }));
      var pad = W < 600 ? 90 : 140, s = Math.min((W - pad) / Math.max(x1 - x0, 1), (H - 80) / Math.max(y1 - y0, 1), 1.6);
      nodes.forEach(function (d) {
        d.x = W / 2 + (d.x - (x0 + x1) / 2) * s; d.y = H / 2 - 8 + (d.y - (y0 + y1) / 2) * s;
      });
    };

    var tip = graph.querySelector(".graph-tip"), netPanel = graph.querySelector(".net-panel");
    var showTip = function (ev, html) {
      tip.innerHTML = html; tip.hidden = false;
      var b = netPanel.getBoundingClientRect(), x = ev.clientX - b.left + 12, y = ev.clientY - b.top + 12;
      if (x > b.width - 290) x = Math.max(4, ev.clientX - b.left - 290);
      tip.style.left = x + "px"; tip.style.top = y + "px";
    };
    var hideTip = function () {
      tip.hidden = true;
      graph.querySelectorAll("tr.hl").forEach(function (t) { t.classList.remove("hl"); });
    };

    // --- node filter: four stops that keep all, ½, ¼ and ⅛ of the linked tags. Each stop is a 글타래 threshold
    // worked out from the data (tag counts are lopsided, so evenly spaced thresholds would jump from everything to
    // almost nothing). The choice is saved as a stop, so it keeps its meaning as tags accumulate. ---
    var STOPS = [{ share: 1, label: "전체" }, { share: 1 / 2, label: "½" }, { share: 1 / 4, label: "¼" }, { share: 1 / 8, label: "⅛" }];
    var filterBox = graph.querySelector(".graph-filter"), slider = graph.querySelector("#graph-min");
    var minOut = graph.querySelector(".graph-min-val"), autoBtn = graph.querySelector(".graph-auto");
    var shownOut = graph.querySelector(".graph-shown"), ticks = graph.querySelector(".graph-ticks"), chosen = null;
    try { var saved = localStorage.getItem("graphStop"); chosen = saved === null ? null : Number(saved); } catch (err) {}
    if (!(chosen >= 0 && chosen < STOPS.length)) chosen = null;
    var visible = function (D, min) { // tags at or above min, links whose two tags both remain, and no lone tags
      var keep = {};
      D.nodes.forEach(function (n) { if (n.n >= min) keep[n.id] = true; });
      var edges = D.edges.filter(function (e) { return keep[e.a] && keep[e.b]; }), linked = {};
      edges.forEach(function (e) { linked[e.a] = linked[e.b] = true; });
      return { nodes: D.nodes.filter(function (n) { return linked[n.id]; }), edges: edges };
    };
    var stopsFor = function (D) { // [{label, min, tags}] with distinct thresholds and something left to show
      var total = D.nodes.length, maxN = Math.max.apply(null, D.nodes.map(function (n) { return n.n; })), out = [];
      var counts = [];                                               // tags left at each threshold 2..maxN
      for (var t = 2; t <= maxN; t++) counts.push({ min: t, tags: visible(D, t).nodes.length });
      counts = counts.filter(function (x) { return x.tags > 0; });  // never a stop that empties the graph
      STOPS.forEach(function (st) {                                  // the threshold whose tag count is closest
        var goal = st.share * total, best = counts[0];
        counts.forEach(function (x) { if (Math.abs(x.tags - goal) < Math.abs(best.tags - goal)) best = x; });
        if (!out.length || best.min > out[out.length - 1].min) out.push({ label: st.label, min: best.min, tags: best.tags });
      });
      return out;
    };
    var setupFilter = function (D, W) {
      var stops = stopsFor(D);
      var auto = Math.min(W < 600 ? 3 : 2, stops.length - 1);        // phone ⅛, wider screens ¼
      var idx = chosen === null ? auto : Math.min(chosen, stops.length - 1), st = stops[idx];
      filterBox.hidden = stops.length < 2;
      slider.max = stops.length - 1; slider.value = idx;
      slider.setAttribute("aria-valuetext", st.label + ", 글타래 " + st.min + "개 이상");
      minOut.textContent = st.min;
      ticks.innerHTML = stops.map(function (x, i) {
        return '<button type="button" data-stop="' + i + '"' + (i === idx ? ' aria-current="true"' : "") + ">" + x.label +
          " <small>" + x.min + "+</small></button>";
      }).join("");
      autoBtn.setAttribute("aria-pressed", String(chosen === null));
      var v = visible(D, st.min);
      shownOut.textContent = "태그 " + v.nodes.length + "/" + D.nodes.length + "개 · 연결 " + v.edges.length + "/" + D.edges.length + "개 표시";
      return st.min;
    };
    var redrawSoon = (function () {
      var timer = 0; // dragging the slider fires many input events: lay out once it pauses
      return function () { clearTimeout(timer); timer = setTimeout(function () { if (gcache[gscope]) drawNet(gcache[gscope]); }, 40); };
    })();
    var pick = function (i) {
      chosen = i;
      try { if (i === null) localStorage.removeItem("graphStop"); else localStorage.setItem("graphStop", String(i)); } catch (err) {}
      redrawSoon();
    };
    slider.addEventListener("input", function () { pick(Number(slider.value)); });
    ticks.addEventListener("click", function (e) {    // the labels under the slider are buttons too
      var b = e.target.closest("[data-stop]");
      if (b) pick(Number(b.dataset.stop));
    });
    autoBtn.addEventListener("click", function () { pick(null); });

    var HUBS = 5; // how many of the most-used tags get their own colour
    var drawNet = function (D) {
      // draw at the panel's real width so labels keep their size; phones get a taller box
      var svg = graph.querySelector("svg.net"), W = Math.round(Math.min(900, Math.max(320, svg.clientWidth || 900)));
      var H = W < 600 ? Math.round(W * 1.35) : 560;
      svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      svg.innerHTML = "";
      if (!D.nodes.length) {
        filterBox.hidden = true;
        el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "empty-txt" }, svg).textContent =
          D.minsup + "개 이상의 글타래에 함께 붙은 태그 쌍이 아직 없습니다";
        return;
      }
      var maxN = Math.max.apply(null, D.nodes.map(function (n) { return n.n; }));
      var maxK = Math.max.apply(null, [2].concat(D.edges.map(function (e) { return e.k; })));
      var min = setupFilter(D, W);
      var shown = visible(D, min), byId = {};
      var nodes = shown.nodes.map(function (n) { var d = Object.assign({}, n); byId[d.id] = d; return d; });
      var links = shown.edges.map(function (e) { return { s: byId[e.a], t: byId[e.b], e: e, i: D.edges.indexOf(e) }; });
      if (!nodes.length) {
        el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "empty-txt" }, svg).textContent =
          "글타래 " + min + "개 이상인 태그끼리 이어진 연결이 없습니다. 기준을 낮춰 보세요";
        return;
      }
      var rad = function (d) { return 6 + 16 * Math.sqrt((d.n - 1) / Math.max(maxN - 1, 1)); };
      var wid = function (k) { return 1.5 + 5.5 * (k - 2) / Math.max(maxK - 2, 1); };
      place(nodes, links, W, H, rad);
      // the most-used tags get their own colour, and so do their links. Ranked over all tags, not the visible ones,
      // so a tag keeps its colour whichever stop is chosen (colour follows the tag, not its rank on screen).
      var hubs = {};
      D.nodes.slice().sort(function (a, b) { return b.n - a.n || (a.id < b.id ? -1 : 1); }).slice(0, HUBS)
        .forEach(function (n, i) { hubs[n.id] = { slot: i + 1, n: n.n }; });
      var hubOf = function (l) { // a link takes the colour of its more-used hub end
        var a = hubs[l.e.a], b = hubs[l.e.b];
        return a && b ? (a.n >= b.n ? a : b) : a || b;
      };
      var g = el("g", {}, svg);
      links.slice().sort(function (x, y) { return (hubOf(x) ? 1 : 0) - (hubOf(y) ? 1 : 0); }).forEach(function (l) {
        var h = hubOf(l); // hub links on top of the others
        var line = el("line", { x1: l.s.x, y1: l.s.y, x2: l.t.x, y2: l.t.y,
                                class: "edge" + (isSig(l.e) ? " sig" : "") + (h ? " hub" : ""),
                                "stroke-width": wid(l.e.k), "stroke-dasharray": l.e.g < l.e.k ? "5 4" : "none" }, g);
        if (h) line.style.stroke = "var(--hub-" + h.slot + ")";
      });
      graph.querySelector(".graph-hubs").innerHTML =
        '<span class="graph-hubs-title" title="이 태그의 연결도 같은 색이며, 흐린 선은 유의하지 않은 연결이다">많이 쓰인 태그 ' +
        '<small>(연결도 같은 색 · 흐리면 유의하지 않음)</small></span>' +
        Object.keys(hubs).filter(function (id) { return byId[id]; }).map(function (id) {
          return '<span class="hub-chip"><i style="background:var(--hub-' + hubs[id].slot + ')"></i>#' + esc(id) +
            " <small>" + hubs[id].n + "</small></span>";
        }).join("");
      links.forEach(function (l) { // wide invisible hit targets
        var hit = el("line", { x1: l.s.x, y1: l.s.y, x2: l.t.x, y2: l.t.y, class: "edge-hit" }, g);
        hit.addEventListener("mousemove", function (ev) {
          var row = document.getElementById("gp-" + l.i);
          if (row) row.classList.add("hl");
          showTip(ev, "<b>" + esc(l.e.a) + " ↔ " + esc(l.e.b) + "</b><br>함께 " + l.e.k + "건 · 향상도 " + l.e.lift.toFixed(1) +
            " · q " + fmtP(l.e.q) + "<br>" + l.e.cards.slice(0, 3).map(function (c) { return "· " + esc(c.title); }).join("<br>") +
            (l.e.cards.length > 3 ? "<br>· 외 " + (l.e.cards.length - 3) + "건" : ""));
        });
        hit.addEventListener("mouseleave", hideTip);
        hit.addEventListener("click", function () {
          var row = document.getElementById("gp-" + l.i);
          if (row) { row.scrollIntoView({ behavior: "smooth", block: "center" }); row.classList.add("flash"); }
        });
      });
      nodes.forEach(function (d) {
        var a = el("a", { href: tagHref(d), class: "node" + (hubs[d.id] ? " hub" : ""),
                          transform: "translate(" + d.x.toFixed(1) + "," + d.y.toFixed(1) + ")" }, g);
        var c = el("circle", { r: rad(d) }, a);
        if (hubs[d.id]) c.style.setProperty("--hub", "var(--hub-" + hubs[d.id].slot + ")");
        var t = el("text", { y: rad(d) + 14, "text-anchor": "middle", class: d.n >= maxN * 0.5 ? "big" : "" }, a);
        t.textContent = "#" + d.id;
        a.addEventListener("mousemove", function (ev) { showTip(ev, "<b>#" + esc(d.id) + "</b><br>글타래 " + d.n + "개 · 누르면 목록 보기"); });
        a.addEventListener("mouseleave", hideTip);
      });
    };

    var fillText = function (D) {
      graph.querySelectorAll(".minsup").forEach(function (e) { e.textContent = D.minsup; });
      var tbody = graph.querySelector(".graph-table tbody");
      tbody.innerHTML = D.edges.map(function (e, i) {
        return '<tr id="gp-' + i + '"><td><div class="pair">' + esc(e.a) + "<em>↔</em>" + esc(e.b) + "</div>" +
          "<details><summary>글타래 " + e.cards.length + "개</summary><ul>" + e.cards.map(function (c) {
            return '<li><a href="' + esc(cardHref(c)) + '"' + (c.sec === "intel" ? ' target="_blank" rel="noopener"' : "") + ">" + esc(c.title) +
              '</a> <span class="sec">' + (c.sec === "intel" ? "인텔" : "사고") + "</span></li>";
          }).join("") + "</ul></details></td>" +
          '<td class="num">' + e.k + '</td><td class="num">' + e.fa + '</td><td class="num">' + e.fb + '</td><td class="num">' + e.lift.toFixed(1) +
          '</td><td class="num">' + fmtP(e.p) + '</td><td class="num">' + fmtP(e.q) + "</td><td>" +
          (isSig(e) ? '<span class="pill yes">✓ 연결</span>' : '<span class="pill no">– 불충분</span>') +
          '</td><td class="num">' + e.g + "/" + e.k + "</td></tr>";
      }).join("") || '<tr><td colspan="9" class="empty-row">검정할 태그 쌍이 아직 없습니다</td></tr>';
      var notes = [
        "<b>" + D.npairs + "쌍 중 " + D.single + "쌍은 글타래 하나에만 함께 나온다.</b> 한 번 겹친 것은 연결의 근거가 되지 않아 검정하지 않았다. 태그 " + D.ntags + "개 중 " +
          (D.ntags - D.reused) + "개는 한 번만 쓰였으며, 대부분 피해 기관·공격 그룹·CVE 이름이다.",
        "<b>같은 사건의 중복 보도가 연결을 부풀릴 수 있다.</b> 여러 매체가 같은 사건을 다루면 글타래가 따로 생길 수 있어, 강한 연결이 일반적인 관계보다 한 사건의 보도량을 반영할 수 있다. 표에서 글타래 목록을 열어 확인할 것.",
        "<b>점선 연결은 원문 근거가 부족하다.</b> 어떤 원문에서 태그를 찾지 못했다는 뜻이다. 본문이 잘려 수집됐을 수도 있지만 모델의 추론일 수도 있다.",
        "<b>원문 확인은 글자 그대로 찾는다.</b> 태그 사용 " + D.tt + "건 중 " + D.tg + "건(" + (D.tt ? Math.round(D.tg / D.tt * 100) : 0) +
          "%)이 원문 또는 별칭 목록과 일치했다. 패치관리·AI보안 같은 추상적 태그는 \"patch\", \"AI\" 같은 별칭으로만 통과해 이 확인이 관대하다.",
        "<b>글타래 " + D.N + "개로는 검정력이 낮다.</b> 드문 태그끼리는 2개만 겹쳐도 유의하지만, 흔한 태그끼리는 훨씬 많이 겹쳐야 한다. 데이터가 쌓일수록 결과가 달라질 수 있다.",
      ];
      graph.querySelector(".graph-notes").innerHTML = notes.map(function (n) { return "<li>" + n + "</li>"; }).join("");
    };

    var gscope = "all"; // one graph over every 글타래 (incident and intel); /api/tag-graph also takes scope=incident|intel
    var load = async function (force) {
      var my = ++gseq;
      try {
        if (force || !gcache[gscope]) {
          var r = await fetch("/api/tag-graph?scope=" + gscope, { cache: "no-store" });
          if (!r.ok) throw new Error("태그간 관계를 불러오지 못했습니다 (" + r.status + ")");
          gcache[gscope] = await r.json();
        }
        if (my !== gseq) return;
        fillText(gcache[gscope]); drawNet(gcache[gscope]);
      } catch (err) {
        if (my !== gseq) return;
        var svg = graph.querySelector("svg.net");
        svg.innerHTML = "";
        el("text", { x: 450, y: 280, "text-anchor": "middle", class: "empty-txt" }, svg).textContent = err.message;
      }
    };
    load();
    var gtimer, gwidth = window.innerWidth;
    window.addEventListener("resize", function () { // phone rotation or window resize: lay the network out again
      clearTimeout(gtimer);
      gtimer = setTimeout(function () {
        if (window.innerWidth !== gwidth && gcache[gscope]) { gwidth = window.innerWidth; drawNet(gcache[gscope]); }
      }, 200);
    });

    // new 글타래 change the graph: re-fetch when /api/version moves ([server].refresh_seconds; 0 = off)
    var gsec = Number(graph.dataset.refresh);
    if (gsec > 0) {
      var gcheck = async function () {
        try {
          var r = await fetch("/api/version", { cache: "no-store" });
          if (!r.ok) return;
          var v = (await r.json()).incidents;
          if (gver !== null && v !== gver) { gcache = {}; load(true); }
          gver = v;
        } catch (err) { /* offline: try next round */ }
      };
      gcheck();
      setInterval(function () { if (!document.hidden) gcheck(); }, gsec * 1000);
    }
  }
})();
