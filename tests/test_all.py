import threading
import json
import re
import httpx
import pytest
from fastapi.testclient import TestClient

from newscollect import collector, db, dedupe, extractor, llm
from newscollect.config import load_config
from newscollect.web import create_app

TOML = """
[llm]
endpoint = "127.0.0.1:11434"
model = "solar:10.7b"
[collector]
database = "{db}"
[[sources]]
name = "A"
url = "http://x/a.xml"
[[sources]]
name = "B"
url = "http://x/b.xml"
enabled = false
"""


def write_cfg(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(TOML.format(db=tmp_path / "t.db"))
    return p


def test_config_sources_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    cfg = load_config(write_cfg(tmp_path))
    assert [s.name for s in cfg.sources] == ["A", "B"] and not cfg.sources[1].enabled
    assert cfg.llm_base_url == "http://127.0.0.1:11434"
    monkeypatch.setenv("OLLAMA_HOST", "10.0.2.2:11434")
    assert cfg.llm_base_url == "http://10.0.2.2:11434"
    monkeypatch.setenv("OLLAMA_HOST", "10.0.2.2")
    assert cfg.llm_base_url == "http://10.0.2.2:11434"


def test_config_edit_applies(tmp_path):
    p = write_cfg(tmp_path)
    p.write_text(p.read_text() + '\n[[sources]]\nname="C"\nurl="http://x/c.xml"\n')
    assert len(load_config(p).sources) == 3


def test_parse_llm_json_and_bad_date():
    ex = extractor.parse_llm_json('x {"is_incident":true,"title":"t","occurred_at":"어제","summary":"s"} y', "2026-10-01")
    assert ex.is_incident and ex.occurred_at == "2026-10-01"


def test_heuristic():
    a = {"title": "A사 랜섬웨어 공격", "body": "A사가 공격받았다. 피해가 크다.", "published_at": "2026-10-01"}
    assert extractor.extract_heuristic(a).is_incident
    assert not extractor.extract_heuristic({"title": "신제품 출시", "body": "행사", "published_at": "2026-10-01"}).is_incident


def ex(title, victim, attacker="", d="2026-10-10"):
    return extractor.Extraction(True, title, d, attacker, victim, "", "해킹", "요약 " + title)


def seed_article(conn, url):
    return conn.execute("INSERT INTO articles(source,url,title) VALUES('s',?,'t')", (url,)).lastrowid


def test_dedupe_same_victim_merges_and_different_not():
    conn = db.connect(":memory:")
    a = dedupe.upsert_incident(conn, ex("SK텔레콤 유심 해킹", "SK텔레콤"), seed_article(conn, "u1"))
    b = dedupe.upsert_incident(conn, ex("SKT 가입자 정보 유출 사태 확산", "(주)SK텔레콤", d="2026-10-12"), seed_article(conn, "u2"))
    c = dedupe.upsert_incident(conn, ex("롯데카드 해킹", "롯데카드"), seed_article(conn, "u3"))
    d = dedupe.upsert_incident(conn, ex("SK텔레콤 유심 해킹", "SK텔레콤", d="2026-12-01"), seed_article(conn, "u4"))
    assert a == b and c != a and d != a
    assert conn.execute("SELECT COUNT(*) FROM incident_articles WHERE incident_id=?", (a,)).fetchone()[0] == 2


def test_dedupe_title_similarity_without_victim():
    conn = db.connect(":memory:")
    a = dedupe.upsert_incident(conn, ex("AI 활용 랜섬웨어 조직 n0n 14개국 공격", ""), seed_article(conn, "u1"))
    b = dedupe.upsert_incident(conn, ex("AI 활용 랜섬웨어 조직 n0n 14개국 23개 조직 공격", ""), seed_article(conn, "u2"))
    assert a == b


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>A사 랜섬웨어 공격으로 서버 마비</title><link>http://x/1</link><description>A사가 랜섬웨어 공격을 받아 서버가 마비됐다.</description><pubDate>Tue, 27 Oct 2026 10:00:00 +0900</pubDate></item>
<item><title>신제품 발표 행사</title><link>http://x/2</link><description>행사 소개</description><pubDate>Tue, 27 Oct 2026 11:00:00 +0900</pubDate></item>
</channel></rss>"""


def test_collect_end_to_end_mocked(tmp_path, monkeypatch):
    cfg = load_config(write_cfg(tmp_path))
    cfg.max_age_days = 10000
    def handler(req):
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            assert body["model"] == "solar:10.7b"
            incident = "랜섬웨어" in body["messages"][1]["content"]
            out = {"is_incident": incident, "title": "A사 랜섬웨어 피해", "occurred_at": "2026-10-27",
                   "attacker": "미상", "victim": "A사", "damage": "서버 마비", "category": "랜섬웨어",
                   "summary": "10월 27일 A사가 랜섬웨어 공격을 받아 서버가 마비됐다."}
            return httpx.Response(200, json={"message": {"content": json.dumps(out)}})
        if req.url.host == "x" and req.url.path.endswith(".xml"):
            return httpx.Response(200, content=RSS.encode())
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    real = httpx.Client
    monkeypatch.setattr(collector.httpx, "Client", lambda **kw: real(transport=transport, **kw))
    conn = db.connect(cfg.database)
    s1 = collector.collect_once(conn, cfg)
    assert s1["new"] == 2 and s1["incidents"] == 1 and s1["skipped"] == 1
    s2 = collector.collect_once(conn, cfg)  # rerun: no duplicates
    assert s2["new"] == 0 and conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1


def test_web(tmp_path):
    p = write_cfg(tmp_path)
    cfg = load_config(p)
    conn = db.connect(cfg.database)
    i = dedupe.upsert_incident(conn, ex("A사 해킹", "A사", "B그룹"), seed_article(conn, "http://u/1"))
    conn.commit()
    c = TestClient(create_app(p, run_collector=False))
    r = c.get("/")
    assert r.status_code == 200 and "A사 해킹" in r.text and "2026-10-10" in r.text
    assert c.get(f"/incidents/{i}").status_code == 200
    assert c.get("/incidents/999").status_code == 404
    assert c.get("/api/incidents").json()[0]["victim"] == "A사"
    assert c.get("/healthz").json()["incidents"] == 1


def test_heuristic_rejects_vulnerability_advisory():
    a = {"title": "국내 대학 5곳, PHP2셸 노출됐나... 취약 가능성 커 패치 시급", "body": "공격에 악용될 수 있다.", "published_at": "2026-10-01"}
    assert not extractor.extract_heuristic(a).is_incident


def llm_client(answers):
    """MockTransport returning the given JSON answers in order (last one repeats); records requests."""
    calls = []
    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        out = answers[min(len(calls) - 1, len(answers) - 1)]
        return httpx.Response(200, json={"message": {"content": json.dumps(out)}})
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


GOOD = {"is_incident": True, "title": "빗썸 해킹", "summary": "빗썸이 해킹당했다.", "victim": "빗썸", "occurred_at": "2026-10-01"}
ART = {"title": "빗썸 해킹 사고", "body": "빗썸에서 개인정보가 유출됐다.", "published_at": "2026-10-01"}


def test_llm_english_answer_retried_with_higher_temperature_then_fails():
    english = {**GOOD, "summary": "A was hacked."}
    client, calls = llm_client([english])
    with pytest.raises(ValueError):
        extractor.extract_llm(llm.Ollama(client, "http://x"), "m", ART)
    assert [c["options"]["temperature"] for c in calls] == [0, 0.3, 0.6]


def test_llm_garbled_name_is_retried_and_recovers():
    garbled = {**GOOD, "title": "비티코 해킹", "victim": "비티코", "summary": "비티코가 해킹당했다."}
    client, calls = llm_client([garbled, GOOD])
    ex = extractor.extract_llm(llm.Ollama(client, "http://x"), "m", ART)
    assert ex.victim == "빗썸" and len(calls) == 2
    assert "비티코" in calls[1]["messages"][1]["content"]       # retry hint names the bad token


def test_llm_garbled_name_always_rejected():
    garbled = {**GOOD, "victim": "비티코", "summary": "비티코가 해킹당했다."}
    client, calls = llm_client([garbled])
    with pytest.raises(ValueError, match="기사 본문에 없는 이름"):
        extractor.extract_llm(llm.Ollama(client, "http://x"), "m", ART)


def test_grounding_accepts_partial_and_ascii_names():
    ex_ = extractor.Extraction(True, "t", "2026-10-01", victim="인도국영은행(SBI)", attacker="Siriusim", summary="요약입니다")
    assert extractor.ungrounded_names(ex_, "인도 최대 국영은행 SBI ... 해커 siriusim") == []
    assert extractor.ungrounded_names(ex_, "전혀 다른 기사") != []


def test_summary_clipped():
    long = "문장이다. " * 200
    ex = extractor.parse_llm_json(json.dumps({"is_incident": True, "title": "t", "summary": long}), "2026-10-01")
    assert len(ex.summary) <= 450 and ex.summary.endswith(".")


def test_prefilter_skips_vulnerability_without_llm():
    class C: llm_enabled = True
    a = {"title": "AI 해커, 산업제어 소프트웨어 치명적 취약점 4개 발견", "body": "공격에 악용될 수 있다.", "published_at": "2026-10-01"}
    assert not extractor.extract(None, C(), a).is_incident


# ---- search / themes / regenerate ----
from newscollect import regen  # noqa: E402


def make_client(tmp_path, *incidents):
    p = write_cfg(tmp_path)
    conn = db.connect(load_config(p).database)
    ids = []
    for n, (title, victim, attacker, d, art_title) in enumerate(incidents):
        a = conn.execute("INSERT INTO articles(source,url,title,body) VALUES('S',?,?,'기존 본문')",
                         (f"http://u/{n}", art_title)).lastrowid
        ids.append(dedupe.upsert_incident(conn, ex(title, victim, attacker, d), a))
    conn.commit()
    return TestClient(create_app(p, run_collector=False)), ids


def test_search(tmp_path):
    c, _ = make_client(tmp_path,
                       ("랜섬웨어 공격", "롯데카드", "", "2026-10-10", "롯데카드 서버 마비"),
                       ("100% 데이터 유출", "삼성전자", "라자루스", "2026-10-20", "삼성 기사"))
    titles = lambda q: [r["victim"] for r in c.get("/api/incidents", params={"q": q}).json()]
    assert titles("롯데카드") == ["롯데카드"]
    assert titles("라자루스") == ["삼성전자"]            # attacker field
    assert titles("서버 마비") == ["롯데카드"]           # linked article title, AND of two terms
    assert titles("롯데카드 삼성") == []                  # AND semantics
    assert len(titles("")) == 2
    assert titles("%") == ["삼성전자"]                    # wildcard is escaped: only the title with a literal '%'
    assert titles("_") == []
    page = c.get("/", params={"q": "라자루스"}).text
    assert "검색 결과 1건" in page and "삼성전자" in page and "롯데카드 해킹" not in page
    assert "검색 결과가 없습니다" in c.get("/", params={"q": "없는키워드"}).text


def test_theme_buttons_and_search_box_rendered(tmp_path):
    c, _ = make_client(tmp_path, ("A", "A사", "", "2026-10-10", "t"))
    html = c.get("/").text
    assert 'data-theme-choice="light"' in html and 'data-theme-choice="dark"' in html
    assert 'name="q"' in html and "/static/app.js" in html
    css = c.get("/static/style.css").text
    assert "[data-theme=light]" in css and "[data-theme=dark]" in css
    assert "data-theme-choice" in c.get("/static/app.js").text or "themeChoice" in c.get("/static/app.js").text
    assert 'class="regen"' in html


GOOD_SUMMARY = ("2026년 10월 10일 A사가 B그룹의 공격을 받아 서버가 마비됐다. "
                "이 사고로 고객 3만 명의 개인정보가 유출된 것으로 확인됐다. A사는 원인을 조사하고 피해 고객에게 안내하고 있다.")


def fake_llm(monkeypatch, summaries=(GOOD_SUMMARY,), victim="A사", title="새 제목", incident=True):
    """JSON-mode calls return fields; plain calls return the next of `summaries` (last repeats)."""
    seen = {"json": [], "plain": []}
    def handler(req):
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            if "format" in body:
                seen["json"].append(body)
                out = {"is_incident": incident, "title": title, "occurred_at": "2030-01-01", "attacker": "",
                       "victim": victim, "damage": "서버 마비", "category": "랜섬웨어", "summary": "무시되는 요약입니다 요약입니다."}
                return httpx.Response(200, json={"message": {"content": json.dumps(out)}})
            seen["plain"].append(body)
            text = summaries[min(len(seen["plain"]) - 1, len(summaries) - 1)]
            return httpx.Response(200, json={"message": {"content": text}})
        return httpx.Response(404)  # article fetch fails -> stored body is used
    real = httpx.Client
    monkeypatch.setattr(regen.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


def test_regenerate_writes_prose_summary_keeps_date_and_nonempty_fields(tmp_path, monkeypatch):
    c, (i,) = make_client(tmp_path, ("원래 제목", "A사", "B그룹", "2026-10-10", "A사 B그룹 기사 제목"))
    seen = fake_llm(monkeypatch)
    r = c.post(f"/incidents/{i}/regenerate")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["summary"] == GOOD_SUMMARY                  # comes from the dedicated prose call, not the JSON one
    assert d["title"] == "새 제목"
    assert d["occurred_at"] == "2026-10-10"              # date untouched
    assert d["attacker"] == "B그룹"                       # LLM returned "" -> keep old value
    assert seen["json"][0]["options"]["temperature"] > 0
    prompt = seen["plain"][0]["messages"][1]["content"]
    assert "기존 본문" in prompt and "피해 대상: A사" in prompt and "공격자: B그룹" in prompt
    assert "format" not in seen["plain"][0]
    assert "새 제목" in c.get(f"/incidents/{i}/card").text
    assert c.get("/api/incidents").json()[0]["summary"] == GOOD_SUMMARY


def test_regenerate_retries_bad_summary_style(tmp_path, monkeypatch):
    c, (i,) = make_client(tmp_path, ("원래 제목", "A사", "", "2026-10-10", "A사 기사"))
    memo = "A사 서버 마비. 고객정보 유출 추정. 조사 착수."          # telegraphic notes
    seen = fake_llm(monkeypatch, summaries=(memo, GOOD_SUMMARY))
    assert c.post(f"/incidents/{i}/regenerate").json()["summary"] == GOOD_SUMMARY
    assert len(seen["plain"]) == 2 and "이전 답변의 문제" in seen["plain"][1]["messages"][1]["content"]
    assert seen["plain"][1]["options"]["temperature"] >= 0.4


def test_regenerate_failure_keeps_old_summary_and_cooldown(tmp_path, monkeypatch):
    c, (i,) = make_client(tmp_path, ("원래 제목", "A사", "", "2026-10-10", "A사 기사"))
    old = c.get("/api/incidents").json()[0]["summary"]
    fake_llm(monkeypatch, summaries=("English only summary",))     # never acceptable
    r = c.post(f"/incidents/{i}/regenerate")
    assert r.status_code == 502 and "요약 생성 실패" in r.json()["detail"]
    assert c.get("/api/incidents").json()[0]["summary"] == old
    assert c.post(f"/incidents/{i}/regenerate").status_code == 429   # cooldown after an attempt
    assert c.post("/incidents/999/regenerate").status_code == 404


def test_regenerate_repairs_garbled_name_from_article(tmp_path, monkeypatch):
    """The model garbles the victim name every time -> stored garbled name is dropped, headline restored."""
    c, (i,) = make_client(tmp_path, ("브이스비스 해킹", "브이스비스", "", "2026-10-10", "빗썸 해킹 사고 개인정보 유출"))
    summary = ("2026년 10월 10일 빗썸에서 외부 침입으로 개인정보가 유출됐다. 피해 고객은 3만 명으로 확인됐다. "
               "회사는 원인을 조사하고 있다.")
    fake_llm(monkeypatch, summaries=(summary,), victim="비티코", title="비티코 해킹")   # JSON step keeps failing grounding
    r = c.post(f"/incidents/{i}/regenerate")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["victim"] == "" and d["title"] == "빗썸 해킹 사고 개인정보 유출"
    assert "브이스비스" not in d["summary"] and d["summary"] == summary


def test_regenerate_rejects_summary_with_garbled_name(tmp_path, monkeypatch):
    c, (i,) = make_client(tmp_path, ("브이스비스 해킹", "브이스비스", "", "2026-10-10", "빗썸 해킹 사고 개인정보 유출"))
    bad = ("2026년 10월 10일 브이스비스에서 외부 침입으로 개인정보가 유출됐다. 피해 고객은 3만 명으로 확인됐다. "
           "회사는 원인을 조사하고 있다.")
    fake_llm(monkeypatch, summaries=(bad,), victim="비티코")
    assert c.post(f"/incidents/{i}/regenerate").status_code == 502


def test_regenerate_not_incident_keeps_summary(tmp_path, monkeypatch):
    c, (i,) = make_client(tmp_path, ("원래 제목", "A사", "", "2026-10-10", "A사 기사"))
    old = c.get("/api/incidents").json()[0]["summary"]
    fake_llm(monkeypatch, incident=False)
    r = c.post(f"/incidents/{i}/regenerate")
    assert r.status_code == 502 and "사고로 판단하지 않아" in r.json()["detail"]
    assert c.get("/api/incidents").json()[0]["summary"] == old


def test_regenerate_busy_returns_429(tmp_path, monkeypatch):
    c, (i,) = make_client(tmp_path, ("원래 제목", "A사", "", "2026-10-10", "A사 기사"))
    gate, started = threading.Event(), threading.Event()
    def slow(conn, lock, cfg, inc_id):
        started.set(); gate.wait(5)
        return {"id": inc_id}
    monkeypatch.setattr(regen, "regenerate", slow)
    t = threading.Thread(target=lambda: c.post(f"/incidents/{i}/regenerate"))
    t.start(); started.wait(5)
    try:
        assert c.post(f"/incidents/{i}/regenerate").status_code == 429  # lock held by the in-flight request
    finally:
        gate.set(); t.join()


def test_num_ctx_sent_and_transient_500_retried():
    calls = []
    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"message": {"content": json.dumps(GOOD)}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    ex = extractor.extract_llm(llm.Ollama(client, "http://x", num_ctx=6000), "m", ART)
    assert ex.victim == "빗썸" and len(calls) == 2
    assert all(c["options"]["num_ctx"] == 6000 for c in calls)


def test_num_ctx_config_default_and_override(tmp_path):
    assert load_config(write_cfg(tmp_path)).llm_num_ctx == 8192
    p = tmp_path / "c2.toml"
    p.write_text("[llm]\nnum_ctx = 16384\n")
    assert load_config(p).llm_num_ctx == 16384


def test_static_assets_are_versioned_and_revalidated(tmp_path):
    import re
    c, _ = make_client(tmp_path, ("A", "A사", "", "2026-10-10", "t"))
    html = c.get("/").text
    css = re.search(r'href="(/static/style\.css\?v=\d+)"', html)
    js = re.search(r'src="(/static/app\.js\?v=\d+)"', html)
    assert css and js
    assert c.get(css.group(1)).headers["cache-control"] == "no-cache"
    assert c.get(js.group(1)).status_code == 200


def test_summary_problems_flags_memo_style_lists_and_repeats():
    good = GOOD_SUMMARY
    assert extractor.summary_problems(good) == []
    memo = "유출 추정 항목: 이름, 전화번호. 회사 비정상적 접근 차단, 범위 조사 착수. 피해 고객 몇 명 확인 안됨. 2차 피해 막기 위한 폐기 조치."
    assert any("메모체" in p for p in extractor.summary_problems(memo))
    assert any("목록" in p for p in extractor.summary_problems("진에어는 △이번 유출 △공격 통로 △비상 모니터링 체계를 밝혔다. " * 2))
    assert any("메모체" in p for p in extractor.summary_problems("고객 1454명의 개인정보가 유출되는 사고가 발생했음. 비밀번호는 유출되지 않았음. 회사가 사과했음."))
    rep = "회사는 원인을 조사하고 있다. 회사는 원인을 조사하고 있다. 고객 3만 명의 정보가 유출됐다."
    assert any("반복" in p for p in extractor.summary_problems(rep + " 추가 조사가 필요하다."))
    assert any("한국어" in p for p in extractor.summary_problems("A company was hacked and data leaked yesterday."))
    assert any("한자" in p for p in extractor.summary_problems("2026年10月29日 國內虛擬資產交易所比特孫發生了侵害事故 로 인해 개인정보가 유출됐다. 회사는 조사 중이다."))
    assert any("이름" in p for p in extractor.summary_problems(good, forbidden=["A사"]))


def test_collector_path_rewrites_cramped_json_summary():
    class Cfg:
        llm_enabled = True; llm_base_url = "http://x"; llm_model = "m"; llm_num_ctx = 8192
    cramped = {**GOOD, "summary": "빗썸 해킹. 개인정보 유출 추정. 조사 착수."}
    def handler(req):
        body = json.loads(req.content)
        if "format" in body:
            return httpx.Response(200, json={"message": {"content": json.dumps(cramped)}})
        return httpx.Response(200, json={"message": {"content": "2026년 10월 1일 빗썸이 해킹을 당해 개인정보가 유출됐다. 피해 규모는 약 3만 명이다. 회사는 원인을 조사하고 있다."}})
    art = {**ART, "title": "빗썸 해킹 사고 개인정보 유출"}
    ex_ = extractor.extract(llm.Ollama(httpx.Client(transport=httpx.MockTransport(handler)), "http://x"), Cfg(), art)
    assert ex_.summary.startswith("2026년 10월 1일 빗썸이") and ex_.victim == "빗썸"


def test_card_fragment_views(tmp_path):
    c, (i,) = make_client(tmp_path, ("A", "A사", "", "2026-10-10", "t"))
    lst = c.get(f"/incidents/{i}/card").text
    det = c.get(f"/incidents/{i}/card?view=detail").text
    for frag in (lst, det):
        assert f'id="inc-{i}"' in frag and 'class="regen"' in frag and "<html" not in frag
    assert "<h1>" in det and "관련 기사" in det and "<h1>" not in lst
    assert f'id="inc-{i}"' in c.get(f"/incidents/{i}").text and "<h1>" in c.get(f"/incidents/{i}").text


def test_summary_problems_flags_english_drift():
    src = "KEPCO 한국전력공사 개인정보 유출"
    drift = "2026년 10월 29일 KEPCO는 Dataily Sycue에 confirm한바 퇴직자 개인정보가 유출된 사실을 확인했다. 회사는 조사 중이다."
    assert any("영어" in p for p in extractor.summary_problems(drift, source_text=src))
    ok = "2026년 10월 29일 KEPCO가 퇴직자 65명의 개인정보가 유출된 사실을 확인했다. 한국전력공사는 현재 원인을 조사하고 있다."
    assert extractor.summary_problems(ok, source_text=src) == []


def test_extractive_summary_skips_noise():
    body = ("홍길동 기자 = 기사 시작. 2026년 10월 29일 빗썸에서 고객 6,444명의 개인정보가 유출됐다. "
            "회사는 즉시 접근을 차단하고 당국에 신고했다. 사진=연합뉴스. 추가 피해는 확인되지 않았다. 네 번째 문장도 길게 이어지는 내용이다.")
    out = extractor.extractive_summary(body)
    assert out.startswith("2026년 10월 29일 빗썸") and "기자" not in out and "사진" not in out
    assert extractor.summary_problems(out) == [] and out.count("다.") == 3


def test_regenerate_falls_back_to_article_lead_when_llm_fails_and_old_summary_is_broken(tmp_path, monkeypatch):
    p = write_cfg(tmp_path)
    conn = db.connect(load_config(p).database)
    lead = "2026년 10월 10일 빗썸에서 고객 3만 명의 개인정보가 유출됐다. 회사는 즉시 접근을 차단하고 당국에 신고했다."
    a = conn.execute("INSERT INTO articles(source,url,title,body) VALUES('S','http://u/9','빗썸 해킹 사고 개인정보 유출',?)", (lead,)).lastrowid
    i = dedupe.upsert_incident(conn, ex("브이스비스 해킹", "브이스비스", "", "2026-10-10"), a)
    conn.commit()
    fake_llm(monkeypatch, summaries=("English only summary",), victim="비티코", title="비티코 해킹")
    c = TestClient(create_app(p, run_collector=False))
    r = c.post(f"/incidents/{i}/regenerate")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["method"] == "extract" and d["summary"] == lead
    assert d["victim"] == "" and d["title"] == "빗썸 해킹 사고 개인정보 유출"    # garbled name repaired


def test_regenerate_keeps_good_old_summary_when_llm_fails(tmp_path, monkeypatch):
    p = write_cfg(tmp_path)
    conn = db.connect(load_config(p).database)
    lead = "2026년 10월 10일 A사에서 고객 3만 명의 개인정보가 유출됐다. 회사는 즉시 접근을 차단하고 당국에 신고했다."
    a = conn.execute("INSERT INTO articles(source,url,title,body) VALUES('S','http://u/9','A사 해킹 사고',?)", (lead,)).lastrowid
    i = dedupe.upsert_incident(conn, ex("A사 해킹", "A사", "", "2026-10-10"), a)
    good_old = "2026년 10월 10일 A사가 해킹을 당해 고객 3만 명의 개인정보가 유출됐다. 회사는 원인을 조사하고 있다."
    conn.execute("UPDATE incidents SET summary=? WHERE id=?", (good_old, i)); conn.commit()
    fake_llm(monkeypatch, summaries=("English only summary",))
    c = TestClient(create_app(p, run_collector=False))
    assert c.post(f"/incidents/{i}/regenerate").status_code == 502
    assert c.get("/api/incidents").json()[0]["summary"] == good_old


# ---- English / non-Korean sources ----
from newscollect import feeds  # noqa: E402

EN_BODY = ("The FBI has linked a $6.3 billion cryptocurrency heist at Binance to North Korean hackers. "
           "The TraderTraitor group, also tracked as Lazarus, compromised a DevOps engineer's laptop and submitted "
           "unauthorized withdrawals. Binance said it has reimbursed most of the stolen funds and is working with "
           "investigators. The attack happened on October 29, 2026 and is the largest crypto theft on record.")
EN_ART = {"title": "FBI links $6.3 billion Binance crypto heist to North Korean hackers", "body": EN_BODY,
          "published_at": "2026-10-29"}
EN_JSON = {"is_incident": True, "title": "북한 해킹 그룹, 바이낸스에서 63억 달러 암호화폐 도난", "occurred_at": "2026-10-29",
           "attacker": "TraderTraitor", "victim": "Binance", "damage": "63억 달러 상당의 암호화폐",
           "category": "해킹", "summary": "짧은 요약."}
EN_SUMMARY = ("2026년 10월 29일 북한 해킹 그룹 TraderTraitor가 Binance를 공격해 63억 달러 상당의 암호화폐를 탈취했다. "
              "공격자는 개발 담당자의 노트북을 장악해 승인되지 않은 출금을 요청했다. "
              "Binance는 대부분의 자산을 보상했으며 수사기관과 협력하고 있다.")


class EnCfg:
    llm_enabled = True; llm_base_url = "http://x"; llm_model = "solar"; llm_foreign_model = "exaone"; llm_num_ctx = 8192


def en_llm(json_answer=EN_JSON, summary=EN_SUMMARY):
    seen = []
    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        text = json.dumps(json_answer) if "format" in body else summary
        return httpx.Response(200, json={"message": {"content": text}})
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_is_korean_and_english_incident_gate():
    assert extractor.is_korean("랜섬웨어 공격으로 서버가 마비됐다 (LockBit)") and not extractor.is_korean(EN_BODY)
    yes = lambda t, b="": extractor.looks_like_incident({"title": t, "body": b, "published_at": "2026-10-29"})
    assert yes("Hackers breach Acme and steal customer data")
    assert yes("Ivanti patches two more Connect Secure zero-days chained in attacks")   # patch word, but attacks reported
    assert not yes("Ivanti releases security updates for Connect Secure flaw", "A vulnerability was fixed.")
    assert not yes("Microsoft announces new Surface laptops", "Pricing and availability.")
    assert not yes("How to harden your router", "Tips and tricks for a safer network.")


def test_english_article_summarized_in_korean_with_foreign_model():
    client, seen = en_llm()
    ex_ = extractor.extract(llm.Ollama(client, "http://x"), EnCfg, EN_ART)
    assert ex_.victim == "Binance" and ex_.attacker == "TraderTraitor"          # names stay in the article's spelling
    assert ex_.summary == EN_SUMMARY and extractor.summary_problems(ex_.summary, source_text=EN_ART["title"] + EN_BODY) == []
    assert {b["model"] for b in seen} == {"exaone"}                              # not the Korean model
    assert extractor.is_korean(ex_.title)


def test_korean_article_uses_main_model():
    client, seen = en_llm({**GOOD, "summary": GOOD_SUMMARY})
    extractor.extract(llm.Ollama(client, "http://x"), EnCfg, {**ART, "title": "빗썸 해킹 사고 개인정보 유출", "body": "빗썸에서 개인정보가 유출됐다. " * 5})
    assert {b["model"] for b in seen} == {"solar"}


def test_english_article_never_falls_back_to_english_heuristic():
    class Off(EnCfg): llm_enabled = False
    with pytest.raises(RuntimeError):
        extractor.extract(None, Off, EN_ART)
    client, _ = en_llm({"is_incident": True, "title": "x", "summary": "English only text here."})
    with pytest.raises(ValueError):
        extractor.extract(llm.Ollama(client, "http://x"), EnCfg, EN_ART)


def test_english_article_with_transliterated_victim_is_blanked_and_english_fields_are_rejected():
    translit = {**EN_JSON, "victim": "바이낸스"}
    client, calls = en_llm(translit)
    ex_ = extractor.extract_llm(llm.Ollama(client, "http://x"), "m", EN_ART)         # retried 3x, then the field is blanked
    assert ex_.victim == "" and ex_.attacker == "TraderTraitor" and len(calls) == 3
    english_title = {**EN_JSON, "title": "North Korean hackers steal crypto from Binance"}
    client, _ = en_llm(english_title)
    with pytest.raises(ValueError, match="title이 한국어가 아님"):
        extractor.extract_llm(llm.Ollama(client, "http://x"), "m", EN_ART)
    english_damage = {**EN_JSON, "damage": "limited exploitation in the wild"}
    client, _ = en_llm(english_damage)
    with pytest.raises(ValueError, match="damage가 한국어가 아님"):
        extractor.extract_llm(llm.Ollama(client, "http://x"), "m", EN_ART)


def test_english_weak_summary_is_not_stored_but_korean_one_is_kept():
    cramped = {**EN_JSON, "summary": "바이낸스 해킹. 63억 달러 도난."}
    client, _ = en_llm(cramped, summary="English only.")
    with pytest.raises(ValueError):
        extractor.extract(llm.Ollama(client, "http://x"), EnCfg, EN_ART)
    ko_art = {"title": "빗썸 해킹 사고 개인정보 유출", "body": "빗썸에서 고객 개인정보가 대량 유출됐다. " * 5, "published_at": "2026-10-29"}
    client, _ = en_llm({**GOOD, "summary": "빗썸 해킹. 유출 추정."}, summary="English only.")
    assert extractor.extract(llm.Ollama(client, "http://x"), EnCfg, ko_art).summary == "빗썸 해킹. 유출 추정."   # Korean keeps its terse summary


def test_foreign_model_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[llm]\nmodel = "a"\nforeign_model = "b"\n')
    assert load_config(p).llm_foreign_model == "b"
    assert load_config(write_cfg(tmp_path)).llm_foreign_model == ""
    assert extractor.model_for(load_config(write_cfg(tmp_path)), korean=False) == "solar:10.7b"


THN_HTML = b"""<html><body><article><h1>Short wrapper</h1></article>
<div id="articlebody" class="articlebody"><p>Attackers posed as IT support on <a href="/t">Microsoft Teams</a>, and abused <b>AD CS</b>.</p>
<p>The group convinced employees to grant remote access, then used weak certificate templates to get privileged logins for the company's domain. This is a long enough paragraph to pass the minimum.</p>
<p>It also copied data to an external server before it was detected by the response team at the victim organization.</p>
<ul><li>Related Articles:</li></ul></div></body></html>"""
BC_HTML = b"""<html><body><article><header>By Reporter November 2, 2026 06:28 AM 0</header>
<div class="articleBody"><p>Synology warned customers about ransomware attacks. The company's advisory says <a href="x">C3RB3R</a>'s operators target NAS devices.</p>
<p>It urged users to enable automatic updates and upgrade DiskStation Manager as soon as possible to avoid compromise by the gang.</p>
<p>Related Articles:</p><ul><li>Other story one</li><li>Other story two</li></ul></div></article></body></html>"""


def test_article_text_extraction_for_english_layouts():
    t = feeds.extract_article_text(THN_HTML)           # <article> is only a tiny shell here
    assert t.startswith("Attackers posed as IT support on Microsoft Teams, and abused AD CS.")   # no stray spaces
    assert "weak certificate templates" in t and "Short wrapper" not in t
    b = feeds.extract_article_text(BC_HTML)
    assert "C3RB3R's operators" in b and "By Reporter" not in b                 # inline tags joined, header dropped
    assert "Other story" not in b and "Related Articles" not in b               # related-articles block cut
    dense = b"<html><body><div class='x'>" + b"<p>" + b"Some long unlabeled paragraph of text. " * 10 + b"</p></div></body></html>"
    assert feeds.extract_article_text(dense).startswith("Some long unlabeled paragraph")


def test_feed_prefers_content_encoded_when_longer():
    rss = """<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>x</title>
<item><title>Hackers breach Acme</title><link>http://x/1</link><description>short</description>
<content:encoded><![CDATA[<p>The full text of the story is much longer than the short description.</p>]]></content:encoded>
<pubDate>Tue, 27 Oct 2026 10:00:00 +0000</pubDate></item></channel></rss>"""
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=rss.encode())))
    items = feeds.fetch_feed(client, "http://x/feed", 10, 100000)
    assert items[0]["snippet"].startswith("The full text of the story")


def _row(conn, title, victim, d="2026-10-29"):
    a = seed_article(conn, "u-" + title)
    return dedupe.upsert_incident(conn, extractor.Extraction(True, title, d, "", victim, "", "해킹", "요약 " + title), a)


def test_dedupe_cross_language_uses_judge_only_when_scripts_differ_and_dates_close():
    conn = db.connect(":memory:")
    ko = _row(conn, "바이낸스 암호화폐 63억 달러 도난", "바이낸스")
    asked = []
    def yes(row, ex_): asked.append(ex_.victim); return True
    new = extractor.Extraction(True, "북한 해킹 그룹의 Binance 공격", "2026-10-30", "", "Binance", "", "해킹", "요약")
    assert dedupe.find_match(conn, new) is None                                   # no judge: names can't match
    assert dedupe.find_match(conn, new, judge=lambda r, e: False) is None         # judge says different
    assert dedupe.find_match(conn, new, judge=yes) == ko and asked == ["Binance"]
    far = extractor.Extraction(True, "Binance attack", "2026-11-10", "", "Binance", "", "해킹", "요약")
    n = len(asked); assert dedupe.find_match(conn, far, judge=yes) is None and len(asked) == n   # > 7 days: not asked
    same_script = extractor.Extraction(True, "전혀 다른 회사 해킹", "2026-10-30", "", "롯데카드", "", "해킹", "요약")
    assert dedupe.find_match(conn, same_script, judge=yes) is None and len(asked) == n          # Korean vs Korean: not asked


def test_llm_judge_parses_answer_and_defaults_to_not_same():
    row = {"title": "t", "victim": "v", "attacker": "", "occurred_at": "2026-10-29", "summary": "s"}
    ex_ = extractor.Extraction(True, "t", "2026-10-29", "", "V", "", "", "s")
    mk = lambda h: dedupe.llm_judge(llm.Ollama(httpx.Client(transport=httpx.MockTransport(h)), "http://x"), "m")
    ok = lambda req: httpx.Response(200, json={"message": {"content": '{"same": true}'}})
    assert mk(ok)(row, ex_) is True
    assert mk(lambda req: httpx.Response(200, json={"message": {"content": '{"same": "maybe"}'}}))(row, ex_) is False
    assert mk(lambda req: httpx.Response(500))(row, ex_) is False
    assert mk(lambda req: httpx.Response(200, json={"message": {"content": "not json"}}))(row, ex_) is False


EN_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>FBI links $6.3 billion Binance crypto heist to North Korean hackers</title><link>http://en/1</link>
<description>The FBI has linked a heist.</description><pubDate>Thu, 29 Oct 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""


def english_cfg(tmp_path):
    p = tmp_path / "en.toml"
    p.write_text(f'[llm]\nmodel = "solar"\nforeign_model = "exaone"\n[collector]\ndatabase = "{tmp_path / "en.db"}"\n'
                 '[[sources]]\nname = "BC"\nurl = "http://en/feed.xml"\n')
    cfg = load_config(p); cfg.max_age_days = 100000
    return cfg


def test_collect_english_source_end_to_end(tmp_path, monkeypatch):
    cfg = english_cfg(tmp_path)
    models = set()
    page = ("<html><body><div class='articleBody'><p>" + EN_BODY + "</p></div></body></html>").encode()
    def handler(req):
        if req.url.path == "/api/chat":
            body = json.loads(req.content); models.add(body["model"])
            return httpx.Response(200, json={"message": {"content": json.dumps(EN_JSON) if "format" in body else EN_SUMMARY}})
        if req.url.path.endswith("feed.xml"):
            return httpx.Response(200, content=EN_RSS.encode())
        return httpx.Response(200, content=page)
    real = httpx.Client
    monkeypatch.setattr(collector.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    conn = db.connect(cfg.database)
    stats = collector.collect_once(conn, cfg)
    assert stats["new"] == 1 and stats["incidents"] == 1 and stats["failed"] == 0
    row = conn.execute("SELECT * FROM incidents").fetchone()
    assert row["summary"] == EN_SUMMARY and row["victim"] == "Binance" and extractor.is_korean(row["title"])
    assert "63억" in row["damage"] and models == {"exaone"}
    art = conn.execute("SELECT * FROM articles").fetchone()
    assert art["status"] == "done" and "DevOps engineer" in art["body"]            # full page body, not the RSS snippet
    assert collector.collect_once(conn, cfg)["new"] == 0                            # rerun: nothing duplicated
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1


def test_llm_unreachable_leaves_articles_for_next_cycle(tmp_path, monkeypatch):
    cfg = english_cfg(tmp_path)
    def handler(req):
        if req.url.path == "/api/chat":
            raise httpx.ConnectError("connection refused")
        if req.url.path.endswith("feed.xml"):
            return httpx.Response(200, content=EN_RSS.encode())
        return httpx.Response(404)
    real = httpx.Client
    monkeypatch.setattr(collector.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    conn = db.connect(cfg.database)
    stats = collector.collect_once(conn, cfg)
    row = conn.execute("SELECT status, attempts FROM articles").fetchone()
    assert (row["status"], row["attempts"]) == ("new", 0) and stats["failed"] == 0   # no retry attempt burned


def test_regenerate_english_incident_uses_foreign_model(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text(f'[llm]\nmodel = "solar"\nforeign_model = "exaone"\n[collector]\ndatabase = "{tmp_path / "t.db"}"\n')
    conn = db.connect(load_config(p).database)
    a = conn.execute("INSERT INTO articles(source,url,title,body) VALUES('BC','http://u/e',?,?)", (EN_ART["title"], EN_BODY)).lastrowid
    i = dedupe.upsert_incident(conn, extractor.Extraction(True, "옛 제목", "2026-10-29", "", "Binance", "", "해킹", "오래된 요약"), a)
    conn.commit()
    models = set()
    def handler(req):
        if req.url.path == "/api/chat":
            body = json.loads(req.content); models.add(body["model"])
            return httpx.Response(200, json={"message": {"content": json.dumps(EN_JSON) if "format" in body else EN_SUMMARY}})
        return httpx.Response(404)
    real = httpx.Client
    monkeypatch.setattr(regen.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    r = TestClient(create_app(p, run_collector=False)).post(f"/incidents/{i}/regenerate")
    assert r.status_code == 200 and r.json()["summary"] == EN_SUMMARY and models == {"exaone"}


def test_plausible_date_rejects_invented_dates():
    pd = extractor.plausible_date
    assert pd("2026-10-20", "2026-11-02") == "2026-10-20"
    assert pd("2021-12-31", "2026-11-02") == "2026-11-02"          # invented year
    assert pd("2026-12-25", "2026-11-02") == "2026-11-02"          # future
    assert pd("2026-05-10", "2026-11-02") == "2026-05-10"          # ~6 months back is allowed
    assert pd("garbage", "2026-11-02") == "2026-11-02"
    ex_ = extractor.parse_llm_json(json.dumps({"is_incident": True, "title": "t", "occurred_at": "2021-12-31", "summary": "s"}), "2026-11-02")
    assert ex_.occurred_at == "2026-11-02"


VOLTA = ("A new Windows infostealer called VOLTA-AUTODOXER uses LLMs such as Claude, Gemini and Kimi to rank victims. "
         "Researchers at Acme said the malware, written in Go, steals browser data. The campaign abuses SysGoRAT. "
         "In October the operators moved to MicroCloud servers. VOLTA-AUTODOXER ranks victims by financial value.")


def test_proper_nouns_and_prompt_hint_for_english_only():
    names = extractor.proper_nouns(VOLTA)
    for n in ("VOLTA-AUTODOXER", "Claude", "Gemini", "Kimi", "SysGoRAT", "MicroCloud"):
        assert n in names, names
    assert "October" not in names and "Researchers" not in names
    assert "음차하지 말고" in extractor.names_hint(VOLTA)
    assert extractor.names_hint("빗썸에서 고객 개인정보가 대량 유출됐다. " * 10) == ""      # Korean text: no hint


def test_name_hint_reaches_both_llm_prompts():
    client, seen = en_llm({**EN_JSON, "summary": "짧음."})
    art = {**EN_ART, "title": "VOLTA infostealer uses Claude and Gemini", "body": VOLTA + " " + EN_BODY}
    extractor.extract(llm.Ollama(client, "http://x"), EnCfg, art)
    prompts = [b["messages"][1]["content"] for b in seen if not b["messages"][0]["content"].startswith("너는 교정자다")]
    assert len(prompts) >= 2 and all("음차하지 말고" in p and "Claude" in p for p in prompts)   # JSON call and summary call


def test_korean_article_uses_article_lead_when_both_summaries_are_broken():
    lead = "2026년 11월 2일 더존비즈온에서 고객사 임직원 개인정보가 유출됐다. 회사는 즉시 접근을 차단하고 당국에 신고했다."
    art = {"title": "더존비즈온 해킹 사고 개인정보 유출", "body": lead + " 추가 내용도 있다.", "published_at": "2026-11-02"}
    junk = {**GOOD, "title": "더존비즈온 해킹", "victim": "더존비즈온", "summary": "△성명 △이메일 △주소 △유출 △조사"}
    client, _ = en_llm(junk, summary="△또 △목록")
    out = extractor.extract(llm.Ollama(client, "http://x"), EnCfg, art)
    assert out.summary.startswith("2026년 11월 2일 더존비즈온에서") and extractor.summary_problems(out.summary) == []


def test_english_vulnerability_headline_with_attackers_is_not_an_incident():
    gate = lambda t: extractor.looks_like_incident({"title": t, "body": "Attackers could exploit it.", "published_at": "2026-11-01"})
    assert not gate("CVSS 10.0 LXD, MicroCloud Flaw Lets Attackers Impersonate Trusted Clients Without a Private Key")
    assert gate("Ivanti patches two more Connect Secure zero-days chained in attacks")
    assert gate("Hackers exploit flaw to breach Acme and steal data")


def test_summary_year_must_come_from_article_or_publish_date():
    src = "Hackers stole funds on October 29. The incident was reported in 2026."
    yrs = extractor.known_years(src, "2026-11-01")
    bad = "2023년 10월 29일 해커가 자금을 탈취했다. 회사는 조사 중이다. 피해 고객에게 안내하고 있다."
    assert any("연도" in p for p in extractor.summary_problems(bad, allowed_years=yrs))
    assert not any("연도" in p for p in extractor.summary_problems(bad.replace("2023", "2026"), allowed_years=yrs))
    assert not any("연도" in p for p in extractor.summary_problems(bad))          # no year info supplied: not checked


def test_transliteration_with_english_in_parentheses_is_reduced_to_the_english_name():
    s = "이 악성코드는 클로이데(Claude), 지미니 (Gemini) 등을 활용한다. 바이낸스(Binance)가 피해를 입었다."
    out = extractor.strip_transliterations(s, ["Claude", "Gemini"])
    assert out.startswith("이 악성코드는 Claude, Gemini 등을 활용한다.") and "바이낸스(Binance)" in out   # only listed names
    summary = ("2026년 11월 2일 VOLTA 악성코드가 클로이데(Claude)와 Gemini를 이용해 피해자 데이터를 평가했다. "
               "공격자는 훔친 계정 정보를 순위로 매겼다. 연구진은 키트가 판매될 가능성을 지적했다.")
    client, _ = en_llm(summary=summary)
    art = {"title": "VOLTA infostealer uses Claude and Gemini", "body": VOLTA, "published_at": "2026-11-02"}
    got = extractor.write_summary(llm.Ollama(client, "http://x"), "m", art, {})
    assert "클로이데" not in got and "Claude와 Gemini를" in got


def test_name_only_failure_blanks_the_field_for_english_and_leaks_are_fatal_only_for_korean():
    descriptive = {**EN_JSON, "victim": "정부 기관"}
    client, calls = en_llm(descriptive)
    ex_ = extractor.extract_llm(llm.Ollama(client, "http://x"), "m", EN_ART)            # used to fail after 3 attempts; now repaired
    assert ex_.victim == "" and ex_.attacker == "TraderTraitor" and len(calls) == 3
    # English article: the Korean word may legitimately appear in the Korean prose, so it is not an error
    leaked = {**EN_JSON, "victim": "정부 기관", "summary": "정부 기관이 공격을 받아 데이터가 유출됐다. 피해는 크다. 조사 중이다."}
    client, _ = en_llm(leaked)
    assert extractor.extract_llm(llm.Ollama(client, "http://x"), "m", EN_ART).victim == ""
    # Korean article: a garbled company name that also appears in the prose cannot be trusted
    ko_art = {"title": "빗썸 해킹 사고 개인정보 유출", "body": "빗썸에서 개인정보가 유출됐다. " * 5, "published_at": "2026-10-29"}
    garbled = {**GOOD, "title": "비티코 해킹", "victim": "비티코", "summary": "비티코가 해킹당했다. 피해가 크다. 조사 중이다."}
    client, _ = en_llm(garbled)
    with pytest.raises(ValueError, match="제목/요약"):
        extractor.extract_llm(llm.Ollama(client, "http://x"), "m", ko_art)
    english_title = {**EN_JSON, "title": "North Korean hackers steal crypto from Binance", "victim": "정부 기관"}   # other problems stay fatal
    client, _ = en_llm(english_title)
    with pytest.raises(ValueError, match="title이 한국어가 아님"):
        extractor.extract_llm(llm.Ollama(client, "http://x"), "m", EN_ART)


def test_cross_language_judge_runs_on_the_main_model(tmp_path, monkeypatch):
    cfg = english_cfg(tmp_path)
    judge_models = set()
    page = ("<html><body><div class='articleBody'><p>" + EN_BODY + "</p></div></body></html>").encode()
    def handler(req):
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            if body["messages"][0]["content"].startswith("두 보안 사고 기록"):
                judge_models.add(body["model"])
                return httpx.Response(200, json={"message": {"content": '{"same": true}'}})
            return httpx.Response(200, json={"message": {"content": json.dumps(EN_JSON) if "format" in body else EN_SUMMARY}})
        if req.url.path.endswith("feed.xml"):
            return httpx.Response(200, content=EN_RSS.encode())
        return httpx.Response(200, content=page)
    real = httpx.Client
    monkeypatch.setattr(collector.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    conn = db.connect(cfg.database)
    ko_art = conn.execute("INSERT INTO articles(source,url,title,status) VALUES('보안뉴스','http://ko/1','바이낸스 해킹 분석','done')").lastrowid
    ko = dedupe.upsert_incident(conn, extractor.Extraction(True, "‘한방에 8조원’ 바이낸스 해킹", "2026-11-02", "", "바이낸스", "8조원", "해킹",
                                                           "바이낸스에서 8조원이 넘는 피해가 발생했다."), ko_art)
    conn.commit()
    collector.collect_once(conn, cfg)
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1                    # merged, not duplicated
    assert conn.execute("SELECT COUNT(*) FROM incident_articles WHERE incident_id=?", (ko,)).fetchone()[0] == 2
    assert judge_models == {"solar"}                                                            # main model, not exaone


VOLTA_ART = {"title": "New VOLTA Infostealer Uses Claude, Gemini, and Kimi AI to Rank Victims by Financial Value",
             "body": VOLTA, "published_at": "2026-11-02"}
TRANSLIT_SUMMARY = ("2026년 11월 2일 새로운 VOLTA 악성코드가 발견됐다. 이 악성코드는 클로이디, 젬니, 킴리 등 대형 언어 모델을 활용해 "
                    "피해자의 훔친 데이터를 금전적 가치에 따라 평가한다. 연구진은 이 도구가 판매용 키트일 가능성을 지적했다.")


def test_headline_names_only_keep_words_the_body_confirms_as_names():
    names = extractor.headline_names(VOLTA_ART)
    assert {"VOLTA", "Claude", "Gemini", "Kimi"} <= set(names)
    assert "Rank" not in names and "Victims" not in names and "Financial" not in names     # title-case words


def test_polish_names_restores_transliterated_names_when_edit_is_clean():
    fixed = TRANSLIT_SUMMARY.replace("클로이디, 젬니, 킴리", "Claude, Gemini, Kimi")
    client, seen = en_llm(summary=fixed)
    out = extractor.polish_names(llm.Ollama(client, "http://x"), "m", VOLTA_ART, TRANSLIT_SUMMARY)
    assert out == fixed and "Claude, Gemini, Kimi" in seen[0]["messages"][1]["content"]
    assert seen[0]["options"]["temperature"] == 0 and "format" not in seen[0]


def test_polish_names_keeps_original_when_edit_is_bad_or_not_needed():
    # nothing missing -> no LLM call at all
    ok = TRANSLIT_SUMMARY.replace("클로이디, 젬니, 킴리", "Claude, Gemini, Kimi")
    client, seen = en_llm(summary="아무거나")
    assert extractor.polish_names(llm.Ollama(client, "http://x"), "m", VOLTA_ART, ok) == ok and seen == []
    # edit that drops most of the text, doesn't restore a name, or is English -> original kept
    for bad in ("짧다.", TRANSLIT_SUMMARY, "Claude Gemini Kimi were mentioned in the report by researchers."):
        client, _ = en_llm(summary=bad)
        assert extractor.polish_names(llm.Ollama(client, "http://x"), "m", VOLTA_ART, TRANSLIT_SUMMARY) == TRANSLIT_SUMMARY
    # Korean articles are never touched
    ko = {"title": "빗썸 해킹 사고", "body": "빗썸에서 개인정보가 유출됐다. " * 8, "published_at": "2026-11-02"}
    client, seen = en_llm(summary="x")
    assert extractor.polish_names(llm.Ollama(client, "http://x"), "m", ko, "빗썸 요약이다.") == "빗썸 요약이다." and seen == []


def test_extract_runs_name_polish_for_english_article_only():
    fixed = TRANSLIT_SUMMARY.replace("클로이디, 젬니, 킴리", "Claude, Gemini, Kimi")
    answers = {"json": {**EN_JSON, "title": "VOLTA 인포스테일러, AI로 피해자 가치 평가", "victim": "", "attacker": "VOLTA",
                        "damage": "피해 규모 미상", "summary": TRANSLIT_SUMMARY}}
    def handler(req):
        body = json.loads(req.content)
        if body["messages"][0]["content"].startswith("너는 교정자다"):
            return httpx.Response(200, json={"message": {"content": fixed}})
        return httpx.Response(200, json={"message": {"content": json.dumps(answers["json"])}})
    out = extractor.extract(llm.Ollama(httpx.Client(transport=httpx.MockTransport(handler)), "http://x"), EnCfg, VOLTA_ART)
    assert "Claude, Gemini, Kimi" in out.summary and "클로이디" not in out.summary


def posted_client(tmp_path, *incidents, toml_extra=""):
    """incidents: (title, occurred_at, published_at of its article, hashtags)."""
    p = write_cfg(tmp_path)
    if toml_extra:
        p.write_text(p.read_text() + toml_extra)
    conn = db.connect(load_config(p).database)
    ids = []
    for n, (title, occurred, published, tags) in enumerate(incidents):
        a = conn.execute("INSERT INTO articles(source,url,title,published_at) VALUES('S',?,?,?)",
                         (f"http://p/{n}", title, published)).lastrowid
        e = ex(title, title.split()[0], d=occurred)
        e.hashtags = tags
        ids.append(dedupe.upsert_incident(conn, e, a))
    conn.commit()
    return TestClient(create_app(p, run_collector=False)), ids


def test_tagcloud_counts_tags_of_incidents_published_in_period(tmp_path):
    from datetime import date, timedelta
    ago = lambda n: (date.today() - timedelta(days=n)).isoformat()
    c, _ = posted_client(tmp_path,
                         ("롯데카드 해킹", ago(0), ago(0), ["Scattered Spider", "롯데카드", "개인정보유출"]),
                         ("삼성전자 랜섬웨어", ago(3), ago(3), ["LockBit", "삼성전자", "랜섬웨어", "개인정보유출"]),
                         # incident long ago, but the news came out 2 days ago: counts in the week by publication date
                         ("넥슨 침해", ago(200), ago(2), ["넥슨", "lockbit"]),
                         ("옛날 해킹", ago(400), ago(400), ["옛회사"]))
    get = lambda p: c.get("/api/tagcloud", params={"period": p}).json()
    tags = lambda p: {x["tag"]: x["count"] for x in get(p)["tags"]}
    assert get("day")["incidents"] == 1 and tags("day") == {"Scattered Spider": 1, "롯데카드": 1, "개인정보유출": 1}
    week = tags("week")
    assert get("week")["incidents"] == 3 and week["개인정보유출"] == 2 and week["넥슨"] == 1
    assert week["LockBit"] == 2 and "lockbit" not in week          # case variants merged under the common spelling
    assert "옛회사" not in tags("year")
    assert c.get("/api/tagcloud", params={"period": "decade"}).status_code == 400
    assert c.get("/api/wordcloud").status_code == 404
    page = c.get("/").text
    assert "태그 클라우드" in page and 'data-period="day"' in page and 'data-period="year"' in page
    assert 'data-period="day"' in c.get("/", params={"q": "해킹"}).text   # the side pane stays on search results


def test_tagcloud_exclude_whole_tags_from_config(tmp_path):
    from datetime import date
    from newscollect import tagcloud
    rows = [{"hashtags": '["해킹", "금융권해킹", "Ransomware"]'}, {"hashtags": ["ransomware", "해킹"]}, {"hashtags": "oops"}]
    got = {x["tag"]: x["count"] for x in tagcloud.cloud(rows, exclude=["해킹", " RANSOMWARE "])}
    assert got == {"금융권해킹": 1}                                   # whole tags only, case-insensitive
    today = date.today().isoformat()
    c, _ = posted_client(tmp_path, ("A사 해킹", today, today, ["A사", "해킹"]))
    tags = lambda: {x["tag"] for x in c.get("/api/tagcloud", params={"period": "day"}).json()["tags"]}
    assert tags() == {"A사", "해킹"}                                   # test config has no [tagcloud] section
    p = tmp_path / "config.toml"
    p.write_text(p.read_text() + '\n[wordcloud]\ntrivial = ["해킹"]\n')  # the old section still works
    assert tags() == {"A사"}
    p.write_text(p.read_text().replace('[wordcloud]\ntrivial = ["해킹"]', '[tagcloud]\nexclude = ["A사"]'))
    assert tags() == {"해킹"}                                          # applies without a restart
    p.write_text(p.read_text().replace('exclude = ["A사"]', 'exclude = "A사"'))  # invalid: keep the startup list
    assert tags() == {"A사", "해킹"}


def test_timeline_groups_by_publication_date_and_shows_incident_date(tmp_path):
    c, (old, new) = posted_client(tmp_path,
                                  ("Sussex 유출", "2026-08-10", "2026-11-08", ["SCPO"]),
                                  ("Norad 해킹", "2026-11-03", "2026-11-03", ["Norad"]))
    page = c.get("/").text
    assert page.index("2026-11-08") < page.index(f'id="inc-{old}"') < page.index("2026-11-03") < page.index(f'id="inc-{new}"')
    card = re.search(rf'<article class="incident" id="inc-{old}">.*?</article>', page, re.S).group(0)
    assert "<dt>발생</dt>" in card and "2026-08-10" in card
    other = re.search(rf'<article class="incident" id="inc-{new}">.*?</article>', page, re.S).group(0)
    assert "<dt>발생</dt>" not in other                                # same day: no separate incident date
    assert "2026-08-10" not in page.split(f'id="inc-{old}"')[0]        # no 08-10 date heading
    det = c.get(f"/incidents/{old}").text
    assert "2026-11-08" in det and "발생 2026-08-10" in det
    assert c.get("/api/incidents").json()[0]["posted_at"] == "2026-11-08"


def test_retag_rewrites_hashtags_with_five_roles(tmp_path):
    from newscollect import retag
    c, (i, j) = posted_client(tmp_path, ("A사 랜섬웨어", "2026-11-01", "2026-11-01", ["옛태그"]),
                              ("B사 유출", "2026-11-02", "2026-11-02", ["B사"]))
    answers = {"A사 랜섬웨어": {"tags": {"attacker": "LockBit", "victim": "A사", "system": "VPN", "technique": "",
                                     "type": "랜섬웨어"}},
               "B사 유출": "not json"}
    def handler(req):
        body = json.loads(req.content)
        assert body["format"] == "json" and "attacker" in body["messages"][0]["content"]
        title = re.search(r"제목: (.+)", body["messages"][1]["content"]).group(1)
        a = answers[title]
        return httpx.Response(200, json={"message": {"content": a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)}})
    cfg = load_config(tmp_path / "config.toml")
    conn = db.connect(cfg.database)
    real = llm.from_config
    llm.from_config = lambda cfg, http: llm.Ollama(httpx.Client(transport=httpx.MockTransport(handler)), "http://x")
    try:
        assert retag.retag(conn, cfg) == {"updated": 1, "unchanged": 0, "failed": 1}
    finally:
        llm.from_config = real
    tags = lambda k: json.loads(conn.execute("SELECT hashtags FROM incidents WHERE id=?", (k,)).fetchone()[0])
    assert tags(i) == ["LockBit", "A사", "VPN", "랜섬웨어"] and tags(j) == ["B사"]   # failure keeps the old tags
def test_article_stats_zero_filled_per_unit(tmp_path):
    from datetime import date
    from newscollect import stats
    conn = db.connect(":memory:")
    for n, ts in enumerate(["2026-11-07 12:00:00", "2026-11-07 12:00:00", "2026-11-05 12:00:00",
                            "2026-09-20 12:00:00", "2025-03-01 12:00:00"]):  # noon UTC: same local date in any TZ
        conn.execute("INSERT INTO articles(source,url,title,fetched_at) VALUES('s',?,'t',?)", (f"u{n}", ts))
    today = date(2026, 11, 7)
    assert stats.article_counts(conn, "day", today)[-1]["count"] == 2  # no published_at: counted by collection date
    day = stats.article_counts(conn, "day", today)
    assert len(day) == 30 and day[-1] == {"period": "2026-11-07", "count": 2}
    assert day[-2]["count"] == 0 and day[-3] == {"period": "2026-11-05", "count": 1} and day[0]["period"] == "2026-10-09"
    month = stats.article_counts(conn, "month", today)
    assert [m["period"] for m in month][:2] == ["2025-12", "2026-01"] and month[-1] == {"period": "2026-11", "count": 3}
    assert {m["period"]: m["count"] for m in month}["2026-09"] == 1    # 2025-03 falls outside the 12 months
    assert stats.article_counts(conn, "year", today) == [{"period": "2025", "count": 1}, {"period": "2026", "count": 4}]
    assert stats.article_counts(db.connect(":memory:"), "year", today) == [{"period": "2026", "count": 0}]


def test_article_stats_api_and_chart_section(tmp_path):
    c, _ = make_client(tmp_path, ("A사 해킹", "A사", "", "2026-10-10", "t"))
    d = c.get("/api/article-stats", params={"unit": "day"}).json()
    assert d["unit"] == "day" and len(d["series"]) == 30 and d["total"] == 1   # the article was just inserted
    assert len(c.get("/api/article-stats", params={"unit": "month"}).json()["series"]) == 12
    assert c.get("/api/article-stats", params={"unit": "week"}).status_code == 400
    page = c.get("/").text
    assert all(f'data-unit="{u}"' in page for u in ("day", "month", "year")) and "날짜별" in page


def test_side_pane_and_toggle_only_on_list_page(tmp_path):
    c, ids = make_client(tmp_path, ("A사 해킹", "A사", "", "2026-10-10", "t"))
    page = c.get("/").text
    assert 'class="pane-toggle"' in page and 'aria-controls="side"' in page and '<aside id="side"' in page
    assert page.index('<aside id="side"') < page.index("<main>")          # pane is left of the list
    assert page.index('class="cloud-box"') < page.index("<main>") and page.index('class="trend-chart"') < page.index("<main>")
    detail = c.get(f"/incidents/{ids[0]}").text
    assert "pane-toggle" not in detail and '<aside id="side"' not in detail


def _org_lists(tmp_path):
    gov = tmp_path / "gov.csv"   # KOSIS layout, EUC-KR like the original download
    gov.write_bytes("기관코드,기관명,기관유형\n101,국방부,중앙행정기관\n201,서울특별시,지방자치단체\n"
                    "202,서울특별시 종로구,지방자치단체\n301,한국인터넷진흥원,기타기관\n".encode("cp949"))
    pub = tmp_path / "pub.csv"   # 공공기관 지정현황 layout, UTF-8 with BOM
    pub.write_text("기관명,기관유형,주무부처\n(재)예술경영지원센터,기타공공기관,문화체육관광부\n88관광개발(주),기타공공기관,국가보훈부\n"
                   "주식회사 에스알,공기업,국토교통부\n한국인터넷진흥원,준정부기관,과학기술정보통신부\n", encoding="utf-8-sig")
    return gov, pub


def test_org_classifier_lists_parentheses_aliases_and_containment(tmp_path):
    from newscollect import orgs
    gov, pub = _org_lists(tmp_path)
    c = orgs.Classifier(str(gov), str(pub))
    assert c.tags("국방부") == ["정부기관"]
    assert c.tags("예술경영지원센터") == c.tags("(재) 예술경영지원센터") == ["공공기관"]   # "(재)" ignored
    assert c.tags("88관광개발") == c.tags("에스알") == ["공공기관"]                         # "(주)", 주식회사 ignored
    assert c.tags("한국인터넷진흥원") == ["정부기관", "공공기관"]                            # on both lists
    assert c.tags("서울시") == c.tags("종로구청") == c.tags("서울특별시 종로구") == ["정부기관"]  # local-gov short forms
    assert c.tags("한국인터넷진흥원 침해대응센터") == ["정부기관", "공공기관"]                # victim containing a listed name
    assert c.tags("SK텔레콤") == c.tags("") == c.tags("한국") == []
    assert orgs.Classifier(str(tmp_path / "missing.csv"), str(pub)).tags("국방부") == []     # missing file: no crash


def test_gov_tab_lists_only_tagged_incidents(tmp_path):
    gov, pub = _org_lists(tmp_path)
    p = write_cfg(tmp_path)
    p.write_text(p.read_text() + f'\n[orgs]\ngovernment_list = "{gov}"\npublic_list = "{pub}"\n')
    conn = db.connect(load_config(p).database)
    for n, (title, victim) in enumerate([("국방부 해킹", "국방부"), ("에스알 유출", "주식회사 에스알"),
                                         ("롯데카드 해킹", "롯데카드"), ("국방 관련 랜섬웨어", "")]):
        dedupe.upsert_incident(conn, ex(title, victim, d=f"2026-10-1{n}"), seed_article(conn, f"http://u/{n}"))
    conn.commit()
    c = TestClient(create_app(p, run_collector=False))
    victims = lambda **kw: [r["victim"] for r in c.get("/api/incidents", params=kw).json()]
    assert victims(tab="gov") == ["주식회사 에스알", "국방부"]
    assert len(victims()) == 4 and victims(tab="gov", q="국방부") == ["국방부"]
    assert c.get("/api/incidents", params={"tab": "x"}).status_code == 400
    page = c.get("/", params={"tab": "gov"}).text
    import re
    assert re.search(r'tab=gov" aria-current="page"[^>]*>정부공공 <span class="count">2</span>', page)
    assert "롯데카드 해킹" not in page
    assert '<span class="tag org">공공기관</span>' in page and 'name="tab" value="gov"' in page  # search keeps the tab
    assert '전체 <span class="count">4</span>' in page
    assert '<span class="tag org">정부기관</span>' in c.get("/incidents/1").text


class FakeClaude:
    """Stands in for anthropic.Anthropic: records create() kwargs and returns canned responses (or raises)."""
    def __init__(self, *outs):
        from types import SimpleNamespace as NS
        self.calls, self.outs = [], list(outs)
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        out = self.outs[min(len(self.calls) - 1, len(self.outs) - 1)]
        if isinstance(out, Exception):
            raise out
        return out


def claude_msg(text="", stop_reason="end_turn", category=None):
    from types import SimpleNamespace as NS
    blocks = [NS(type="thinking", thinking="")] + ([NS(type="text", text=text)] if text else [])
    return NS(content=blocks, stop_reason=stop_reason,
              stop_details=NS(category=category) if stop_reason == "refusal" else None)


def test_claude_backend_request_shape_text_and_errors():
    import anthropic, httpx2
    fake = FakeClaude(claude_msg('{"same": true}'))
    out = llm.Claude(fake, effort="medium").chat("claude-sonnet-5-5", "SYS", "USER", json=True, temperature=0.4)
    assert out == '{"same": true}'
    kw = fake.calls[0]
    assert kw["model"] == "claude-sonnet-5-5" and kw["system"] == "SYS"
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    assert kw["output_config"] == {"effort": "medium"} and "temperature" not in kw   # sampling params are rejected
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    with pytest.raises(llm.LLMError, match="거부.*cyber"):
        llm.Claude(FakeClaude(claude_msg(stop_reason="refusal", category="cyber"))).chat("m", "s", "u")
    with pytest.raises(llm.LLMError, match="텍스트가 없음"):
        llm.Claude(FakeClaude(claude_msg(""))).chat("m", "s", "u")
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    with pytest.raises(llm.LLMUnavailable):          # network down: the collector waits for the next cycle
        llm.Claude(FakeClaude(anthropic.APIConnectionError(request=req))).chat("m", "s", "u")
    with pytest.raises(llm.LLMUnavailable):          # wrong key: same, don't burn article attempts
        llm.Claude(FakeClaude(anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=req), body=None))).chat("m", "s", "u")
    with pytest.raises(llm.LLMError):                # a bad request is this answer's problem
        llm.Claude(FakeClaude(anthropic.BadRequestError("bad", response=httpx2.Response(400, request=req), body=None))).chat("m", "s", "u")


def test_extract_with_claude_backend_uses_claude_model_for_all_languages():
    class Cfg:
        llm_enabled, llm_provider, claude_model = True, "claude", "claude-sonnet-5-5"
        llm_model, llm_foreign_model = "solar", "exaone"
    answer = json.dumps({**GOOD, "summary": "2026년 10월 1일 빗썸이 해킹을 당해 개인정보가 유출됐다. 회사는 조사 중이다."})
    fake = FakeClaude(claude_msg(answer))
    ex_ = extractor.extract(llm.Claude(fake), Cfg, ART)
    assert ex_.is_incident and ex_.victim == "빗썸" and fake.calls[0]["model"] == "claude-sonnet-5-5"
    assert extractor.model_for(Cfg, korean=False) == "claude-sonnet-5-5"
    fake2 = FakeClaude(claude_msg(stop_reason="refusal", category="cyber"))   # every attempt refused
    assert extractor.extract(llm.Claude(fake2), Cfg, ART).method == "heuristic"   # Korean: keyword fallback
    assert len(fake2.calls) == 3
    with pytest.raises(Exception):
        extractor.extract(llm.Claude(fake2), Cfg, EN_ART)                      # English: fail, retried next cycle


def test_provider_config_and_missing_key(tmp_path, monkeypatch):
    p = write_cfg(tmp_path)
    assert load_config(p).llm_provider == "local" and load_config(p).claude_effort == "low"
    p.write_text(p.read_text().replace('[llm]\n', '[llm]\nprovider = "claude"\nclaude_effort = "high"\n'))
    cfg = load_config(p)
    assert (cfg.llm_provider, cfg.claude_model, cfg.claude_effort) == ("claude", "claude-sonnet-5-5", "high")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.LLMUnavailable, match="ANTHROPIC_API_KEY"):
        llm.from_config(cfg, None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert isinstance(llm.from_config(cfg, None), llm.Claude)
    bad = tmp_path / "bad.toml"
    bad.write_text(p.read_text().replace('provider = "claude"', 'provider = "openai"'))
    with pytest.raises(ValueError, match="provider"):
        load_config(bad)


def test_collector_with_claude_and_no_key_leaves_articles_untouched(tmp_path, monkeypatch):
    p = write_cfg(tmp_path)
    p.write_text(p.read_text().replace('[llm]\n', '[llm]\nprovider = "claude"\n'))
    cfg = load_config(p)
    conn = db.connect(cfg.database)
    conn.execute("INSERT INTO articles(source,url,title,body) VALUES('s','http://u/1','빗썸 해킹','빗썸 해킹 유출')")
    conn.commit()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(collector.feeds, "fetch_feed", lambda *a, **k: [])
    collector.collect_once(conn, cfg)
    assert tuple(conn.execute("SELECT status, attempts FROM articles").fetchone()) == ("new", 0)


def test_hashtags_cleaned_from_llm_answer():
    c = extractor.clean_hashtags
    # role object -> attacker, victim, system, technique, type; unknown slots skipped
    assert c({"type": "랜섬웨어", "attacker": "Lock Bit", "victim": "A사", "system": "#VPN", "technique": "크리덴셜 스터핑"}) == \
        ["LockBit", "A사", "VPN", "크리덴셜스터핑", "랜섬웨어"]
    assert c({"attacker": "미상", "victim": "Norad", "system": "", "technique": "unknown", "type": "개인정보유출"}) == \
        ["Norad", "개인정보유출"]
    assert c({"attacker": "", "victim": ["A사", "B사"]}) == ["A사"]
    assert c({"victim": "현대건설,우신시스템, 대원화성", "system": "Microsoft·Google"}) == ["현대건설", "Microsoft"]  # one per slot
    assert c(["#랜섬웨어", "Lock Bit", "제조업", "a4", "a5", "extra"]) == ["랜섬웨어", "LockBit", "제조업", "a4", "a5"]  # max 5
    assert c("#빗썸 #해킹, #거래소") == ["빗썸", "해킹", "거래소"]                              # string form accepted
    assert c(["해킹", "해킹", "x", "#", "아주아주아주아주아주아주아주아주아주긴해시태그입니다", 5]) == ["해킹"]  # dupes/short/long/non-str
    assert c(None) == [] and c({"a": 1}) == []
    tags = {"attacker": "", "victim": "빗썸", "system": "핫월렛", "technique": "", "type": "가상자산탈취"}
    ex_ = extractor.parse_llm_json(json.dumps({**GOOD, "tags": tags}), "2026-10-01")
    assert ex_.hashtags == ["빗썸", "핫월렛", "가상자산탈취"]
    ex_ = extractor.parse_llm_json(json.dumps({**GOOD, "hashtags": ["#빗썸", "해킹"]}), "2026-10-01")  # older answer shape
    assert ex_.hashtags == ["빗썸", "해킹"]
    assert extractor.parse_llm_json(json.dumps(GOOD), "2026-10-01").hashtags == []        # missing: no failure
    assert all(f'"{r}"' in extractor.TAGS_SYSTEM for r in extractor.TAG_ROLES) and "- tags:" in extractor.SYSTEM


def test_hashtags_column_added_to_existing_database(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY, title TEXT NOT NULL, occurred_at TEXT NOT NULL, "
                "attacker TEXT DEFAULT '', victim TEXT DEFAULT '', damage TEXT DEFAULT '', category TEXT DEFAULT '', "
                "summary TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    old.execute("INSERT INTO incidents(title, occurred_at) VALUES('옛 사고', '2026-01-01')")
    old.commit(); old.close()
    conn = db.connect(str(path))
    assert conn.execute("SELECT hashtags FROM incidents").fetchone()[0] == "[]"
    db.connect(str(path))  # second start: no error


def test_hashtags_stored_merged_shown_and_searchable(tmp_path):
    p = write_cfg(tmp_path)
    conn = db.connect(load_config(p).database)
    a = ex("빗썸 해킹", "빗썸")
    a.hashtags = ["빗썸", "가상자산거래소", "해킹"]
    i = dedupe.upsert_incident(conn, ex("빗썸 해킹", "빗썸"), seed_article(conn, "http://u/1"))   # first: no tags
    assert dedupe.upsert_incident(conn, a, seed_article(conn, "http://u/2")) == i                 # merge fills them
    b = ex("빗썸 해킹", "빗썸"); b.hashtags = ["다른", "태그들", "입니다"]
    dedupe.upsert_incident(conn, b, seed_article(conn, "http://u/3"))                              # existing tags kept
    conn.commit()
    assert json.loads(conn.execute("SELECT hashtags FROM incidents").fetchone()[0]) == a.hashtags
    c = TestClient(create_app(p, run_collector=False))
    assert c.get("/api/incidents").json()[0]["hashtags"] == a.hashtags
    assert len(c.get("/api/incidents", params={"q": "가상자산거래소"}).json()) == 1
    page = c.get("/").text
    assert '<a href="/?q=%EA%B0%80%EC%83%81%EC%9E%90%EC%82%B0%EA%B1%B0%EB%9E%98%EC%86%8C">#가상자산거래소</a>' in page
    assert "#가상자산거래소" in c.get(f"/incidents/{i}").text


def test_regenerate_refreshes_hashtags(tmp_path, monkeypatch):
    c, ids = make_client(tmp_path, ("A사 랜섬웨어", "A사", "", "2026-10-10", "A사 랜섬웨어 공격"))
    seen = {"n": 0}
    def handler(req):
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            if "format" in body:
                out = {"is_incident": True, "title": "A사 랜섬웨어 피해", "occurred_at": "2026-10-10", "attacker": "",
                       "victim": "A사", "damage": "서버 마비", "category": "랜섬웨어", "summary": "요약.",
                       "hashtags": ["A사", "랜섬웨어", "서버마비"]}
                return httpx.Response(200, json={"message": {"content": json.dumps(out)}})
            return httpx.Response(200, json={"message": {"content": GOOD_SUMMARY}})
        return httpx.Response(404)
    real = httpx.Client
    monkeypatch.setattr(regen.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    r = c.post(f"/incidents/{ids[0]}/regenerate")
    assert r.status_code == 200, r.text
    assert c.get("/api/incidents").json()[0]["hashtags"] == ["A사", "랜섬웨어", "서버마비"]


def test_version_changes_on_new_or_updated_incident(tmp_path):
    c, (i,) = make_client(tmp_path, ("A", "A사", "", "2026-10-10", "t"))
    v0 = c.get("/api/version").json()
    page = c.get("/").text
    assert f'data-incidents="{v0["incidents"]}"' in page and 'class="new-banner"' in page
    assert c.get("/api/version").json() == v0  # stable while nothing changes
    conn = db.connect(str(tmp_path / "t.db"))
    conn.execute("UPDATE incidents SET summary='new', updated_at='2999-01-01 00:00:00' WHERE id=?", (i,))
    conn.commit()
    v1 = c.get("/api/version").json()
    assert v1["incidents"] != v0["incidents"] and v1["articles"] == v0["articles"]
    assert 'id="live"' not in c.get(f"/incidents/{i}").text  # detail pages don't poll


def test_refresh_seconds_config(tmp_path):
    from pathlib import Path
    p = write_cfg(tmp_path)
    assert load_config(p).refresh_seconds == 60
    base = Path(p).read_text()
    for val, want in (("0", 0), ("2", 5), ("90", 90)):
        Path(p).write_text(f"[server]\nrefresh_seconds = {val}\n" + base)
        assert load_config(p).refresh_seconds == want
    c = TestClient(create_app(p, run_collector=False))
    assert 'data-refresh="90"' in c.get("/").text
    Path(p).write_text("[server]\nrefresh_seconds = 15\n" + base)
    assert 'data-refresh="15"' in c.get("/").text  # applies on the next page load
    for bad in ("-1", '"fast"', "true"):
        Path(p).write_text(f"[server]\nrefresh_seconds = {bad}\n" + base)
        with pytest.raises(ValueError):
            load_config(p)


def test_article_stats_count_by_published_date(tmp_path):
    from datetime import date
    from newscollect import stats
    conn = db.connect(":memory:")
    # one collection run on 11-07 picks up a backlog published over several days
    for n, pub in enumerate(["2026-11-07", "2026-11-05", "2026-11-05", "2026-10-30", ""]):
        conn.execute("INSERT INTO articles(source,url,title,published_at,fetched_at) VALUES('s',?,'t',?,?)",
                     (f"u{n}", pub, "2026-11-07 12:00:00"))
    day = {d["period"]: d["count"] for d in stats.article_counts(conn, "day", date(2026, 11, 7))}
    assert (day["2026-11-07"], day["2026-11-05"], day["2026-10-30"], day["2026-11-06"]) == (2, 2, 1, 0)
    month = {d["period"]: d["count"] for d in stats.article_counts(conn, "month", date(2026, 11, 7))}
    assert (month["2026-11"], month["2026-10"]) == (4, 1)


def test_ollama_think_switch_and_breach_notice_prefilter(tmp_path):
    seen = []
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": "{}"}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm.Ollama(client, "http://x").chat("m", "s", "u")
    llm.Ollama(client, "http://x", think=True).chat("m", "s", "u")
    assert [b["think"] for b in seen] == [False, True]  # thinking off by default: it ate the reply budget
    p = write_cfg(tmp_path)
    assert load_config(p).llm_think is False
    p.write_text(p.read_text().replace("[llm]", "[llm]\nthink = true", 1))
    assert load_config(p).llm_think is True
    assert llm.from_config(load_config(p), client).think is True
    notice = {"title": "County of Sussex, New Jersey Notice of Data Privacy Incident",
              "body": "The Prosecutor's Office experienced a data privacy event that involved personal information."}
    assert extractor.looks_like_incident(notice)
    assert extractor.looks_like_incident({"title": "Clinic notifies patients", "body": "An unauthorized third party accessed files."})
    assert not extractor.looks_like_incident({"title": "UK Wades Into Tech Sovereignty", "body": "Domestic AI investment."})


def test_summary_check_ignores_english_abbreviation_periods():
    ok = ("2023년 Perry Johnson & Associates, Inc. (PJ&A), Perry Johnson, Inc. (PJI)에서 데이터 유출 사고가 발생했다. "
          "NYS Attorney General Letitia James와 U.S. 3개 주 검찰총장은 최대 1,000만 달러의 합의금을 확보했다. "
          "이들은 시정 조치 계획을 수립하기로 했다.")
    assert extractor.summary_problems(ok) == []
    assert any("메모체" in p for p in extractor.summary_problems("A사가 해킹을 당했다. 피해 규모 3만 건. 조사 중임. 대응 예정."))


def test_summaries_use_plain_da_style_not_polite():
    polite = ("2026년 11월 3일 Norad가 사이버 공격을 받았습니다. 사용자 20,614명의 이름과 이메일 주소가 유출되었습니다. "
              "피해 기관은 Norad입니다. 서비스는 곧 재개됩니다. 복구는 끝난 것이 아닙니다.")
    plain = extractor.plain_form(polite)
    assert plain == ("2026년 11월 3일 Norad가 사이버 공격을 받았다. 사용자 20,614명의 이름과 이메일 주소가 유출되었다. "
                     "피해 기관은 Norad이다. 서비스는 곧 재개된다. 복구는 끝난 것이 아니다.")
    assert extractor.summary_problems(plain) == []
    # ambiguous polite forms are not guessed ("필요합니다" -> 필요하다, "진행합니다" -> 진행한다): the check rejects them
    rest = "A사가 해킹을 당해 고객 정보가 유출됐다. 회사는 원인 조사를 진행합니다. 피해 고객에게 개별 안내를 했어요."
    assert extractor.plain_form(rest) == rest
    assert any("존댓말" in p for p in extractor.summary_problems(rest))
    assert extractor.plain_form("고객 정보를 받습니다.") == "고객 정보를 받습니다."  # present-tense verb: 받는다, not 받다
    assert not any("존댓말" in p for p in extractor.summary_problems("A사의 복구는 아직 끝난 것이 아니다. 회사는 원인을 조사하고 있다."))


def test_polite_summary_is_converted_or_rewritten():
    calls = []
    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if "format" in body:  # the JSON extraction answers in polite form; "진행합니다" can't be converted safely
            return httpx.Response(200, json={"message": {"content": json.dumps({
                "is_incident": True, "title": "Acme 랜섬웨어 공격", "occurred_at": "2026-10-01", "victim": "Acme",
                "attacker": "", "damage": "고객 3만 명 정보 유출", "category": "랜섬웨어",
                "summary": "2026년 10월 1일 Acme가 랜섬웨어 공격을 받았습니다. 고객 3만 명의 정보가 유출되었습니다. Acme는 조사를 진행합니다."})}})
        return httpx.Response(200, json={"message": {"content":
            "2026년 10월 1일 Acme가 랜섬웨어 공격을 받았습니다. 이 사고로 고객 3만 명의 정보가 유출되었습니다. Acme는 외부 전문가와 함께 원인을 조사하고 있습니다."}})
    art = {**ART, "title": "Acme hit by ransomware, customer data stolen",
           "body": "Acme said on October 1, 2026 that a ransomware attack stole data on 30,000 customers. Acme is investigating with outside experts."}
    class Cfg:
        llm_enabled, llm_model, llm_foreign_model = True, "m", ""
    ex_ = extractor.extract(llm.Ollama(httpx.Client(transport=httpx.MockTransport(handler)), "http://x"), Cfg(), art)
    assert ex_.summary == ("2026년 10월 1일 Acme가 랜섬웨어 공격을 받았다. 이 사고로 고객 3만 명의 정보가 유출되었다. "
                           "Acme는 외부 전문가와 함께 원인을 조사하고 있다.")
    assert len(calls) == 2 and "존댓말" in calls[1]["messages"][0]["content"]  # rewritten by the prose step


def test_daily_article_span_follows_max_age_days(tmp_path):
    from datetime import date
    from newscollect import stats
    days = stats.article_counts(db.connect(":memory:"), "day", date(2026, 11, 8), days=60)
    assert len(days) == 60 and days[0]["period"] == "2026-09-10" and days[-1]["period"] == "2026-11-08"
    c, _ = make_client(tmp_path, ("A사 해킹", "A사", "", "2026-10-10", "t"))
    assert len(c.get("/api/article-stats?unit=day").json()["series"]) == 30   # test config has no max_age_days
    p = tmp_path / "config.toml"
    p.write_text(p.read_text().replace("[collector]", "[collector]\nmax_age_days = 90", 1))
    assert len(c.get("/api/article-stats?unit=day").json()["series"]) == 90   # re-read: no restart
    assert len(c.get("/api/article-stats?unit=month").json()["series"]) == 12  # month/year views unchanged


def test_article_average_skips_empty_lead_in_and_current_period(tmp_path):
    from newscollect import stats
    s = lambda counts: [{"period": f"2026-11-{i + 1:02d}", "count": c} for i, c in enumerate(counts)]
    # nothing before 11-04, today (11-08) only partly over: (10+24+37+16)/4, not 90/8
    a = stats.average(s([0, 0, 0, 10, 24, 37, 16, 3]))
    assert (a["value"], a["from"], a["to"], a["periods"]) == (87 / 4, "2026-11-04", "2026-11-07", 4)
    assert stats.average(s([0, 0, 5]))["value"] == 5            # only the current period has data: use it
    assert stats.average(s([0, 4, 0, 0]))["value"] == 4 / 2     # zero days after collection began do count
    assert stats.average(s([0, 0, 0])) is None
    c, _ = make_client(tmp_path, ("A사 해킹", "A사", "", "2026-10-10", "t"))
    d = c.get("/api/article-stats?unit=day").json()
    assert d["average"]["value"] == 1 and d["average"]["periods"] == 1


def test_feed_snippet_drops_wordpress_footer():
    from newscollect import feeds
    html = ('<p>DigiCert says attackers accessed business customer records.</p> <p>The post '
            '<a href="https://www.securityweek.com/x">DigiCert Data Breach Impacts Business Customer Information</a> '
            'appeared first on <a href="https://www.securityweek.com">SecurityWeek</a>.</p>')
    assert feeds._clean(html) == "DigiCert says attackers accessed business customer records."
    assert feeds._clean("<p>The post office was hacked, police said.</p>") == "The post office was hacked, police said."


def test_prefilter_skips_conference_talks_columns_and_roundups():
    body = "랜섬웨어 공격으로 제약사 시스템이 마비되고 개인정보가 유출된 사례를 소개했다. hacked breach ransomware"
    skip = ["[AIS 2026] 에브리존 “공격은 발생해도 업무는 멈추지 않아야”…랜섬웨어 방어 전략",
            "[RSAC 2026] Panel: lessons from the MOVEit breach", "[인터뷰] 해킹 사고 대응 전문가 홍길동",
            "[기고] 개인정보 유출 사고, 이제는 달라져야", "In Other News: Trezor Implant, LLMJacking Bills Hit $1M",
            "Week in Review: Ransomware hits hospitals", "SecurityWeek Ransomware Roundup: LockBit returns"]
    for t in skip:
        assert not extractor.looks_like_incident({"title": t, "body": body}), t
    keep = ["[단독] 랜섬웨어 조직 메타인크립터, 한국 기업 4곳 데이터 탈취 주장", "미래에셋증권 그룹웨어 해킹…3만5,255명 개인정보 유출",
            "DigiCert Data Breach Impacts Business Customer Information", "Hackers breached 2026 election vendor"]
    for t in keep:
        assert extractor.looks_like_incident({"title": t, "body": body}), t
    assert "주간 요약" in extractor.SYSTEM and "강연" in extractor.SYSTEM
