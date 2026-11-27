"use client";

import { Fragment, useState, useRef, useEffect } from "react";
import { useRouter } from "next/navigation";
import Image from "next/image";
import Link from "next/link";
import homeData from "@/data/routes/home.json";
import { useQueryContext } from "./context/QueryContext";
import { useAuth } from "./context/AuthContext";
import AuthButtons from "./components/AuthButtons";
import PopularQuestions from "./components/PopularQuestions";
import { fetchIsAdmin } from "./lib/api";

const { header, hero, search, footer } = homeData.page;

const NAVY = "#25348B";
const NAVY_MUTED = "rgba(37,52,139,0.45)";

export default function HomePage() {
  const router = useRouter();
  const { setPendingQuery } = useQueryContext();
  const { user, loading: authLoading } = useAuth();
  const [query, setQuery] = useState("");
  // 관리자로 확인된 사용자 id. 로그아웃·계정 전환 시 user.id 와 달라져 자동으로 숨겨진다.
  const [adminUserId, setAdminUserId] = useState<string | null>(null);
  const isAdmin = !!user && adminUserId === user.id;
  const inputRef = useRef<HTMLInputElement>(null);

  // Admin 버튼은 관리자 이메일로 로그인했을 때만 보인다(판정은 백엔드 /admin/me).
  useEffect(() => {
    if (authLoading || !user) return;
    const controller = new AbortController();
    fetchIsAdmin(controller.signal).then((admin) => {
      if (!controller.signal.aborted) setAdminUserId(admin ? user.id : null);
    });
    return () => controller.abort();
  }, [user, authLoading]);

  // 로그인 상태면 홈 대신 바로 /chat 으로. loading 중에는 깜빡임 방지를 위해 대기.
  // 단, 채팅 화면의 로고를 눌러 일부러 홈으로 온 경우(?from=chat)는 머문다
  // (홈에서만 관리자 페이지로 갈 수 있어서). useSearchParams 는 페이지 전체를
  // Suspense 로 감싸야 해서 클라이언트에서만 도는 effect 안에서 직접 읽는다.
  useEffect(() => {
    if (authLoading || !user) return;
    const cameFromChat = new URLSearchParams(window.location.search).get("from") === "chat";
    if (!cameFromChat) router.replace("/chat");
  }, [user, authLoading, router]);

  const ask = (question: string) => {
    const q = question.trim();
    if (!q) return;
    setPendingQuery(q);
    router.push("/chat");
  };
  const handleSearch = () => ask(query);

  return (
    /* ── 그라데이션 배경 ── */
    <div className="bg-app min-h-[100dvh] flex items-center justify-center p-3 sm:p-4 lg:p-5">

      {/* ── 흰색 반투명 카드 ──
          padding 별 minHeight (dvh = 모바일 브라우저 UI를 뺀 실제 보이는 높이):
            mobile  (p-3): 12px×2 = 24px → calc(100dvh - 24px)
            tablet  (p-4): 16px×2 = 32px → calc(100dvh - 32px)
            desktop (p-5): 20px×2 = 40px → calc(100dvh - 40px)
      ── */}
      <div
        className="
          home-card w-full flex flex-col
          rounded-2xl sm:rounded-[24px]
          min-h-[calc(100dvh-24px)] sm:min-h-[calc(100dvh-32px)] lg:min-h-[calc(100dvh-40px)]
        "
        style={{ maxWidth: "860px" }}
      >

        {/* ── Header ── */}
        <header
          className="flex items-center justify-between px-4 py-3 sm:px-6 sm:py-4 lg:px-8 lg:py-5"
          style={{ borderBottom: "1px solid rgba(37,52,139,0.1)" }}
        >
          {/* 로고 — 좁은 화면에서 잘림 방지 */}
          <div className="flex items-center gap-2 sm:gap-2.5 min-w-0 flex-1 mr-2">
            <Image
              src="/pukyong_logo.png"
              alt="부경대학교 로고"
              width={32}
              height={32}
              className="flex-shrink-0 object-contain sm:w-9 sm:h-9"
              style={{ width: 32, height: 32 }}
            />
            <span
              className="text-sm sm:text-base font-semibold truncate min-w-0"
              style={{ color: NAVY }}
            >
              {header.logo}
            </span>
          </div>

          <div className="flex items-center gap-1 sm:gap-2 flex-shrink-0">
            {/*
              Admin 진입 버튼 — 관리자에게만 노출(isAdmin).

              router.push 대신 next/link Link 사용 이유:
              - hydration 완료 전에도 native <a> 클릭으로 동작 (intermittent 클릭 누락 해결)
              - Cmd/Ctrl+Click·우클릭 새 탭, viewport 진입 시 자동 prefetch 등 표준 동작 확보
            */}
            {isAdmin && (
              <Link
                href="/admin"
                prefetch
                className="
                  inline-flex items-center justify-center
                  px-2.5 sm:px-4 py-1 sm:py-1.5
                  rounded-full text-xs font-medium
                  transition-opacity hover:opacity-75
                "
                style={{
                  color: NAVY,
                  border: `1.5px solid ${NAVY}`,
                  background: "transparent",
                  textDecoration: "none",
                }}
              >
                Admin
              </Link>
            )}

            {/* 로그인 상태에 따라 Sign Up/Login 또는 사용자 메뉴 렌더 */}
            <AuthButtons />
          </div>
        </header>

        {/* ── Main ── */}
        <main className="flex-1 flex flex-col items-center justify-center px-4 sm:px-8 lg:px-10 gap-6 sm:gap-8">

          {/* Hero */}
          <div className="text-center flex flex-col gap-2 sm:gap-2.5">
            {/* break-keep: 한글 단어 중간("찾아|보세요")에서 줄이 끊기지 않게.
                title의 "\n" 은 모바일(sm 미만)에서만 줄바꿈되고, 그 이상에선 띄어쓰기로 이어진다. */}
            <h1
              className="break-keep text-2xl sm:text-3xl lg:text-[2.35rem] font-bold leading-tight tracking-tight"
              style={{ color: NAVY }}
            >
              {hero.title.split("\n").map((line, index) => (
                <Fragment key={index}>
                  {index > 0 && (
                    <>
                      {" "}
                      <br className="sm:hidden" />
                    </>
                  )}
                  {line}
                </Fragment>
              ))}
            </h1>
            <p
              className="text-xs sm:text-sm lg:text-base break-keep text-balance"
              style={{ color: NAVY_MUTED }}
            >
              {hero.subtitle}
            </p>
          </div>

          {/* 검색창 */}
          <div className="w-full sm:max-w-[480px] lg:max-w-[520px] flex flex-col gap-2 sm:gap-3">
            <div
              className="flex items-center rounded-xl sm:rounded-2xl px-3 sm:px-4 py-1.5 bg-white shadow-sm"
              style={{ border: "1px solid rgba(37,52,139,0.1)" }}
            >
              <input
                ref={inputRef}
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSearch()}
                placeholder={search.placeholder}
                className="flex-1 min-w-0 bg-transparent outline-none text-xs sm:text-sm py-2 sm:py-2.5 font-semibold"
                style={{ color: NAVY, caretColor: NAVY }}
              />
              <button
                onClick={handleSearch}
                disabled={!query.trim()}
                className="ml-1.5 flex-shrink-0 w-8 h-8 sm:w-9 sm:h-9 rounded-full flex items-center justify-center text-white transition-opacity disabled:opacity-35"
                style={{ background: NAVY }}
                aria-label="검색"
              >
                {/* 위쪽 화살표 아이콘 */}
                <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="8" y1="13" x2="8" y2="3" />
                  <polyline points="3,8 8,3 13,8" />
                </svg>
              </button>
            </div>

            {/* 자주 묻는 질문 — 누르면 바로 질문 */}
            <PopularQuestions onSelect={ask} />
          </div>
        </main>

        {/* ── Footer ── */}
        <footer
          className="py-4 sm:py-5 text-center text-[10px] sm:text-xs flex flex-col items-center gap-1.5"
          style={{ color: NAVY_MUTED }}
        >
          <span>{footer}</span>
        </footer>
      </div>
    </div>
  );
}
