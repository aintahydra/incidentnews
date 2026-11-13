"""Tag incidents whose victim is a government body (정부기관) or a public institution (공공기관).

Name lists are CSV files from config.toml [orgs]:
- government_list: KOSIS 기관목록 (name in the 2nd column). Its 기관유형 decides the tag: 중앙행정기관 and 지방자치단체
  -> "정부기관"; 공사/공단, 연구기관, 기타기관, 금융기관 -> "공공기관"; 협회/조합 (private associations) -> no tag.
- public_list: 재정경제부 공공기관 지정현황 (name in the 1st column) -> "공공기관"
A victim can carry both tags (some bodies are on both lists). Files are re-read when they change on disk.

Neither list has local public bodies (지방공기업 and city/province foundations and centres such as 광주관광공사,
서울시설공단, 서울시 육아종합지원센터): a name made of a region and an institution suffix is tagged "공공기관".
A victim naming several parties ("방위사업청, HD현대") is checked part by part.
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
KIND_TAG = {"중앙행정기관": GOV, "지방자치단체": GOV, "공사/공단": PUBLIC, "연구기관": PUBLIC, "기타기관": PUBLIC,
            "금융기관": PUBLIC, "협회/조합": None}  # KOSIS 기관유형 -> tag; a missing or unknown type counts as 정부기관
# region names a local public body starts with, besides the local governments on the KOSIS list
REGIONS = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남",
           "경북", "경남", "제주")
LOCAL_BODY = re.compile(r"(공사|공단|관리단|재단|진흥원|센터|연구원|의료원|정보원|평생교육원|문화원|체육회|복지관|사업단)$")
# region-named bodies that are not public: universities and schools, financial companies, private hospitals
NOT_LOCAL_BODY = re.compile(r"대학|학교|학원|은행|보험|증권|카드|캐피탈|저축|병원|의원|교회|성당|주식회사|㈜")
_SPLIT = re.compile(r"[,，·/]|\s및\s|\s와\s|\s과\s")

_PAREN = re.compile(r"\([^)]*\)|（[^）]*）")  # "(사)", "(재)", "(주)", "(DaeJonBizOn)"
_LEGAL = re.compile(r"재단법인|사단법인|주식회사|㈜")
_METRO = re.compile(r"^(서울|부산|대구|인천|광주|대전|울산)(특별시|광역시)$")


def norm(name: str) -> str:
    name = _LEGAL.sub("", _PAREN.sub("", name or ""))
    return re.sub(r"[^0-9a-z가-힣]", "", name.lower())


def _aliases(name: str, kind: str) -> set[str]:
    """Short forms used in news for local governments: 서울특별시 -> 서울시; 서울특별시 종로구 -> 종로구청, 종로구,
    서울종로구, 서울시종로구; 경기도 수원시 -> 수원시, 수원시청. Two-letter districts (동구, 중구) only with their city."""
    out = {norm(name)}
    if kind == "지방자치단체" and "교육청" not in name:
        parts = name.split()
        last = parts[-1]
        if m := _METRO.match(last):
            out.add(m.group(1) + "시")
        elif last.endswith(("시", "군", "구")) and len(last) >= 2:
            out.add(norm(last + "청"))
            if len(last) >= 3:
                out.add(norm(last))
            if len(parts) >= 2 and (m := _METRO.match(parts[0])):   # 광주광역시 동구 -> 광주동구, 광주시동구
                out.update({m.group(1) + norm(last), m.group(1) + "시" + norm(last)})
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
def _load(gov_path: str, gov_mtime: float, pub_path: str, pub_mtime: float):
    """((name, tag), ...) and the region names (local governments and their short forms) as a sorted tuple."""
    names: dict[str, set[str]] = {}
    regions = set(REGIONS)
    for path, col, default in ((gov_path, 1, GOV), (pub_path, 0, PUBLIC)):
        if not path:
            continue
        for name, kind in _read_csv(Path(path), col):
            tag = KIND_TAG.get(kind, GOV) if default == GOV else PUBLIC
            aliases = _aliases(name, kind)
            if kind == "지방자치단체" and "교육청" not in name:
                regions |= aliases | {a[:-1] for a in aliases if a.endswith("청")}  # 종로구청 -> 종로구
            if tag:
                for a in aliases:
                    names.setdefault(a, set()).add(tag)
    pairs = tuple((n, t) for n in sorted(names) for t in sorted(names[n]))
    return pairs, tuple(sorted(regions, key=len, reverse=True))


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
            return (), ()

    def tags(self, victim: str) -> list[str]:
        names, regions = self._names()
        found = set()
        for part in _SPLIT.split(victim or ""):
            found.update(_tags_for(names, regions, norm(part)))
        return [t for t in (GOV, PUBLIC) if t in found]


@lru_cache(maxsize=4096)
def _tags_for(names: tuple[tuple[str, str], ...], regions: tuple[str, ...], v: str) -> tuple[str, ...]:
    if len(v) < 2:
        return ()
    exact = {tag for n, tag in names if n == v}
    if exact:
        return tuple(t for t in (GOV, PUBLIC) if t in exact)
    # a region followed by an institution name: a local public body (광주관광공사, 서울시설공단, 서울시육아종합지원센터)
    region = next((r for r in regions if v.startswith(r) and len(v) - len(r) >= 2), None)
    if region and LOCAL_BODY.search(v[len(region):]) and not NOT_LOCAL_BODY.search(v):
        found = {PUBLIC}
        found |= {tag for n, tag in names if len(n) >= MIN_CONTAIN and n in v and n not in regions and not region.startswith(n)
                  and not n.startswith(region)}
        return tuple(t for t in (GOV, PUBLIC) if t in found)
    found = {tag for n, tag in names if len(n) >= MIN_CONTAIN and n in v}
    return tuple(t for t in (GOV, PUBLIC) if t in found)
