"""Tag incidents whose victim is a government body (정부기관) or a public institution (공공기관).

Name lists are CSV files from config.toml [orgs]:
- government_list: KOSIS 기관목록 (name in the 2nd column) -> "정부기관"
- public_list: 재정경제부 공공기관 지정현황 (name in the 1st column) -> "공공기관"
A victim can carry both tags (some bodies are on both lists). Files are re-read when they change on disk.
"""
import csv
import logging
import re
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("newscollect")

GOV, PUBLIC = "정부기관", "공공기관"
TAB_TAGS = {"gov": (GOV, PUBLIC)}  # tab name -> incidents carrying any of these tags
MIN_CONTAIN = 3  # a list name this short or longer may match inside a longer victim name ("국민건강보험공단 일산병원")

_PAREN = re.compile(r"\([^)]*\)|（[^）]*）")  # "(사)", "(재)", "(주)", "(DaeJonBizOn)"
_LEGAL = re.compile(r"재단법인|사단법인|주식회사|㈜")
_METRO = re.compile(r"^(서울|부산|대구|인천|광주|대전|울산)(특별시|광역시)$")


def norm(name: str) -> str:
    name = _LEGAL.sub("", _PAREN.sub("", name or ""))
    return re.sub(r"[^0-9a-z가-힣]", "", name.lower())


def _aliases(name: str, kind: str) -> set[str]:
    """Short forms used in news for local governments: 서울특별시 -> 서울시, 서울특별시 종로구 -> 종로구청."""
    out = {norm(name)}
    if kind == "지방자치단체" and "교육청" not in name:
        last = name.split()[-1]
        if m := _METRO.match(last):
            out.add(m.group(1) + "시")
        elif last.endswith(("시", "군", "구")) and len(last) >= 2:
            out.add(norm(last + "청"))
    return {a for a in out if len(a) >= 2}


def _read_csv(path: Path, col: int) -> list[tuple[str, str]]:
    """(name, 기관유형) rows. Accepts UTF-8 (with or without BOM) and, as a fallback, EUC-KR/CP949."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"{path}: not UTF-8 or EUC-KR")
    rows = list(csv.reader(text.splitlines()))
    kind_col = rows[0].index("기관유형") if rows and "기관유형" in rows[0] else None
    return [(r[col].strip(), r[kind_col].strip() if kind_col is not None and len(r) > kind_col else "")
            for r in rows[1:] if len(r) > col and r[col].strip()]


@lru_cache(maxsize=8)
def _load(gov_path: str, gov_mtime: float, pub_path: str, pub_mtime: float) -> tuple[tuple[str, str], ...]:
    names: dict[str, set[str]] = {}
    for path, col, tag in ((gov_path, 1, GOV), (pub_path, 0, PUBLIC)):
        if not path:
            continue
        for name, kind in _read_csv(Path(path), col):
            for a in _aliases(name, kind):
                names.setdefault(a, set()).add(tag)
    return tuple((n, t) for n in sorted(names) for t in sorted(names[n]))


def _mtime(path: str) -> float:
    try:
        return Path(path).stat().st_mtime if path else 0.0
    except OSError:
        log.warning("org list not found: %s", path)
        return -1.0


class Classifier:
    def __init__(self, gov_path: str = "", pub_path: str = ""):
        self.gov_path, self.pub_path = gov_path, pub_path

    def _names(self):
        g, p = _mtime(self.gov_path), _mtime(self.pub_path)
        try:
            return _load(self.gov_path if g >= 0 else "", g, self.pub_path if p >= 0 else "", p)
        except Exception:
            log.exception("could not read org lists")
            return ()

    def tags(self, victim: str) -> list[str]:
        return list(_tags_for(self._names(), norm(victim)))


@lru_cache(maxsize=4096)
def _tags_for(names: tuple[tuple[str, str], ...], v: str) -> tuple[str, ...]:
    if len(v) < 2:
        return ()
    found = set()
    for n, tag in names:
        if n == v or (len(n) >= MIN_CONTAIN and n in v):
            found.add(tag)
    return tuple(t for t in (GOV, PUBLIC) if t in found)
