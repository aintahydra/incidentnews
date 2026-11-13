"""python -m pytest scripts/KakaotalkParser -q   (offline: page fetches are stubbed, the LLM is not called)"""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[2])]
import kakao_import  # noqa: E402
import kakao_log  # noqa: E402
from newscollect import db  # noqa: E402

LOG1 = """공급망보안연구회 님과 카카오톡 대화\r
저장한 날짜 : 2026-01-06 09:00:00\r
\r
--------------- 2025년 12월 30일 화요일 ---------------\r
[A] [오후 1:00] 작년 기사\r
https://news.example/old\r
--------------- 2026년 1월 5일 월요일 ---------------\r
B님이 C님을 초대했습니다.\r
[A] [오후 12:51] 국정원 실태평가 결과가 나왔습니다. 의견입니다.\r
\r
국정원, 152개 기관 ‘2025 사이버보안 실태평가’ 공개…우수 32·미흡 6\r
https://www.dailysecu.com/news/articleView.html?idxno=203973 \r
‘우수’ 공공기관 32개로 증가…취약점 상시 점검·개선이 갈랐다\r
[A] [오후 12:52] 사진\r
[A] [오후 12:52] 美 연방 계약업체 세드윅 피격\r
https://www.boannews.com/media/view.asp?idx=141289&page=1&mkind=1&kind=1 \r
해커들은 자회사를 공격해 3.4GB를 훔쳤다고 주장했다.\r
[B] [오전 12:05] 감사합니다\r
"""
LOG2 = LOG1.replace("저장한 날짜 : 2026-01-06 09:00:00", "저장한 날짜 : 2026-01-08 09:00:00") + """\
--------------- 2026년 1월 7일 수요일 ---------------\r
[A] [오전 10:00] 영상 https://youtu.be/xyz 과 기사 두 개\r
첫 번째 기사 제목입니다\r
https://thehackernews.com/2026/01/one.html\r
첫 기사 요약\r
두 번째 기사 제목입니다\r
https://thehackernews.com/2026/01/two.html\r
"""


def test_parse_header_days_messages_and_links():
    lg = kakao_log.parse(LOG1)
    assert lg.chat == "공급망보안연구회" and lg.saved_at == datetime(2026, 1, 6, 9, 0)
    assert [m.when.strftime("%m-%d %H:%M") for m in lg.messages] == ["12-30 13:00", "01-05 12:51", "01-05 12:52",
                                                                      "01-05 12:52", "01-05 00:05"]
    first = kakao_log.links(lg.messages[1])[0]
    assert first.url == "https://www.dailysecu.com/news/articleView.html?idxno=203973"
    assert first.title.startswith("국정원, 152개 기관") and first.preview.startswith("‘우수’ 공공기관")
    assert first.comment.startswith("국정원 실태평가")
    assert [ln.url for _, ln in kakao_log.shared_links(lg, date(2026, 1, 1))][1].startswith("https://www.boannews.com")
    assert len(kakao_log.shared_links(lg, date(2026, 1, 1))) == 2              # the 2025 link is out


def test_links_split_several_urls_in_one_message():
    msg = kakao_log.parse(LOG2).messages[-1]
    ls = kakao_log.links(msg)
    assert [ln.url for ln in ls] == ["https://youtu.be/xyz", "https://thehackernews.com/2026/01/one.html",
                                     "https://thehackernews.com/2026/01/two.html"]
    assert ls[1].title == "첫 번째 기사 제목입니다" and ls[1].preview == "첫 기사 요약"
    assert ls[2].title == "두 번째 기사 제목입니다"


def test_norm_url_ignores_scheme_mobile_host_and_paging():
    n = kakao_import.norm_url
    assert n("http://www.boannews.com/news/articleView.html?idxno=1") == n("https://m.boannews.com/news/articleView.html?idxno=1")
    assert n("https://a.com/x/?utm_source=k&id=2#frag") == n("https://a.com/x?id=2")
    assert n("https://a.com/x?id=2") != n("https://a.com/x?id=3")


