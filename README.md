# 🏮 교양중국어2 강의 웹사이트

고려대학교 중어중문학과 「교양중국어2」 강의 소개·운영 사이트입니다.
강의 소개부터 커리큘럼, 공지, 회원가입, 출석·과제, Kahoot식 실시간 퀴즈, 관리자 화면까지 **`index.html` 한 파일**에 담았습니다.

## 🌐 사이트 주소

| 주소 | 비고 |
|---|---|
| https://jungimchang75-byte.github.io/chinese2-course/ | GitHub Pages |
| https://jungimchang75-byte.github.io/web_chinese/ | GitHub Pages (두 번째 저장소) |
| https://chinese2-course.vercel.app | Vercel |

> 현재 버전은 사이트 왼쪽 위 「교양중국어2」 옆의 빨간 글씨(예: `v1.1.8`)로 확인할 수 있습니다.

## ✨ 주요 기능

**방문자·수강생**
- 첫 화면: 과목 소개, 일정·시간·수업 방식·대상, 숫자 카드(16주, AI 도구, 강의 경력 등)
- 공지사항: 고정 공지를 맨 위에, 한 페이지 3개씩 번호로 넘겨 보기, NEW 표시, 팝업 공지
- 교과목 소개: 장점 슬라이드
- 커리큘럼: 16주 펼침 목록 (날짜·장소·학습 내용·참고 영상·과제·마감 카운트다운)
- 수업 달력: 매주 화요일 자동 표시, 휴강일 반영, 날짜를 누르면 그날 수업
- 주차 학습 자료(1~15주): 강의 노트 슬라이드 20장(프레젠테이션 모드), 과제, 중국어 문장 50개(번호·병음·번역·TTS·문법 설명), 온라인 퀴즈(객관식·OX·빈칸·순서, 결과 피드백·만점 폭죽), 실시간 퀴즈, 문화 슬라이드 10장, 슬라이드마다 그림 1장, 유튜브 영상·노래(AI 추천 각 4개, 실제 영상 확인), 모든 주차 일괄 생성, 자료마다 직접 편집·AI 수정 요청
- 주제 투표: 막대그래프 결과
- 회원가입: 아이디·비밀번호·휴대폰(하이픈 자동) 등, 빠진 항목 안내, 비밀번호는 해시로만 저장
- 내 강의실: 아이디·비밀번호 로그인, 출석 체크, 과제 제출(구글 드라이브 공유 링크)
- 실시간 퀴즈: QR·PIN 입장, 휴대폰으로 답하기, 빨리 맞힐수록 높은 점수, 순위·시상대, 결과 CSV
- 첫 방문 폭죽, 안내 팝업(오늘 하루 보지 않기), 맨 위로 버튼, 휴대폰 화면 대응

**관리자** (오른쪽 위 🔒 → 비밀번호)
- 사이트 정보·섹션 내용을 화면에서 바로 수정 (저장할 때마다 버전 자동 증가)
- 수강생 명단 (추가·CSV 불러오기·선택/전체 삭제), 공지 (올리기·수정·고정·팝업)
- 출석·과제·회원 목록 확인과 엑셀용 CSV 내려받기
- API 키 관리 (Claude API 키 상태, BAZE API 키 입력·연결 확인)
- 주차별 AI 생성 버튼과 참고 자료 업로드(PDF·PPTX·DOCX·XLSX·HWPX·HTML) — 올린 자료를 반드시 참고해 생성
- 설정 파일 저장·불러오기, 수정 내용을 담은 `index.html` 내려받기, 비밀번호 변경

## 📁 파일 구성

| 파일 | 설명 |
|---|---|
| `weeks/content.js` | 주차 학습 자료 (AI 생성 결과, `weeks/content.json`이 원본) |
| `index.html` | 사이트 전체 (설정 + 디자인 + 기능). 설정은 `/* CONFIG-START */` ~ `/* CONFIG-END */` 사이 |
| `server.py` | 실시간 퀴즈·AI 문제 출제 서버 (Python 표준 라이브러리 + `anthropic`) |
| `requirements.txt` | 서버에 필요한 패키지 |
| `Procfile` | Railway 등에서 서버 실행 명령 |
| `사용설명서.md` | 자세한 사용법·배포 방법 |
| `기획서_교양중국어.md` | 처음 기획 메모 |

## 🚀 실행 방법

**사이트만 보기**: `index.html`을 더블클릭하면 실시간 퀴즈를 뺀 모든 기능이 작동합니다.

**실시간 퀴즈·AI 출제까지**:

```bash
pip install -r requirements.txt
```

```bash
python server.py
```

브라우저에서 http://localhost:8000 을 엽니다. 같은 Wi-Fi의 휴대폰은 화면에 표시되는 내부 주소로 접속합니다.

## 🔑 환경변수 (server.py)

| 이름 | 설명 |
|---|---|
| `PORT` | 포트 번호 (기본 8000) |
| `QUIZ_HOST_PASSWORD` | 퀴즈 만들기·AI 출제 비밀번호 (배포 시 꼭 설정) |
| `ANTHROPIC_API_KEY` | AI 문제 출제용 Claude API 키 |
| `BAZE_API_KEY` | (선택) 고려대학교 BAZE API Gateway 키. 관리자 화면에서도 입력 가능 |

AI 문제 출제 모델: Claude Opus 5.5 (`claude-opus-5-5`)
BAZE 게이트웨이: `https://api.baze.co/v1/gateway/claude` ([개발자 문서](https://docs.factchat.kr/docs/korea-seoul/api-gateway/getting-started/overview#api-gateway))

## ⚠️ 알아 둘 점

- 서버 없는 정적 사이트라 **투표·회원·출석·과제 기록은 각 브라우저에만 저장**됩니다. 수강생 전체가 공유하려면 서버 데이터베이스 연결이 필요합니다.
- 관리자 화면에서 고친 내용은 그 브라우저에만 적용됩니다. 모두에게 보이게 하려면 「index.html 내려받기」로 받은 파일을 저장소에 올립니다.
- 실시간 퀴즈는 `server.py`가 계속 켜져 있는 곳(이 PC 또는 Railway)에서 안정적으로 작동합니다. GitHub Pages에서는 작동하지 않고, Vercel에서는 불안정할 수 있습니다.
- 설정이 꼬였을 때는 주소 끝에 `?reset-config`를 붙여 열면 원래 설정으로 돌아갑니다.

## 🛠 수정·배포

저장소에 커밋하면 GitHub Pages와 Vercel이 자동으로 다시 배포합니다. 수정할 때마다 `index.html`의 `site.version`을 올려 커밋합니다.

---

Copyright 2026 ⓒ AI WEB, All Rights Reserved.
