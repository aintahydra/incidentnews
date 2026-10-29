# newscollect

보안 뉴스 RSS를 수집해 사고(incident)별로 중복 없이 정리하는 서비스. 설계는 [DESIGN.md](DESIGN.md) 참고.

## 실행

```bash
export OLLAMA_HOST=10.0.2.2:11434   # 선택: config.toml의 [llm].endpoint보다 우선
python3 -m newscollect              # http://localhost:8000
python3 -m pytest                   # 테스트
```

의존성: fastapi, uvicorn, jinja2, httpx, feedparser, beautifulsoup4, lxml (pytest).

## 설정 (`config.toml`)

- 뉴스 소스 추가/삭제/수정: `[[sources]]` 블록을 편집. 매 수집 주기마다 파일을 다시 읽으므로 재시작이 필요 없다. `enabled = false`로 일시 중지.
- LLM: `[llm]`의 `endpoint`, `model`, `enabled`. `enabled = false`이거나 LLM 호출이 실패하면 규칙 기반 추출로 대체된다.
- 수집: `[collector]`의 `interval_minutes`, `max_articles_per_run`, `max_age_days`, `database`.

- `[llm].num_ctx`(기본 8192): Ollama로 보내는 컨텍스트 크기. Ollama 기본값(4096)은 긴 한글 기사(글자당 약 1.1토큰)에서 프롬프트가 잘릴 수 있다.

## 기능

- **테마**: 헤더의 ☀ 라이트 / 🌙 다크 버튼. 선택은 브라우저(localStorage)에 저장되고, 처음에는 OS 설정을 따른다.
- **검색**: 헤더 검색창(`/?q=키워드`). 공백으로 구분한 모든 단어가 제목·요약·공격자·피해·규모·분류 또는 관련 기사 제목에 있어야 한다. `/api/incidents?q=`도 동일.
- **요약 다시 생성**: 각 사고의 버튼(`POST /incidents/{id}/regenerate`). 원문을 다시 읽고(관련 기사 최대 3건) ① 필드(공격자·피해 등)를 다시 추출하고 ② 요약을 JSON이 아닌 줄글로 따로 쓴다. 결과는 같은 자리에 바로 반영되고(목록·상세 모두) 초록색 안내와 함께 잠깐 강조된다. LLM이 끝내 좋은 요약을 못 쓰고 기존 요약도 망가져 있으면(잘못된 이름·메모체) 기사 본문 첫 문장들로 대체하고 그렇다고 알려준다. 기존 요약이 멀쩡하면 오류를 보여주고 그대로 둔다. 동시에 1건만, 같은 사고는 20초 간격, 최대 약 4분.
- **결과 검증**: 요약은 '~다.'로 끝나는 완전한 문장 2~6개여야 하고(메모체·△목록·반복·한자/영어 섞임 불가), 피해/공격자 이름은 기사 본문에 실제로 있어야 한다(소형 모델이 이름을 잘못 쓰는 경우가 있음). 실패하면 온도를 올려 재시도하고, 끝내 실패하면 수집 시에는 규칙 기반 추출로, 재생성 시에는 오류로 처리한다.

## 엔드포인트

`/` 사고 목록(날짜별, `?q=` 검색) · `/incidents/{id}` 상세 · `/incidents/{id}/card` 카드 조각 · `POST /incidents/{id}/regenerate` · `/api/incidents` JSON · `/healthz`

주의: 인증이 없으므로 재생성 버튼은 사이트에 접근할 수 있는 누구나 누를 수 있다(LLM 호출을 일으킴). 외부에 공개한다면 리버스 프록시 등으로 `POST` 경로를 제한하라.
