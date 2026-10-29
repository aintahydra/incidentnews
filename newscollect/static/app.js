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
})();
