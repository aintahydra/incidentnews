import threading
import json
import httpx
import pytest
from fastapi.testclient import TestClient

from newscollect import collector, db, dedupe, extractor
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
        extractor.extract_llm(client, "http://x", "m", ART)
    assert [c["options"]["temperature"] for c in calls] == [0, 0.3, 0.6]


def test_llm_garbled_name_is_retried_and_recovers():
    garbled = {**GOOD, "title": "비티코 해킹", "victim": "비티코", "summary": "비티코가 해킹당했다."}
    client, calls = llm_client([garbled, GOOD])
    ex = extractor.extract_llm(client, "http://x", "m", ART)
    assert ex.victim == "빗썸" and len(calls) == 2
    assert "비티코" in calls[1]["messages"][1]["content"]       # retry hint names the bad token


def test_llm_garbled_name_always_rejected():
    garbled = {**GOOD, "victim": "비티코", "summary": "비티코가 해킹당했다."}
    client, calls = llm_client([garbled])
    with pytest.raises(ValueError, match="기사 본문에 없는 이름"):
        extractor.extract_llm(client, "http://x", "m", ART)


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
    ex = extractor.extract_llm(client, "http://x", "m", ART, num_ctx=6000)
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
    ex_ = extractor.extract(httpx.Client(transport=httpx.MockTransport(handler)), Cfg(), art)
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
    ex_ = extractor.extract(client, EnCfg, EN_ART)
    assert ex_.victim == "Binance" and ex_.attacker == "TraderTraitor"          # names stay in the article's spelling
    assert ex_.summary == EN_SUMMARY and extractor.summary_problems(ex_.summary, source_text=EN_ART["title"] + EN_BODY) == []
    assert {b["model"] for b in seen} == {"exaone"}                              # not the Korean model
    assert extractor.is_korean(ex_.title)


def test_korean_article_uses_main_model():
    client, seen = en_llm({**GOOD, "summary": GOOD_SUMMARY})
    extractor.extract(client, EnCfg, {**ART, "title": "빗썸 해킹 사고 개인정보 유출", "body": "빗썸에서 개인정보가 유출됐다. " * 5})
    assert {b["model"] for b in seen} == {"solar"}


def test_english_article_never_falls_back_to_english_heuristic():
    class Off(EnCfg): llm_enabled = False
    with pytest.raises(RuntimeError):
        extractor.extract(None, Off, EN_ART)
    client, _ = en_llm({"is_incident": True, "title": "x", "summary": "English only text here."})
    with pytest.raises(ValueError):
        extractor.extract(client, EnCfg, EN_ART)


def test_english_article_with_transliterated_victim_is_blanked_and_english_fields_are_rejected():
    translit = {**EN_JSON, "victim": "바이낸스"}
    client, calls = en_llm(translit)
    ex_ = extractor.extract_llm(client, "http://x", "m", EN_ART)         # retried 3x, then the field is blanked
    assert ex_.victim == "" and ex_.attacker == "TraderTraitor" and len(calls) == 3
    english_title = {**EN_JSON, "title": "North Korean hackers steal crypto from Binance"}
    client, _ = en_llm(english_title)
    with pytest.raises(ValueError, match="title이 한국어가 아님"):
        extractor.extract_llm(client, "http://x", "m", EN_ART)
    english_damage = {**EN_JSON, "damage": "limited exploitation in the wild"}
    client, _ = en_llm(english_damage)
    with pytest.raises(ValueError, match="damage가 한국어가 아님"):
        extractor.extract_llm(client, "http://x", "m", EN_ART)


def test_english_weak_summary_is_not_stored_but_korean_one_is_kept():
    cramped = {**EN_JSON, "summary": "바이낸스 해킹. 63억 달러 도난."}
    client, _ = en_llm(cramped, summary="English only.")
    with pytest.raises(ValueError):
        extractor.extract(client, EnCfg, EN_ART)
    ko_art = {"title": "빗썸 해킹 사고 개인정보 유출", "body": "빗썸에서 고객 개인정보가 대량 유출됐다. " * 5, "published_at": "2026-10-29"}
    client, _ = en_llm({**GOOD, "summary": "빗썸 해킹. 유출 추정."}, summary="English only.")
    assert extractor.extract(client, EnCfg, ko_art).summary == "빗썸 해킹. 유출 추정."   # Korean keeps its terse summary


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
    mk = lambda h: dedupe.llm_judge(httpx.Client(transport=httpx.MockTransport(h)), "http://x", "m")
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
    extractor.extract(client, EnCfg, art)
    prompts = [b["messages"][1]["content"] for b in seen if not b["messages"][0]["content"].startswith("너는 교정자다")]
    assert len(prompts) >= 2 and all("음차하지 말고" in p and "Claude" in p for p in prompts)   # JSON call and summary call


