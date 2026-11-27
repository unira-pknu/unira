# 프론트엔드

코드는 [`frontend/`](../../frontend)에 있습니다. Next.js App Router와 React를 사용하며,
채팅 답변은 백엔드 API, 로그인과 채팅 이력은 Supabase에 연결합니다.

## 실행

Node.js와 npm이 설치된 환경에서 `frontend/` 폴더를 기준으로 실행합니다.

```powershell
npm ci
npm run dev
```

개발 화면은 `http://localhost:3000`에서 확인합니다.

```powershell
npm run lint
npm run build
npm start
```

## 연결 설정

Supabase 설정은 `frontend/.env.local`에서 읽으며, 저장소 안에서 실행할 때는
`backend/.env`의 공개 클라이언트 설정도 사용할 수 있습니다.

```env
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=<project anon public key>
```

브라우저에는 공개 URL과 anon key만 전달합니다. `DATABASE_URL`과 service role key는
프론트엔드에 설정하지 않습니다. 백엔드 연결 방식은
[`app/lib/api.ts`](../../frontend/app/lib/api.ts)와
[`app/api/ask/route.ts`](../../frontend/app/api/ask/route.ts)에서 관리합니다.

## 화면과 데이터

| 경로 | 데이터 |
|---|---|
| `/` | 안내 문구와 추천 질문. 인기 질문은 API에서 조회 |
| `/auth` | Supabase 로그인·회원가입 |
| `/chat` | 백엔드 답변, Supabase 채팅 이력·피드백 |
| `/admin` | 관리자 API의 실제 피드백·질문 통계, 싫어요 학습 후보 JSONL 다운로드 |
| `/docs` | 문서 목록 연동 전 준비 화면. 예시 문서는 표시하지 않음 |

관리자 화면에는 파일 업로드가 없습니다. 저장 API 없이 버튼만 있던 칸을 2026-11-11에 없앴습니다.
자료는 수집 파이프라인으로만 들어옵니다.

피드백 로그의 **학습 후보 내려받기 (JSONL)**는 현재 싫어요를 모두 조회해 검수 초안을 제공합니다.
정답 검수와 학습/평가 파일 생성은 [피드백 학습 데이터](../evaluation/FEEDBACK_TRAINING.md)를 따릅니다.

`data/routes/home.json`과 `chat.json`은 화면 문구를 관리합니다. `docs.json`은 문서 목록과
수집 시각을 제공할 연결 지점이며, 현재 목록은 비어 있습니다. 실제 채팅 답변·피드백·문서를
가짜 데이터로 대체하지 않습니다.
