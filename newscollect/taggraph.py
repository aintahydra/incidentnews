"""Tag link graph: which hashtags appear together on cards more often than chance.

This is the analysis from scripts/tag_link_test.py, run inside the web app (see /api/tag-graph). It needs no
scipy: the one-sided Fisher exact test is a hypergeometric tail, computed exactly with math.comb.

Each incident card (with all its linked articles) and each intel card is one document, and its `hashtags`
are its tag set. Two tags are linked when they appear on the same card.
  1. Pair test: every pair on >= min_support cards gets a one-sided Fisher exact test over all cards, with
     Benjamini-Hochberg correction (q).
  2. Permutation test: tags are shuffled across cards (keeping each card's tag count), and the number of
     repeated pairs is compared with the observed number.
  3. Text check: each tag, or an alias, is looked up in the source article title + body.
"""
import collections
import itertools
import json
import math
import random
import re

SCOPES = ("all", "incident", "intel")

# Words that count as evidence for a tag in the source text, beyond the tag itself.
ALIAS = {
    '원격코드실행': ['RCE', 'remote code execution', '원격 코드 실행'],
    'RCE': ['remote code execution', '원격 코드 실행'],
    '패치관리': ['patch', '패치', '업데이트', 'update'],
    '개인정보유출': ['개인정보', 'personal data', 'personal information', '유출', 'breach', 'leak'],
    '데이터유출': ['유출', 'breach', 'leak', 'exposed'],
    'AI보안': ['AI', '인공지능'],
    '피싱': ['phishing', '피싱'],
    '공급망공격': ['supply chain', '공급망'],
    '인증우회': ['authentication bypass', 'auth bypass', '인증 우회', 'bypass'],
    '랜섬웨어': ['ransomware', '랜섬'],
    '취약점분석': ['vulnerab', '취약점'],
    '취약점스캔': ['scan', '스캔'],
    '취약점관리': ['vulnerab', '취약점'],
    '위협분석': ['threat', '위협'],
    '정책': ['policy', 'directive', '정책', 'BOD'],
    '백업서버해킹': ['백업'],
    '세무서비스': ['세무'],
    'NAS해킹': ['NAS'],
    '자비스': ['jobis', '자비스'],
    '디지서트': ['digicert'],
    'DigiCert': ['디지서트'],
}

INCIDENT_SQL = """
SELECT 'incident' AS sec, i.id, i.title, i.hashtags, '/incidents/' || i.id AS link,
       group_concat(a.title || ' ' || COALESCE(a.body, ''), ' ') AS text
FROM incidents i
JOIN incident_articles ia ON ia.incident_id = i.id
JOIN articles a ON a.id = ia.article_id
GROUP BY i.id
"""
INTEL_SQL = """
SELECT 'intel' AS sec, n.id, n.title, n.hashtags, a.url AS link, a.title || ' ' || COALESCE(a.body, '') AS text
FROM intel n
JOIN articles a ON a.id = n.article_id
"""


def norm(s: str) -> str:
    return re.sub(r'[\s\-_·.()]', '', s).lower()


def load_cards(conn, scope: str = "all") -> list[dict]:
    sql = {"incident": INCIDENT_SQL, "intel": INTEL_SQL}.get(scope, INCIDENT_SQL + " UNION ALL " + INTEL_SQL)
    cards = []
    for r in conn.execute(sql):
        try:
            tags = sorted({t for t in json.loads(r["hashtags"] or "[]") if isinstance(t, str) and t})
        except ValueError:
            tags = []
        text = norm(r["text"] or "")
        grounded = {t: any(norm(x) in text for x in [t] + ALIAS.get(t, [])) for t in tags}
        cards.append(dict(sec=r["sec"], id=r["id"], title=r["title"], link=r["link"], tags=tags, grounded=grounded))
    return cards


def fisher_greater(k: int, fa: int, fb: int, n: int) -> float:
    """One-sided Fisher exact p for the 2x2 table [[k, fa-k], [fb-k, n-fa-fb+k]]: P(X >= k) for
    X ~ Hypergeometric(population n, fa successes, fb draws). Same as scipy's alternative='greater'."""
    hi = min(fa, fb)
    tail = sum(math.comb(fa, x) * math.comb(n - fa, fb - x) for x in range(k, hi + 1))
    return min(1.0, tail / math.comb(n, fb))


def bh(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg q-values, in the input order."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q, running = [0.0] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = running
    return q


def permutation_test(cards: list[dict], min_support: int, perms: int, seed: int) -> tuple[int, list[int]]:
    rng = random.Random(seed)
    pool = [t for c in cards for t in c["tags"]]
    sizes = [len(c["tags"]) for c in cards]

    def repeated(tags):
        cnt, i = collections.Counter(), 0
        for s in sizes:
            cnt.update(itertools.combinations(sorted(set(tags[i:i + s])), 2))
            i += s
        return sum(1 for v in cnt.values() if v >= min_support)

    observed, null = repeated(pool), []
    for _ in range(perms):
        rng.shuffle(pool)
        null.append(repeated(pool))
    return observed, null


def analyze(cards: list[dict], min_support: int = 2, perms: int = 1000, seed: int = 7) -> dict:
    """The graph and its statistics as one JSON-ready dict."""
    n = len(cards)
    freq = collections.Counter(t for c in cards for t in c["tags"])
    pairs = collections.Counter(p for c in cards for p in itertools.combinations(c["tags"], 2))
    ground = collections.Counter(t for c in cards for t, g in c["grounded"].items() if g)

    edges = []
    for (a, b), k in pairs.items():
        if k < min_support:
            continue
        fa, fb = freq[a], freq[b]
        both = [c for c in cards if a in c["tags"] and b in c["tags"]]
        edges.append(dict(a=a, b=b, k=k, fa=fa, fb=fb, lift=round(k * n / (fa * fb), 2),
                          p=fisher_greater(k, fa, fb, n),
                          g=sum(c["grounded"][a] and c["grounded"][b] for c in both),
                          cards=[dict(sec=c["sec"], id=c["id"], title=c["title"], link=c["link"]) for c in both]))
    for e, q in zip(edges, bh([e["p"] for e in edges])):
        e["q"] = q
    edges.sort(key=lambda e: (e["p"], -e["k"]))

    observed, null = permutation_test(cards, min_support, perms, seed) if n else (0, [])
    linked = {t for e in edges for t in (e["a"], e["b"])}
    return dict(
        N=n, ninc=sum(c["sec"] == "incident" for c in cards), nint=sum(c["sec"] == "intel" for c in cards),
        ntags=len(freq), npairs=len(pairs), single=sum(1 for v in pairs.values() if v == 1),
        reused=sum(1 for v in freq.values() if v >= 2),
        tt=sum(freq.values()), tg=sum(ground.values()), minsup=min_support,
        obs=observed, nperm=len(null),
        permp=(sum(x >= observed for x in null) + 1) / (len(null) + 1) if null else 1.0,
        nullmean=round(sum(null) / len(null), 1) if null else 0, nullmax=max(null, default=0),
        hist=sorted(collections.Counter(null).items()),
        # ni: cards from the incident section, so the page knows which section a tag link should search
        nodes=[dict(id=t, n=freq[t], ni=sum(c["sec"] == "incident" and t in c["tags"] for c in cards))
               for t in sorted(linked)],
        edges=edges,
    )