def test_korean_article_uses_article_lead_when_both_summaries_are_broken():
    lead = "2026년 11월 2일 더존비즈온에서 고객사 임직원 개인정보가 유출됐다. 회사는 즉시 접근을 차단하고 당국에 신고했다."
    art = {"title": "더존비즈온 해킹 사고 개인정보 유출", "body": lead + " 추가 내용도 있다.", "published_at": "2026-11-02"}
    junk = {**GOOD, "title": "더존비즈온 해킹", "victim": "더존비즈온", "summary": "△성명 △이메일 △주소 △유출 △조사"}
    client, _ = en_llm(junk, summary="△또 △목록")
    out = extractor.extract(client, EnCfg, art)
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
    got = extractor.write_summary(client, "http://x", "m", art, {})
    assert "클로이데" not in got and "Claude와 Gemini를" in got


def test_name_only_failure_blanks_the_field_for_english_and_leaks_are_fatal_only_for_korean():
    descriptive = {**EN_JSON, "victim": "정부 기관"}
    client, calls = en_llm(descriptive)
    ex_ = extractor.extract_llm(client, "http://x", "m", EN_ART)            # used to fail after 3 attempts; now repaired
    assert ex_.victim == "" and ex_.attacker == "TraderTraitor" and len(calls) == 3
    # English article: the Korean word may legitimately appear in the Korean prose, so it is not an error
    leaked = {**EN_JSON, "victim": "정부 기관", "summary": "정부 기관이 공격을 받아 데이터가 유출됐다. 피해는 크다. 조사 중이다."}
    client, _ = en_llm(leaked)
    assert extractor.extract_llm(client, "http://x", "m", EN_ART).victim == ""
    # Korean article: a garbled company name that also appears in the prose cannot be trusted
    ko_art = {"title": "빗썸 해킹 사고 개인정보 유출", "body": "빗썸에서 개인정보가 유출됐다. " * 5, "published_at": "2026-10-29"}
    garbled = {**GOOD, "title": "비티코 해킹", "victim": "비티코", "summary": "비티코가 해킹당했다. 피해가 크다. 조사 중이다."}
    client, _ = en_llm(garbled)
    with pytest.raises(ValueError, match="제목/요약"):
        extractor.extract_llm(client, "http://x", "m", ko_art)
    english_title = {**EN_JSON, "title": "North Korean hackers steal crypto from Binance", "victim": "정부 기관"}   # other problems stay fatal
    client, _ = en_llm(english_title)
    with pytest.raises(ValueError, match="title이 한국어가 아님"):
        extractor.extract_llm(client, "http://x", "m", EN_ART)


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
    out = extractor.polish_names(client, "http://x", "m", VOLTA_ART, TRANSLIT_SUMMARY)
    assert out == fixed and "Claude, Gemini, Kimi" in seen[0]["messages"][1]["content"]
    assert seen[0]["options"]["temperature"] == 0 and "format" not in seen[0]


def test_polish_names_keeps_original_when_edit_is_bad_or_not_needed():
    # nothing missing -> no LLM call at all
    ok = TRANSLIT_SUMMARY.replace("클로이디, 젬니, 킴리", "Claude, Gemini, Kimi")
    client, seen = en_llm(summary="아무거나")
    assert extractor.polish_names(client, "http://x", "m", VOLTA_ART, ok) == ok and seen == []
    # edit that drops most of the text, doesn't restore a name, or is English -> original kept
    for bad in ("짧다.", TRANSLIT_SUMMARY, "Claude Gemini Kimi were mentioned in the report by researchers."):
        client, _ = en_llm(summary=bad)
        assert extractor.polish_names(client, "http://x", "m", VOLTA_ART, TRANSLIT_SUMMARY) == TRANSLIT_SUMMARY
    # Korean articles are never touched
    ko = {"title": "빗썸 해킹 사고", "body": "빗썸에서 개인정보가 유출됐다. " * 8, "published_at": "2026-11-02"}
    client, seen = en_llm(summary="x")
    assert extractor.polish_names(client, "http://x", "m", ko, "빗썸 요약이다.") == "빗썸 요약이다." and seen == []


def test_extract_runs_name_polish_for_english_article_only():
    fixed = TRANSLIT_SUMMARY.replace("클로이디, 젬니, 킴리", "Claude, Gemini, Kimi")
    answers = {"json": {**EN_JSON, "title": "VOLTA 인포스테일러, AI로 피해자 가치 평가", "victim": "", "attacker": "VOLTA",
                        "damage": "피해 규모 미상", "summary": TRANSLIT_SUMMARY}}
    def handler(req):
        body = json.loads(req.content)
        if body["messages"][0]["content"].startswith("너는 교정자다"):
            return httpx.Response(200, json={"message": {"content": fixed}})
        return httpx.Response(200, json={"message": {"content": json.dumps(answers["json"])}})
    out = extractor.extract(httpx.Client(transport=httpx.MockTransport(handler)), EnCfg, VOLTA_ART)
    assert "Claude, Gemini, Kimi" in out.summary and "클로이디" not in out.summary
