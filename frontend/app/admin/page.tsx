"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  AdminApiError,
  fetchAdminDashboard,
  type AdminCommentEntry,
  type AdminDashboard,
  type AdminThumbsEntry,
  type AdminTopQuestion,
} from "@/app/lib/api";

const NAVY = "#25348B";
const NAVY_MUTED = "rgba(37,52,139,0.45)";
const DOWN_RED = "#c0392b";

// ── 타입 ─────────────────────────────────────────────────────────────────────
// 피드백·질문 데이터는 백엔드 /admin/dashboard 실데이터 (message_feedback, chat_messages).
type ThumbsEntry = AdminThumbsEntry;
type CommentEntry = AdminCommentEntry;
type TopQuestion = AdminTopQuestion;
const EMPTY_THUMBS: ThumbsEntry[] = [];
const EMPTY_COMMENTS: CommentEntry[] = [];
const EMPTY_TOP_QUESTIONS: TopQuestion[] = [];
const THUMBS_FILTER_OPTIONS = [
  { value: "all" as const, label: "전체" },
  { value: "up" as const, label: "좋아요" },
  { value: "down" as const, label: "싫어요" },
] satisfies readonly { value: "all" | "up" | "down"; label: string }[];

// ── 아이콘 ────────────────────────────────────────────────────────────────────
const IconArrowLeft = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <line x1="13" y1="8" x2="3" y2="8" />
    <polyline points="7,3 2,8 7,13" />
  </svg>
);
const IconThumbUp = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3" />
  </svg>
);
const IconThumbDown = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17" />
  </svg>
);
const IconClipboardCheck = () => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="10" height="12" rx="1.2" />
    <path d="M6 2.5h4a1 1 0 011 1v1H5v-1a1 1 0 011-1z" />
    <polyline points="6,9 7.5,10.5 10.5,7.5" />
  </svg>
);
const IconQuote = () => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 8c0-2.5 1.5-4 4-4v2c-1.5 0-2 1-2 2h2v4H3V8z" />
    <path d="M9 8c0-2.5 1.5-4 4-4v2c-1.5 0-2 1-2 2h2v4H9V8z" />
  </svg>
);
const IconChartBar = () => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <line x1="2" y1="14" x2="14" y2="14" />
    <rect x="3" y="9" width="2.5" height="4" />
    <rect x="6.75" y="6" width="2.5" height="7" />
    <rect x="10.5" y="3" width="2.5" height="10" />
  </svg>
);
const IconUpload = ({ size = 14 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <line x1="8" y1="11" x2="8" y2="2" />
    <polyline points="4.5,5.5 8,2 11.5,5.5" />
    <path d="M2 11v2a1 1 0 001 1h10a1 1 0 001-1v-2" />
  </svg>
);
const IconCheckCircle = () => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="8" cy="8" r="6" />
    <polyline points="5,8 7.5,10.5 11,6.5" />
  </svg>
);
const IconMessageSquare = () => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 10a1 1 0 01-1 1H5l-3 3V3a1 1 0 011-1h10a1 1 0 011 1v7z" />
  </svg>
);
const IconStat = () => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="2,12 6,7 9,9 14,3" />
    <polyline points="10,3 14,3 14,7" />
  </svg>
);
const IconChevronLeft = () => (
  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="7.5,2 3.5,6 7.5,10" />
  </svg>
);
const IconChevronRight = () => (
  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="4.5,2 8.5,6 4.5,10" />
  </svg>
);

const PAGE_SIZE = 10;

