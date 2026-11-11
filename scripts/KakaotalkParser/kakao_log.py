"""Parse a KakaoTalk chat export (KakaoTalk_{DATE}_{TIME}_*.txt) into messages and shared links.

Export layout (PC KakaoTalk, Korean UI):

    공급망보안연구회 님과 카카오톡 대화
    저장한 날짜 : 2026-11-10 18:41:20

    --------------- 2026년 1월 5일 월요일 ---------------
    홍길동님이 ...님을 초대했습니다.                   <- system line
    [홍길동] [오후 12:51] first line of a message
    more lines of the same message ...
    [김철수] [오후 12:17] next message

A message runs until the next "[name] [time]" line, date separator or system line.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime

HEADER = re.compile(r"^(?P<chat>.+?) 님과 카카오톡 대화\s*$")
SAVED = re.compile(r"^저장한 날짜\s*:\s*(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)\s*$")
DAY = re.compile(r"^-{5,} (?P<y>\d{4})년 (?P<m>\d{1,2})월 (?P<d>\d{1,2})일 \S+요일 -{5,}\s*$")
MSG = re.compile(r"^\[(?P<who>[^\]]+)\] \[(?P<ampm>오전|오후) (?P<h>\d{1,2}):(?P<mi>\d{2})\] ?(?P<text>.*)$")
SYSTEM = re.compile(r"님(이|을|과) .*(초대했습니다|나갔습니다|들어왔습니다|내보냈습니다)\.?\s*$|"
                    r"^채팅방 관리자가 .*$|^.+님이 들어왔습니다\.?$|^.+님이 나갔습니다\.?$")
URL = re.compile(r"https?://[^\s<>\"'\]\)]+")
TRAILING = ".,;:!?…·'\"’”)]}>"


@dataclass
class Message:
    when: datetime
    who: str
    text: str


@dataclass
class Link:
    url: str
    title: str = ""        # the headline line just before the URL (or text before it on the same line)
    preview: str = ""      # the lines after the URL up to the next link: the shared article's lead or a summary
    comment: str = ""      # the sharer's own words before the first link of the message
    when: datetime | None = None
    seq: int = 0           # message index within its minute (see Log.messages)


@dataclass
class Log:
    chat: str
    saved_at: datetime | None
    messages: list[Message] = field(default_factory=list)


def _hour(ampm: str, h: int) -> int:
    return (h % 12) + (12 if ampm == "오후" else 0)   # 오전 12:05 = 00:05, 오후 12:05 = 12:05


def parse(text: str) -> Log:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿").split("\n")
    chat, saved = "", None
    for ln in lines[:5]:
        if m := HEADER.match(ln.strip()):
            chat = m["chat"].strip()
        elif m := SAVED.match(ln.strip()):
            ts = m["ts"] if m["ts"].count(":") == 2 else m["ts"] + ":00"
            saved = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    log, day, cur = Log(chat, saved), None, None

    def flush():
        nonlocal cur
        if cur:
            cur.text = cur.text.rstrip()
            log.messages.append(cur)
        cur = None

    for ln in lines:
        if m := DAY.match(ln):
            flush()
            day = date(int(m["y"]), int(m["m"]), int(m["d"]))
        elif day and (m := MSG.match(ln)):
            flush()
            when = datetime(day.year, day.month, day.day, _hour(m["ampm"], int(m["h"])), int(m["mi"]))
            cur = Message(when, m["who"].strip(), m["text"])
        elif day and SYSTEM.search(ln.strip()) and not ln.startswith(" "):
            flush()
        elif cur is not None:
            cur.text += "\n" + ln
    flush()
    return log


def _clean_url(u: str) -> str:
    return u.rstrip(TRAILING)


def links(msg: Message) -> list[Link]:
    """Each URL in a message, with the headline line before it and the preview text after it."""
    rows = msg.text.split("\n")
    hits = [(i, _clean_url(m.group(0)), m.start()) for i, r in enumerate(rows) for m in URL.finditer(r)]
    out = []
    first = hits[0][0] if hits else 0
    for n, (i, url, col) in enumerate(hits):
        before = rows[i][:col].strip(" :-–—>·\t")
        title = ""
        if len(before) >= 8:                      # "기사 제목 https://..." on one line
            title = before
        elif i > 0 and rows[i - 1].strip() and not URL.search(rows[i - 1]):
            title = rows[i - 1].strip()
        nxt = hits[n + 1][0] if n + 1 < len(hits) else len(rows)
        after = rows[i][col + len(url):].strip()
        body = [after] if after else []
        stop = nxt - 1 if nxt < len(rows) and nxt - 1 > i and rows[nxt - 1].strip() else nxt  # next link's headline
        body += [r for r in rows[i + 1:stop]]
        preview = "\n".join(body).strip()
        title_row = i - 1 if title and not before else i
        comment = "\n".join(rows[:title_row]).strip() if i == first else ""
        out.append(Link(url, title, preview, comment, msg.when))
    return out


def shared_links(log: Log, since: date) -> list[tuple[int, Link]]:
    """(message index, link) for every link in messages on or after `since`."""
    res = []
    for idx, msg in enumerate(log.messages):
        if msg.when.date() < since:
            continue
        for ln in links(msg):
            res.append((idx, ln))
    return res
