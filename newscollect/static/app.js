(function () {
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
  // --- keyword cloud (list page only) ---
  var box = document.querySelector(".cloud-box");
  if (box) {
    var note = document.querySelector(".cloud-note"), cache = {}, current = null, seq = 0;
    var NAMES = { day: "오늘", week: "최근 7일", month: "최근 30일", year: "최근 1년" };

    var layout = function (data) {
      box.innerHTML = "";
      var words = data.words;
      if (!words.length) { box.classList.add("empty-cloud"); return; }
      box.classList.remove("empty-cloud");
      var W = box.clientWidth, H = box.clientHeight;
      var max = words[0].count, min = words[words.length - 1].count;
      var big = Math.min(46, Math.max(26, W / 14)), small = 13, placed = [];
      words.forEach(function (w) {
        var t = max === min ? 0.6 : Math.log(w.count / min) / Math.log(max / min);
        var a = document.createElement("a");
        a.href = "/?q=" + encodeURIComponent(w.word);
        a.textContent = w.word;
        a.title = w.word + " · 사고 " + w.count + "건";
        a.style.fontSize = (small + (big - small) * t).toFixed(1) + "px";
        a.className = "w" + (t > 0.66 ? 3 : t > 0.33 ? 2 : 1);
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
          var r = await fetch("/api/wordcloud?period=" + period, { cache: "no-store" });
          if (!r.ok) throw new Error("키워드를 불러오지 못했습니다 (" + r.status + ")");
          cache[period] = await r.json();
        }
        if (my !== seq) return; // a newer click won
        var d = cache[period];
        layout(d);
        note.textContent = d.incidents
          ? NAMES[period] + " 사고 " + d.incidents + "건 기준 · 단어를 누르면 검색합니다"
          : NAMES[period] + " 동안 수집된 사고가 없습니다";
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
    var saved = null;
    try { saved = localStorage.getItem("cloudPeriod"); } catch (err) {}
    show(NAMES[saved] ? saved : "week");
  }
})();