// ── 유틸 ─────────────────────────────────────────────────────────────────────
function formatDateTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// ── 공용 컴포넌트 ────────────────────────────────────────────────────────────
function FilterPills<V extends string>({
  value,
  onChange,
  options,
}: {
  value: V;
  onChange: (v: V) => void;
  options: readonly { value: V; label: string }[];
}) {
  return (
    <div
      className="inline-flex p-0.5 rounded-xl"
      style={{
        background: "rgba(255,255,255,0.55)",
        border: "1px solid rgba(255,255,255,0.88)",
        backdropFilter: "blur(12px)",
        WebkitBackdropFilter: "blur(12px)",
      }}
    >
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            className="px-2.5 sm:px-3 py-1 sm:py-1.5 rounded-lg text-[11px] sm:text-xs font-semibold transition-colors whitespace-nowrap"
            style={{
              background: active ? NAVY : "transparent",
              color: active ? "white" : NAVY_MUTED,
            }}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

function Pagination({
  page,
  totalPages,
  onChange,
}: {
  page: number;
  totalPages: number;
  onChange: (page: number) => void;
}) {
  if (totalPages <= 1) return null;

  const navButtonClass =
    "flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-semibold transition-opacity disabled:cursor-not-allowed disabled:opacity-40";
  const navButtonStyle = {
    color: NAVY,
    background: "rgba(255,255,255,0.6)",
    border: "1px solid rgba(255,255,255,0.85)",
  };

  return (
    <nav
      className="flex items-center justify-center gap-3 mt-3 pt-3"
      style={{ borderTop: "1px solid rgba(37,52,139,0.06)" }}
      aria-label="페이지네이션"
    >
      <button
        type="button"
        onClick={() => onChange(page - 1)}
        disabled={page <= 1}
        className={navButtonClass}
        style={navButtonStyle}
        aria-label="이전 페이지"
      >
        <IconChevronLeft />
        이전
      </button>
      <span
        className="text-xs sm:text-sm font-semibold tabular-nums"
        style={{ color: NAVY }}
        aria-live="polite"
      >
        {page}{" "}
        <span style={{ color: NAVY_MUTED, fontWeight: 500 }}>/ {totalPages}</span>
      </span>
      <button
        type="button"
        onClick={() => onChange(page + 1)}
        disabled={page >= totalPages}
        className={navButtonClass}
        style={navButtonStyle}
        aria-label="다음 페이지"
      >
        다음
        <IconChevronRight />
      </button>
    </nav>
  );
}

function StatCard({
  label,
  value,
  hint,
  icon,
}: {
  label: string;
  value: string;
  hint?: string;
  icon: ReactNode;
}) {
  return (
    <div
      className="rounded-2xl p-4 sm:p-5 flex flex-col gap-1.5"
      style={{
        background: "rgba(255,255,255,0.58)",
        backdropFilter: "blur(16px)",
        WebkitBackdropFilter: "blur(16px)",
        border: "1px solid rgba(255,255,255,0.88)",
      }}
    >
      <div className="flex items-center justify-between">
        <p
          className="text-[10px] sm:text-[11px] font-semibold uppercase tracking-wide"
          style={{ color: NAVY_MUTED }}
        >
          {label}
        </p>
        <span
          className="w-6 h-6 sm:w-7 sm:h-7 rounded-md flex items-center justify-center"
          style={{ background: "rgba(37,52,139,0.08)", color: NAVY }}
        >
          {icon}
        </span>
      </div>
      <p
        className="text-xl sm:text-2xl lg:text-[1.625rem] font-bold tabular-nums leading-tight"
        style={{ color: NAVY }}
      >
        {value}
      </p>
      {hint && (
        <p className="text-[10px] sm:text-[11px]" style={{ color: NAVY_MUTED }}>
          {hint}
        </p>
      )}
    </div>
  );
}

function SectionCard({
  icon,
  title,
  action,
  children,
}: {
  icon: ReactNode;
  title: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section
      className="rounded-2xl p-4 sm:p-5"
      style={{
        background: "rgba(255,255,255,0.58)",
        backdropFilter: "blur(16px)",
        WebkitBackdropFilter: "blur(16px)",
        border: "1px solid rgba(255,255,255,0.88)",
      }}
    >
      <header className="flex items-center justify-between gap-2 mb-3 sm:mb-4 flex-wrap">
        <div className="flex items-center gap-2">
          <span
            className="w-7 h-7 sm:w-8 sm:h-8 rounded-lg flex items-center justify-center"
            style={{ background: "rgba(37,52,139,0.08)", color: NAVY }}
          >
            {icon}
          </span>
          <h2 className="text-sm sm:text-base font-bold" style={{ color: NAVY }}>
            {title}
          </h2>
        </div>
        {action && <div className="flex-shrink-0">{action}</div>}
      </header>
      {children}
    </section>
  );
}

// ── 섹션별 카드 ──────────────────────────────────────────────────────────────
function ThumbsCard({ entry }: { entry: ThumbsEntry }) {
  const isUp = entry.kind === "up";
  return (
    <div
      className="rounded-xl px-3 py-3 sm:px-4 sm:py-3.5 flex items-start gap-3"
      style={{
        background: "rgba(255,255,255,0.55)",
        border: "1px solid rgba(255,255,255,0.88)",
      }}
    >
      <div
        className="flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center"
        style={{
          background: isUp ? "rgba(37,52,139,0.10)" : "rgba(192,57,43,0.10)",
          color: isUp ? NAVY : DOWN_RED,
        }}
        aria-label={isUp ? "좋아요 피드백" : "싫어요 피드백"}
      >
        {isUp ? <IconThumbUp /> : <IconThumbDown />}
      </div>

      <div className="flex-1 min-w-0 flex flex-col gap-1">
        <p
          className="text-xs sm:text-sm font-semibold leading-snug truncate"
          style={{ color: NAVY }}
        >
          {entry.question}
        </p>
        <p
          className="text-[11px] sm:text-xs leading-relaxed line-clamp-2"
          style={{ color: NAVY_MUTED }}
        >
          {entry.answer}
        </p>
        {!isUp && entry.reasons.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-1">
            {entry.reasons.map((r) => (
              <span
                key={r}
                className="px-2 py-0.5 rounded-full text-[10px] font-medium"
                style={{ background: "rgba(192,57,43,0.08)", color: DOWN_RED }}
              >
                {r}
              </span>
            ))}
          </div>
        )}
      </div>

      <span
        className="flex-shrink-0 text-[10px] sm:text-[11px] whitespace-nowrap"
        style={{ color: NAVY_MUTED }}
      >
        {formatDateTime(entry.createdAt)}
      </span>
    </div>
  );
}

function CommentCard({ entry }: { entry: CommentEntry }) {
  return (
    <div
      className="rounded-xl px-3.5 py-3 sm:px-4 sm:py-3.5 flex flex-col gap-2"
      style={{
        background: "rgba(255,255,255,0.55)",
        border: "1px solid rgba(255,255,255,0.88)",
      }}
    >
      <blockquote
        className="rounded-r-md border-l-[3px] py-1.5 pl-3 pr-2"
        style={{ borderColor: NAVY, background: "rgba(37,52,139,0.04)" }}
      >
        <p className="text-xs sm:text-sm leading-relaxed" style={{ color: "#334155" }}>
          {entry.comment}
        </p>
      </blockquote>
      <div className="flex items-center justify-between gap-2 flex-wrap">
        {entry.relatedQuestion ? (
          <p
            className="text-[10px] sm:text-[11px] truncate min-w-0"
            style={{ color: NAVY_MUTED }}
          >
            관련 질문: <span style={{ color: NAVY, fontWeight: 600 }}>{entry.relatedQuestion}</span>
          </p>
        ) : (
          <span />
        )}
        <span
          className="text-[10px] sm:text-[11px] whitespace-nowrap"
          style={{ color: NAVY_MUTED }}
        >
          {formatDateTime(entry.createdAt)}
        </span>
      </div>
    </div>
  );
}

function TopQuestionRow({
  rank,
  entry,
  maxCount,
}: {
  rank: number;
  entry: TopQuestion;
  maxCount: number;
}) {
  const ratio = maxCount > 0 ? entry.count / maxCount : 0;
  const isPodium = rank <= 3;
  return (
    <div className="flex items-center gap-2.5 sm:gap-3.5">
      <span
        className="flex-shrink-0 w-5 sm:w-6 text-right text-sm sm:text-base font-bold tabular-nums"
        style={{ color: isPodium ? NAVY : NAVY_MUTED }}
      >
        {rank}
      </span>
      <div className="flex-1 min-w-0 flex flex-col gap-1.5">
        <p
          className="text-[11px] sm:text-sm leading-snug truncate font-medium"
          style={{ color: NAVY }}
        >
          {entry.question}
        </p>
        <div
          className="h-1.5 rounded-full overflow-hidden"
          style={{ background: "rgba(37,52,139,0.08)" }}
        >
          <div
            className="h-full rounded-full"
            style={{
              width: `${Math.max(ratio * 100, 4)}%`,
              background: NAVY,
              opacity: isPodium ? 1 : 0.65,
              transition: "width 0.3s ease",
            }}
          />
        </div>
      </div>
      <span
        className="flex-shrink-0 text-[11px] sm:text-xs font-semibold tabular-nums"
        style={{ color: NAVY }}
      >
        {entry.count}
      </span>
    </div>
  );
}

// ── 메인 페이지 ──────────────────────────────────────────────────────────────
// 로딩·권한 없음·오류 안내 패널
function DashboardStatus({
  message,
  tone = "muted",
  action,
}: {
  message: string;
  tone?: "muted" | "error";
  action?: ReactNode;
}) {
  return (
    <div
      className="flex flex-col items-center gap-2 rounded-2xl px-4 py-8 text-center text-xs sm:text-sm"
      style={{
        background: "rgba(255,255,255,0.45)",
        color: tone === "error" ? DOWN_RED : NAVY_MUTED,
        border: "1px solid rgba(255,255,255,0.7)",
      }}
      role={tone === "error" ? "alert" : "status"}
    >
      <p>{message}</p>
      {action}
    </div>
  );
}

export default function AdminPage() {
  const router = useRouter();

  // ── 실데이터 로드 (관리자만: 백엔드가 ADMIN_EMAILS 로 판정) ─────────────────
  const [dashboard, setDashboard] = useState<AdminDashboard | null>(null);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [loadError, setLoadError] = useState<{ status: number; message: string } | null>(null);

  const loadDashboard = useCallback(async (signal?: AbortSignal) => {
    setLoadState("loading");
    setLoadError(null);
    try {
      const data = await fetchAdminDashboard(signal);
      setDashboard(data);
      setLoadState("ready");
    } catch (err) {
      if (signal?.aborted) return;
      const status = err instanceof AdminApiError ? err.status : 0;
      const message = err instanceof Error ? err.message : "대시보드를 불러오지 못했습니다.";
      setLoadError({ status, message });
      setLoadState("error");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    // 마운트 시 1회 조회. setState 는 비동기 응답 이후에만 일어난다.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadDashboard(controller.signal);
    return () => controller.abort();
  }, [loadDashboard]);

  const summary = dashboard?.summary ?? null;
  const allThumbs = dashboard?.feedback.thumbs ?? EMPTY_THUMBS;
  const comments = dashboard?.feedback.comments ?? EMPTY_COMMENTS;
  const topQuestions = dashboard?.topQuestions ?? EMPTY_TOP_QUESTIONS;

  const [thumbsFilter, setThumbsFilter] = useState<"all" | "up" | "down">("all");

  // 페이지네이션 상태
  const [thumbsPage, setThumbsPage] = useState(1);
  const [commentsPage, setCommentsPage] = useState(1);

  // 필터가 바뀌면 1페이지로 되돌린다 (현재 페이지가 결과 범위를 벗어나는 문제 방지)
  const handleThumbsFilterChange = (value: "all" | "up" | "down") => {
    setThumbsFilter(value);
    setThumbsPage(1);
  };

  const filteredThumbs = useMemo(
    () =>
      thumbsFilter === "all"
        ? allThumbs
        : allThumbs.filter((t) => t.kind === thumbsFilter),
    [allThumbs, thumbsFilter],
  );

  const thumbsTotalPages = Math.max(1, Math.ceil(filteredThumbs.length / PAGE_SIZE));
  const visibleThumbs = filteredThumbs.slice(
    (thumbsPage - 1) * PAGE_SIZE,
    thumbsPage * PAGE_SIZE,
  );

  const commentsTotalPages = Math.max(1, Math.ceil(comments.length / PAGE_SIZE));
  const visibleComments = comments.slice(
    (commentsPage - 1) * PAGE_SIZE,
    commentsPage * PAGE_SIZE,
  );

  // 인기 질문은 서버가 최근 질문 표본(questionSampleSize)으로 집계한다.
  const visibleTopQuestions = topQuestions;
  const maxQuestionCount = visibleTopQuestions.reduce(
    (acc, q) => (q.count > acc ? q.count : acc),
    0,
  );

  const satisfactionPct =
    summary && summary.satisfactionRate !== null ? `${Math.round(summary.satisfactionRate * 100)}%` : "-";

  return (
    <div className="bg-app min-h-[100dvh] flex flex-col">
      {/* ── 헤더 ── */}
      <header
        className="flex items-center justify-between px-4 py-3 sm:px-6 sm:py-4 lg:px-8 lg:py-5 flex-shrink-0"
        style={{ borderBottom: "1px solid rgba(37,52,139,0.08)" }}
      >
        <span className="text-sm sm:text-base font-semibold" style={{ color: NAVY }}>
          부경대학교
        </span>
      </header>

      {/* ── 본문 ── */}
      <main className="flex-1 w-full max-w-4xl mx-auto px-4 py-6 sm:px-6 sm:py-8 flex flex-col gap-5 sm:gap-6">
        {/* 뒤로가기 + 페이지 타이틀 */}
        <div className="flex flex-col gap-3">
          <button
            onClick={() => router.back()}
            className="flex items-center gap-1.5 text-sm font-medium w-fit transition-opacity hover:opacity-70"
            style={{ color: NAVY_MUTED }}
          >
            <IconArrowLeft />
            뒤로가기
          </button>

          <div className="flex items-end justify-between gap-3">
            <div className="flex flex-col gap-1">
              <h1 className="text-xl sm:text-2xl font-bold" style={{ color: NAVY }}>
                관리자 대시보드
              </h1>
              {summary && (
                <p className="text-[11px] sm:text-xs" style={{ color: NAVY_MUTED }}>
                  마지막 업데이트: {formatDateTime(summary.lastUpdatedAt)}
                </p>
              )}
            </div>
            {loadState !== "loading" && loadError?.status !== 401 && loadError?.status !== 403 && (
              <button
                type="button"
                onClick={() => void loadDashboard()}
                className="shrink-0 rounded-lg px-3 py-1.5 text-xs font-semibold transition-opacity hover:opacity-75"
                style={{ color: NAVY, background: "rgba(255,255,255,0.6)", border: `1.5px solid ${NAVY}` }}
              >
                새로고침
              </button>
            )}
          </div>
        </div>

        {loadState === "loading" && <DashboardStatus message="대시보드를 불러오는 중이에요..." />}

        {loadState === "error" && loadError && (
          <DashboardStatus
            tone={loadError.status === 403 || loadError.status === 401 ? "muted" : "error"}
            message={
              loadError.status === 401
                ? "로그인이 필요합니다."
                : loadError.status === 403
                  ? "관리자만 볼 수 있는 페이지예요. 관리자 계정으로 로그인해 주세요."
                  : loadError.message
            }
            action={
              loadError.status === 401 ? (
                <Link href="/auth?next=/admin" className="text-xs font-semibold underline" style={{ color: NAVY }}>
                  로그인하러 가기
                </Link>
              ) : loadError.status === 403 ? null : (
                <button
                  type="button"
                  onClick={() => void loadDashboard()}
                  className="text-xs font-semibold underline"
                  style={{ color: NAVY }}
                >
                  다시 시도
                </button>
              )
            }
          />
        )}

        {loadState === "ready" && summary && (
        <>
        {/* ── 1. 요약 통계 ── */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <StatCard
            label="총 피드백"
            value={summary.totalFeedback.toLocaleString("ko-KR")}
            hint={`좋아요 ${summary.upCount.toLocaleString("ko-KR")} · 싫어요 ${summary.downCount.toLocaleString("ko-KR")}`}
            icon={<IconStat />}
          />
          <StatCard
            label="만족도"
            value={satisfactionPct}
            hint={summary.totalFeedback ? `좋아요 비율 · ${summary.totalFeedback}건 기준` : "아직 피드백이 없어요"}
            icon={<IconCheckCircle />}
          />
          <StatCard
            label="누적 질문"
            value={summary.totalQuestions.toLocaleString("ko-KR")}
            hint="누적 사용자 질문 수"
            icon={<IconMessageSquare />}
          />
        </div>

        {/* ── 2. 피드백 로그 ── */}
        <SectionCard
          icon={<IconClipboardCheck />}
          title="피드백 로그"
          action={
            <FilterPills
              value={thumbsFilter}
              onChange={handleThumbsFilterChange}
              options={THUMBS_FILTER_OPTIONS}
            />
          }
        >
          {filteredThumbs.length === 0 ? (
            <div
              className="rounded-xl py-6 text-center text-xs sm:text-sm"
              style={{ background: "rgba(255,255,255,0.45)", color: NAVY_MUTED }}
            >
              해당 조건의 피드백이 없습니다.
            </div>
          ) : (
            <>
              <div className="flex flex-col gap-2">
                {visibleThumbs.map((entry) => (
                  <ThumbsCard key={entry.id} entry={entry} />
                ))}
              </div>
              <Pagination
                page={thumbsPage}
                totalPages={thumbsTotalPages}
                onChange={setThumbsPage}
              />
            </>
          )}
        </SectionCard>

        {/* ── 3. 텍스트 피드백 ── */}
        <SectionCard icon={<IconQuote />} title="텍스트 피드백">
          {comments.length === 0 ? (
            <div
              className="rounded-xl py-6 text-center text-xs sm:text-sm"
              style={{ background: "rgba(255,255,255,0.45)", color: NAVY_MUTED }}
            >
              아직 입력된 의견이 없습니다.
            </div>
          ) : (
            <>
              <div className="flex flex-col gap-2">
                {visibleComments.map((entry) => (
                  <CommentCard key={entry.id} entry={entry} />
                ))}
              </div>
              <Pagination
                page={commentsPage}
                totalPages={commentsTotalPages}
                onChange={setCommentsPage}
              />
            </>
          )}
        </SectionCard>

        {/* ── 4. 인기 질문 Top 10 ── */}
        <SectionCard
          icon={<IconChartBar />}
          title="인기 질문 Top 10"
          action={
            <span className="text-[10px] sm:text-[11px]" style={{ color: NAVY_MUTED }}>
              최근 질문 {summary.questionSampleSize.toLocaleString("ko-KR")}개 기준
            </span>
          }
        >
          <div className="flex flex-col gap-3 sm:gap-3.5">
            {visibleTopQuestions.length === 0 && (
              <div
                className="rounded-xl py-6 text-center text-xs sm:text-sm"
                style={{ background: "rgba(255,255,255,0.45)", color: NAVY_MUTED }}
              >
                아직 질문이 없습니다.
              </div>
            )}
            {visibleTopQuestions.map((q, i) => (
              <TopQuestionRow
                key={q.id}
                rank={i + 1}
                entry={q}
                maxCount={maxQuestionCount}
              />
            ))}
          </div>
        </SectionCard>

        {/* ── 5. 파일 업로드 ── */}
        <SectionCard icon={<IconUpload />} title="파일 업로드">
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs sm:text-sm" style={{ color: NAVY_MUTED }}>
              파일 업로드 기능을 준비 중입니다.
            </p>
            <button
              type="button"
              disabled
              className="px-4 py-1.5 rounded-lg text-xs font-semibold text-white opacity-40 cursor-not-allowed"
              style={{ background: NAVY }}
            >
              업로드 준비 중
            </button>
          </div>
        </SectionCard>
        </>
        )}
      </main>

      {/* ── 푸터 ── */}
      <footer
        className="py-4 text-center text-[10px] sm:text-xs flex-shrink-0"
        style={{ color: NAVY_MUTED }}
      >
        관리자 전용 화면 · 피드백·질문 통계는 실시간 데이터 (파일 업로드는 준비 중)
      </footer>
    </div>
  );
}