def test_import_is_incremental_and_skips_known_urls(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "t.db"))
    conn.executescript(kakao_import.SCHEMA)
    conn.execute("INSERT INTO articles(source,url,title) VALUES('보안뉴스','https://www.boannews.com/news/articleView.html?idxno=141289','x')")
    redirects = {"https://www.boannews.com/media/view.asp?idx=141289&page=1&mkind=1&kind=1":
                 "http://www.boannews.com/news/articleView.html?idxno=141289"}
    fetched = []

    def fake_fetch(client, url):
        fetched.append(url)
        return {"url": redirects.get(url, url), "text": "", "title": "Page Title - 사이트", "date": "2025-01-01", "site": "사이트"}
    monkeypatch.setattr(kakao_import, "fetch", fake_fetch)
    f1, f2 = tmp_path / "KakaoTalk_1.txt", tmp_path / "KakaoTalk_2.txt"
    f1.write_text(LOG1, encoding="utf-8")
    assert kakao_import.import_links(conn, [f1], date(2026, 1, 1)) == 1        # boannews one redirects to a known URL
    row = conn.execute("SELECT * FROM articles WHERE url LIKE '%dailysecu%'").fetchone()
    assert row["status"] == "humint" and row["source"] == "데일리시큐"       # the outlet, not the chat
    prov = conn.execute("SELECT * FROM humint_articles WHERE article_id=?", (row["id"],)).fetchone()
    assert (prov["chat"], prov["shared_at"]) == ("공급망보안연구회", "2026-01-05 12:51")
    assert row["title"].startswith("국정원, 152개 기관")                        # the chat headline, not the page title
    assert row["published_at"] == "2026-01-05"                                 # page date outside 90 days: share date
    assert "‘우수’ 공공기관" in row["body"]                                      # page unreadable: chat preview
    # the newer export repeats everything; only the 1월 7일 links are new, and YouTube is not an article
    f2.write_text(LOG2, encoding="utf-8")
    fetched.clear()
    assert kakao_import.import_links(conn, [f2, f1], date(2026, 1, 1)) == 2
    assert fetched == ["https://thehackernews.com/2026/01/one.html", "https://thehackernews.com/2026/01/two.html"]
    assert kakao_import.import_links(conn, [f2], date(2026, 1, 1)) == 0
    mark = conn.execute("SELECT * FROM humint_imports").fetchone()
    assert (mark["chat"], mark["last_message_at"], mark["file"]) == ("공급망보안연구회", "2026-01-07 10:00", "KakaoTalk_2.txt")


def test_headline_falls_back_to_page_title_without_site_name():
    ln = kakao_log.Link("https://x", title="[공유][일본]SEGA Data Breach in Arara Vendor")
    assert kakao_import.headline(ln, "whatever", "https://x") == "SEGA Data Breach in Arara Vendor"
    assert kakao_import.headline(kakao_log.Link("https://x"), "해킹 사고 발생 - 보안뉴스", "https://x") == "해킹 사고 발생"


def test_outlet_names_and_fixing_old_sources(tmp_path):
    from types import SimpleNamespace as NS
    cfg = NS(sources=[NS(name="보안뉴스", url="https://www.boannews.com/rss/S1N2.xml"),
                      NS(name="The Hacker News", url="https://feeds.feedburner.com/TheHackersNews")],
             intel_sources=[NS(name="Cisco Talos", url="https://blog.talosintelligence.com/rss/")])
    names = kakao_import.outlet_names(cfg)
    o = lambda url, site="": kakao_import.outlet(url, site, names)
    assert o("https://m.boannews.com/html/detail.html?idx=1") == "보안뉴스"           # config name, mobile host
    assert o("https://thehackernews.com/2026/01/x.html") == "The Hacker News"        # built-in (feed is on feedburner)
    assert o("https://blog.talosintelligence.com/x/") == "Cisco Talos"
    assert o("https://news.example.org/a", "Example News") == "Example News"         # the page's og:site_name
    assert o("https://www.unknown-site.io/a") == "unknown-site.io"                    # else the host
    conn = db.connect(str(tmp_path / "t.db"))
    conn.executescript(kakao_import.SCHEMA)
    conn.execute("INSERT INTO articles(source,url,title) VALUES('카카오톡 · 공급망보안연구회','https://www.securityweek.com/a/','t')")
    conn.execute("INSERT INTO articles(source,url,title) VALUES('보안뉴스','https://www.boannews.com/b','t')")
    assert kakao_import.fix_sources(conn, names) == 1 and kakao_import.fix_sources(conn, names) == 0
    assert [r[0] for r in conn.execute("SELECT source FROM articles ORDER BY id")] == ["Security Week", "보안뉴스"]
    assert conn.execute("SELECT chat FROM humint_articles").fetchone()[0] == "공급망보안연구회"
