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
