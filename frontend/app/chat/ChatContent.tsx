"use client";

import { useState, useEffect, useMemo, useRef, type CSSProperties } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import chatData from "@/data/routes/chat.json";
import { useQueryContext } from "@/app/context/QueryContext";
import { displayNameOf, useAuth } from "@/app/context/AuthContext";
import { AskError, askBackend, type ChatTurn } from "@/app/lib/api";
import { prepareSources } from "./mergeSources";
import { sourceKindLabel } from "./sourceLabel";
import PopularQuestions from "@/app/components/PopularQuestions";
import {
  createChat,
  deleteChat,
  deleteFeedback,
  fetchChatMessages,
  fetchFeedbackForMessages,
  insertChatMessage,
  saveFeedback,
  type MessageFeedback,
  listChats,
  parseStoredSources,
  updateChatTitle,
} from "@/app/lib/chatHistory";

// ── 타입 ─────────────────────────────────────────────────────────────────────
interface Attachment {
  name: string;
  url: string;
}

interface Source {
  id: number;
  title: string;
  category: string;
  /** e.g. "학과 홈페이지 · 공지사항" — 없으면 주소로 정한 종류(sourceKindLabel) */
  chipMeta?: string;
  quote: string;
  quoteSource?: string;
  url: string;
  attachments: Attachment[];
}

interface Message {
  /** 화면용 로컬 id (React key) */
  id: string;
  /** DB(chat_messages) id — 저장이 끝난 메시지에만 있다. 피드백은 이 id 에 연결된다. */
  dbId?: string;
  role: "user" | "assistant";
  content: string;
  attachments?: Attachment[];
  sources?: Source[];
  /** 내가 이 답변에 남긴 피드백 (없으면 null/undefined) */
  feedback?: MessageFeedback | null;
}

interface HistoryItem {
  id: string;
  title: string;
}

const CHAT_TITLE_MAX_LEN = 80;
const SIDEBAR_DEFAULT_WIDTH = 210;
const SIDEBAR_MIN_WIDTH = 180;
const SIDEBAR_MAX_WIDTH = 420;

function chatTitleFromQuestion(question: string): string {
  const trimmed = question.trim().replace(/\s+/g, " ");
  if (trimmed.length <= CHAT_TITLE_MAX_LEN) return trimmed;
  return trimmed.slice(0, CHAT_TITLE_MAX_LEN) + "…";
}

/** persisted chat row id (uuid v4) */
function isPersistedChatId(id: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id);
}

// "loading": JSON 답변 대기 ·
// "success"/"error": 완료 상태
type ChatState = "idle" | "loading" | "success" | "error";

// ── 고유 ID ───────────────────────────────────────────────────────────────────
let _seq = 0;
const uid = () => `${Date.now()}-${++_seq}`;

const { sidebar, chat: chatMeta } = chatData;
/** JSON의 `history: []`가 `never[]`로 추론되는 것을 피하기 위한 단언 */
const sidebarHistory = sidebar.history as HistoryItem[];

// ── 아이콘 ────────────────────────────────────────────────────────────────────
const IconSidebar = () => (
  <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6">
    <rect x="2" y="2" width="14" height="14" rx="2" />
    <line x1="7" y1="2" x2="7" y2="16" />
  </svg>
);
const IconArrowLeft = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <line x1="13" y1="8" x2="3" y2="8" />
    <polyline points="7,4 3,8 7,12" />
  </svg>
);
const IconPlus = () => (
  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2">
    <line x1="7" y1="1" x2="7" y2="13" />
    <line x1="1" y1="7" x2="13" y2="7" />
  </svg>
);
const IconSearch = () => (
  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8">
    <circle cx="6" cy="6" r="4.5" />
    <line x1="9.5" y1="9.5" x2="13" y2="13" />
  </svg>
);
const IconSend = () => (
  <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2">
    <line x1="8" y1="14" x2="8" y2="3" />
    <polyline points="3,8 8,3 13,8" />
  </svg>
);
const IconStar = () => (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6">
    <polygon points="7,1 8.8,5.2 13.5,5.6 10.1,8.7 11.1,13.3 7,10.9 2.9,13.3 3.9,8.7 0.5,5.6 5.2,5.2" />
  </svg>
);
const IconPencil = () => (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M9.5 2.5 L11.5 4.5 L4.5 11.5 L2 12 L2.5 9.5 Z" />
    <line x1="8" y1="4" x2="10" y2="6" />
  </svg>
);
const IconTrash = () => (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6">
    <polyline points="1,3 3,3 13,3" />
    <path d="M4 3V2a1 1 0 011-1h4a1 1 0 011 1v1" />
    <rect x="2.5" y="4" width="9" height="8.5" rx="1" />
    <line x1="5.5" y1="6.5" x2="5.5" y2="10" />
    <line x1="8.5" y1="6.5" x2="8.5" y2="10" />
  </svg>
);
const IconDownload = () => (
  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8">
    <line x1="6" y1="1" x2="6" y2="8" />
    <polyline points="3,5.5 6,8 9,5.5" />
    <line x1="2" y1="11" x2="10" y2="11" />
  </svg>
);
const IconUser = () => (
  <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8">
    <circle cx="7" cy="4.5" r="2.5" />
    <path d="M2 12.5c0-2.76 2.24-5 5-5s5 2.24 5 5" />
  </svg>
);
const IconCopy = () => (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <rect x="4.5" y="4.5" width="8" height="8.5" rx="1.2" />
    <path d="M2 9.5V2a1 1 0 011-1h7" />
  </svg>
);
const IconCheckSm = () => (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="2,7 5.5,10.5 12,4" />
  </svg>
);
const IconThumbUp = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3" />
  </svg>
);
const IconThumbDown = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17" />
  </svg>
);


// ── 싫어요 피드백 모달 ────────────────────────────────────────────────────────
const FEEDBACK_TYPES = [
  "정보가 부정확해요",
  "출처가 잘못됐어요",
  "답변이 너무 짧아요",
  "원하는 답변이 아니에요",
];

const FEEDBACK_COMMENT_MAX = 1000; // DB 제약(message_feedback_comment_length)과 같은 값

function DislikeFeedbackModal({
  onClose,
  onSubmit,
  initial,
}: {
  onClose: () => void;
  onSubmit: (reasons: string[], comment: string | null) => void;
  initial?: MessageFeedback | null;
}) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(initial?.reasons ?? []));
  const [comment, setComment] = useState(initial?.comment ?? "");

  const toggle = (type: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  const handleSubmit = () => {
    // 선택 순서와 무관하게 화면 순서대로 저장
    const reasons = FEEDBACK_TYPES.filter((type) => selected.has(type));
    const trimmed = comment.trim();
    onSubmit(reasons, trimmed ? trimmed : null);
    onClose();
  };

  return (
    <>
      {/* 백드롭 */}
      <div
        className="fixed inset-0 z-[9990] bg-black/20 backdrop-blur-[2px]"
        onClick={onClose}
      />
      {/* 모달 */}
      <div
        className="fixed z-[9991] left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-2xl flex flex-col gap-4 p-5 sm:p-6 w-[min(92vw,380px)]"
        style={{
          background: "rgba(255,255,255,0.97)",
          backdropFilter: "blur(24px)",
          WebkitBackdropFilter: "blur(24px)",
          border: "1px solid rgba(200,210,230,0.7)",
          boxShadow: "0 16px 48px rgba(0,0,0,0.16), 0 2px 8px rgba(0,0,0,0.07)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* 제목 */}
        <p className="text-sm sm:text-base font-bold" style={{ color: "var(--clr-navy)" }}>
          답변이 도움이 되지 않으셨나요?
        </p>

        {/* 피드백 유형 */}
        <div className="flex flex-col gap-2">
          {FEEDBACK_TYPES.map((type) => {
            const active = selected.has(type);
            return (
              <button
                key={type}
                onClick={() => toggle(type)}
                className="flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-xs sm:text-[13px] text-left transition-all"
                style={{
                  background: active ? "rgba(37,52,139,0.09)" : "rgba(0,0,0,0.03)",
                  border: active ? "1.5px solid rgba(37,52,139,0.35)" : "1.5px solid rgba(0,0,0,0.07)",
                  color: active ? "var(--clr-navy)" : "var(--clr-text)",
                  fontWeight: active ? 600 : 400,
                }}
              >
                <span
                  className="flex-shrink-0 w-4 h-4 rounded flex items-center justify-center"
                  style={{
                    background: active ? "var(--clr-navy)" : "rgba(0,0,0,0.08)",
                    border: active ? "none" : "1px solid rgba(0,0,0,0.12)",
                  }}
                >
                  {active && (
                    <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round">
                      <polyline points="1.5,5 4,7.5 8.5,2.5" />
                    </svg>
                  )}
                </span>
                {type}
              </button>
            );
          })}
        </div>

        {/* 추가 의견 */}
        <textarea
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="추가 의견을 입력해 주세요 (선택)"
          maxLength={FEEDBACK_COMMENT_MAX}
          rows={3}
          className="w-full resize-none rounded-xl px-3 py-2.5 text-xs sm:text-[13px] outline-none placeholder:text-gray-400 leading-relaxed"
          style={{
            background: "rgba(0,0,0,0.03)",
            border: "1.5px solid rgba(0,0,0,0.08)",
            color: "var(--clr-text)",
          }}
          onFocus={(e) => { e.currentTarget.style.borderColor = "rgba(37,52,139,0.35)"; }}
          onBlur={(e) => { e.currentTarget.style.borderColor = "rgba(0,0,0,0.08)"; }}
        />

        {/* 버튼 */}
        <div className="flex gap-2">
          <button
            onClick={onClose}
            className="flex-1 py-2 rounded-xl text-xs sm:text-[13px] font-medium transition-opacity hover:opacity-75"
            style={{
              border: "1.5px solid rgba(0,0,0,0.12)",
              color: "var(--clr-text-muted)",
              background: "transparent",
            }}
          >
            취소
          </button>
          <button
            onClick={handleSubmit}
            className="flex-1 py-2 rounded-xl text-xs sm:text-[13px] font-semibold text-white transition-opacity hover:opacity-85"
            style={{ background: "var(--clr-navy)" }}
          >
            제출
          </button>
        </div>
      </div>
    </>
  );
}

const SOURCE_CARD_NAVY = "#25348B";

/** ①②… for source ids 1–20; fallback to plain number string */
function circledSourceIndex(id: number): string {
  if (id >= 1 && id <= 20) return String.fromCodePoint(0x2460 + (id - 1));
  return String(id);
}

function chipMetaFromSource(source: Source): string {
  return source.chipMeta ?? sourceKindLabel(source.url);
}

// ── 출처 카드 컴포넌트 ────────────────────────────────────────────────────────
function SourceCard({ source }: { source: Source }) {
  const circled = circledSourceIndex(source.id);
  const chip1 = chipMetaFromSource(source);
  const hasAttachments = source.attachments && source.attachments.length > 0;

  const chipClass =
    "inline-flex items-center max-w-full rounded-full px-2.5 py-1 text-[10px] sm:text-[11px] font-medium leading-tight truncate";
  const chipStyle: CSSProperties = {
    background: "rgba(0,0,0,0.045)",
    border: "1px solid rgba(0,0,0,0.08)",
    color: "var(--clr-text-muted)",
  };
  const formatChipAccentStyle: CSSProperties = {
    ...chipStyle,
    color: SOURCE_CARD_NAVY,
    background: "rgba(37,52,139,0.06)",
    borderColor: "rgba(37,52,139,0.14)",
    textDecoration: "none",
  };

  return (
    <div
      className="rounded-xl overflow-hidden bg-white/95"
      style={{
        border: "1px solid rgba(37,52,139,0.12)",
        boxShadow: "0 2px 14px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04)",
      }}
    >
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2 px-3.5 pt-3.5 pb-2 sm:px-4 sm:pt-4">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="shrink-0 text-[15px] font-semibold tabular-nums leading-none" style={{ color: SOURCE_CARD_NAVY }}>
            {circled}
          </span>
          <span className={chipClass} style={chipStyle} title={chip1}>
            {chip1}
          </span>
          {!hasAttachments && (
            <span className={chipClass} style={formatChipAccentStyle}>
              WEB
            </span>
          )}
        </div>
        <a
          href={source.url}
          target="_blank"
          rel="noopener noreferrer"
          className="shrink-0 text-[11px] sm:text-xs font-semibold transition-opacity hover:opacity-75"
          style={{ color: SOURCE_CARD_NAVY }}
        >
          원문 보기 ↗
        </a>
      </div>

      {/* Body — 제목 + 첨부파일만 (원문 발췌 박스는 길어서 제거, 원문은 "원문 보기"로 확인).
          요소별 margin 대신 gap을 써서 마지막 요소 뒤에 여백이 남지 않게 하고,
          하단 padding은 헤더 상단 padding(pt-3.5 / sm:pt-4)과 맞춘다. */}
      <div className="flex flex-col gap-2.5 px-3.5 pb-3.5 sm:px-4 sm:pb-4">
        <h3 className="text-sm font-bold leading-snug sm:text-[15px]" style={{ color: "var(--clr-text)" }}>
          {source.title}
        </h3>
        {hasAttachments && (
          <div className="flex flex-col items-start gap-1.5">
            {source.attachments.map((attachment) => (
              <a
                key={attachment.url}
                href={attachment.url}
                download
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex max-w-full items-center gap-1.5 text-[11px] font-semibold underline decoration-1 underline-offset-2 transition-opacity hover:opacity-70 sm:text-xs"
                style={{ color: SOURCE_CARD_NAVY }}
                title={`${attachment.name} 다운로드`}
              >
                <span className="shrink-0"><IconDownload /></span>
                <span className="truncate">{attachment.name}</span>
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// 답변 마크다운 → 기존 답변 카드 스타일에 맞춘 요소
// (react-markdown은 원시 HTML을 렌더링하지 않고 javascript: 같은 위험한 링크를 걸러낸다)
const answerMarkdownComponents: Components = {
  h1: ({ children }) => <h4 className="mt-1 font-bold leading-relaxed first:mt-0">{children}</h4>,
  h2: ({ children }) => <h4 className="mt-1 font-bold leading-relaxed first:mt-0">{children}</h4>,
  h3: ({ children }) => <h4 className="mt-1 font-bold leading-relaxed first:mt-0">{children}</h4>,
  p: ({ children }) => <p className="break-words">{children}</p>,
  strong: ({ children }) => <strong className="font-bold">{children}</strong>,
  // 위험한 링크(javascript: 등)는 react-markdown이 href를 비워 주므로 일반 텍스트로 표시
  a: ({ href, children }) => !href ? <span>{children}</span> : (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="break-all font-semibold underline decoration-1 underline-offset-2 hover:opacity-70"
      style={{ color: "var(--clr-navy)" }}
    >
      {children}
    </a>
  ),
  ul: ({ children }) => <ul className="flex list-disc flex-col gap-1 pl-5 marker:font-bold">{children}</ul>,
  ol: ({ children }) => (
    <ol className="flex list-decimal flex-col gap-1 pl-5 marker:font-semibold marker:tabular-nums">{children}</ol>
  ),
  li: ({ children }) => <li className="break-words pl-0.5">{children}</li>,
  code: ({ children }) => (
    <code className="rounded px-1 py-0.5 text-[0.9em]" style={{ background: "rgba(37,52,139,0.08)" }}>
      {children}
    </code>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border px-2 py-1 font-semibold" style={{ borderColor: "rgba(37,52,139,0.15)" }}>
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border px-2 py-1" style={{ borderColor: "rgba(37,52,139,0.15)" }}>
      {children}
    </td>
  ),
};

const LIST_ITEM_RE = /^\s*(?:[-*+•]|\d+[.)])\s+/;

// GPT 답변을 마크다운 규칙에 맞게 다듬는다.
// - "• 항목" 은 마크다운 목록이 아니므로 "- 항목" 으로 바꾼다.
// - 목록 바로 다음에 빈 줄 없이 이어지는 일반 문장은 마크다운에서 마지막 항목에
//   붙어버리므로(lazy continuation) 사이에 빈 줄을 넣어 별도 문단으로 만든다.
function normalizeAnswerMarkdown(content: string): string {
  const lines = content.replace(/\r\n?/g, "\n").split("\n").map((line) => line.replace(/^(\s*)•\s+/, "$1- "));
  const output: string[] = [];
  lines.forEach((line, index) => {
    const previous = index > 0 ? lines[index - 1] : "";
    const continuesList = LIST_ITEM_RE.test(previous) && line.trim() !== "" && !LIST_ITEM_RE.test(line) && !/^\s/.test(line);
    if (continuesList) output.push("");
    output.push(line);
  });
  return output.join("\n");
}

// 완성된 JSON 답변 표시 (마크다운: 굵게·링크·목록·표, 줄바꿈 유지)
function AnswerContent({ content }: { content: string }) {
  return (
    <div
      className="flex flex-col gap-2.5 text-xs leading-7 sm:text-sm sm:leading-7"
      style={{ color: "var(--clr-text)" }}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={answerMarkdownComponents}>
        {normalizeAnswerMarkdown(content)}
      </ReactMarkdown>
    </div>
  );
}

function AssistantMessage({
  msg,
  onFeedbackChange,
}: {
  msg: Message;
  /** 피드백 저장/취소(null). 상태는 부모의 messages 에 있어 대화를 다시 열어도 유지된다. */
  onFeedbackChange: (next: MessageFeedback | null) => void;
}) {
  const [activeSourceId, setActiveSourceId] = useState<number | null>(null);
  const [copied, setCopied] = useState(false);
  const [dislikeOpen, setDislikeOpen] = useState(false);
  const rating = msg.feedback?.rating ?? null;
  // 저장이 끝난 답변(DB id 있음)에만 피드백을 남길 수 있다.
  const canFeedback = Boolean(msg.dbId);
  const feedbackDisabledTitle = "답변 저장이 끝난 뒤 피드백을 남길 수 있어요";

  // 좋아요 ↔ 싫어요는 배타적: 좋아요를 누르면 싫어요(사유 포함)가 좋아요로 바뀌고, 같은 버튼을 다시 누르면 취소.
  const handleLike = () => {
    if (!canFeedback) return;
    onFeedbackChange(rating === "up" ? null : { rating: "up", reasons: [], comment: null });
  };
  const handleDislike = () => {
    if (!canFeedback) return;
    if (rating === "down") onFeedbackChange(null);
    else setDislikeOpen(true);
  };
  // 출처는 답변이 인용한 것만, 같은 공지는 하나로 보여 준다(답변 속 [n]도 같이 맞춘다).
  const { content: shownContent, sources: shownSources } = useMemo(
    () => prepareSources(msg.content, msg.sources ?? []),
    [msg.content, msg.sources],
  );
  const hasSources = shownSources.length > 0;

  const handleCopy = () => {
    navigator.clipboard.writeText(shownContent).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const toggleSourceId = (id: number) => {
    setActiveSourceId((currentId) => (currentId === id ? null : id));
  };

  const visibleSources =
    hasSources && activeSourceId !== null
      ? shownSources.filter((source) => source.id === activeSourceId)
      : [];

  const navyToggle = "#25348B";

  return (
    <div className="flex justify-start w-full">
      <div className="flex flex-col items-start max-w-[88%] sm:max-w-lg w-full">

        {/* 답변 카드 */}
        <div className="glass-card rounded-2xl p-4 sm:p-5 shadow-sm flex flex-col gap-3 sm:gap-4 w-full">
          <AnswerContent content={shownContent} />

          {/* 메시지 레벨 첨부파일 */}
          {msg.attachments && msg.attachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 sm:gap-2">
              {msg.attachments.map((att) => (
                <a
                  key={att.name}
                  href={att.url}
                  className="flex items-center gap-1.5 px-2.5 sm:px-3 py-1 sm:py-1.5 rounded-full text-[11px] sm:text-xs font-medium transition-colors hover:bg-white/80"
                  style={{ background: "rgba(255,255,255,0.55)", border: "1px solid rgba(255,255,255,0.85)", color: "var(--clr-navy)" }}
                >
                  {att.name}
                  <IconDownload />
                </a>
              ))}
            </div>
          )}

          {/* 하단 바: 좌측 액션 · 우측 출처 토글 */}
          <div
            className="flex items-center justify-between gap-2 flex-wrap -mt-1 pt-0"
          >
            <div className="flex items-center gap-1">
              <button
                onClick={handleCopy}
                title="답변 복사"
                className="flex items-center justify-center w-7 h-7 rounded-lg transition-all hover:bg-white/60"
                style={{
                  color: copied ? "var(--clr-navy)" : "var(--clr-text-muted)",
                  background: copied ? "rgba(37,52,139,0.08)" : "transparent",
                }}
              >
                {copied ? <IconCheckSm /> : <IconCopy />}
              </button>
              <button
                type="button"
                onClick={handleLike}
                disabled={!canFeedback}
                aria-pressed={rating === "up"}
                title={canFeedback ? (rating === "up" ? "좋아요 취소" : "도움이 됐어요") : feedbackDisabledTitle}
                className="flex items-center justify-center w-7 h-7 rounded-lg transition-all hover:bg-white/60 disabled:cursor-not-allowed disabled:opacity-40"
                style={{
                  color: rating === "up" ? "var(--clr-navy)" : "var(--clr-text-muted)",
                  background: rating === "up" ? "rgba(37,52,139,0.08)" : "transparent",
                }}
              >
                <IconThumbUp />
              </button>
              <button
                type="button"
                onClick={handleDislike}
                disabled={!canFeedback}
                aria-pressed={rating === "down"}
                title={canFeedback ? (rating === "down" ? "싫어요 취소" : "도움이 안 됐어요") : feedbackDisabledTitle}
                className="flex items-center justify-center w-7 h-7 rounded-lg transition-all hover:bg-white/60 disabled:cursor-not-allowed disabled:opacity-40"
                style={{
                  color: rating === "down" ? "#c53030" : "var(--clr-text-muted)",
                  background: rating === "down" ? "rgba(197,48,48,0.08)" : "transparent",
                }}
              >
                <IconThumbDown />
              </button>
            </div>

            {hasSources && (
              /* 출처가 많으면(①~⑩) 모바일에서 한 줄로 안 들어가 오른쪽이 잘리던 문제:
                 flex-shrink-0 + 줄바꿈 없음 → 묶음 안에서 줄바꿈되도록 flex-wrap, 오른쪽 정렬 유지 */
              <div className="ml-auto flex min-w-0 max-w-full flex-wrap items-center justify-end gap-1.5">
                {shownSources.map((src) => {
                  const active = activeSourceId === src.id;
                  return (
                    <button
                      key={src.id}
                      type="button"
                      onClick={() => toggleSourceId(src.id)}
                      aria-pressed={active}
                      aria-label={active ? `출처 ${src.id} 숨기기` : `출처 ${src.id} 보기`}
                      title={`출처 ${circledSourceIndex(src.id)}`}
                      className="flex items-center justify-center min-w-[1.75rem] h-7 px-1.5 rounded-lg text-[13px] font-semibold leading-none transition-colors"
                      style={{
                        background: active ? navyToggle : "rgba(0,0,0,0.04)",
                        color: active ? "#fff" : "var(--clr-text-muted)",
                        border: active ? `1px solid ${navyToggle}` : "1px solid rgba(0,0,0,0.1)",
                        boxShadow: active ? undefined : "inset 0 1px 0 rgba(255,255,255,0.65)",
                      }}
                    >
                      {circledSourceIndex(src.id)}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          {/* 인라인 출처 — 토글 시에만, 상단과 구분선으로 분리 */}
          {visibleSources.length > 0 && (
            <div className="flex flex-col gap-2 sm:gap-2.5 pt-1">
              {visibleSources.map((src) => (
                <SourceCard key={src.id} source={src} />
              ))}
            </div>
          )}
        </div>

        {/* 싫어요 피드백 모달 */}
        {dislikeOpen && (
          <DislikeFeedbackModal
            onClose={() => setDislikeOpen(false)}
            onSubmit={(reasons, comment) => onFeedbackChange({ rating: "down", reasons, comment })}
            initial={msg.feedback}
          />
        )}

      </div>
    </div>
  );
}

// ── 메인 컴포넌트 ─────────────────────────────────────────────────────────────
export default function ChatContent() {
  const router = useRouter();
  const { takePendingQuery } = useQueryContext();
  const { user, loading: authLoading, signOut } = useAuth();

  // 로그인 필수: 세션 없으면 로그인 화면으로 (비회원 채팅 더 이상 허용 안 함)
  useEffect(() => {
    if (!authLoading && !user) {
      router.replace("/auth?next=/chat");
    }
  }, [authLoading, user, router]);

  const [history, setHistory] = useState<HistoryItem[]>(sidebarHistory);
  const [activeId, setActiveId] = useState<string>(() => sidebarHistory[0]?.id ?? "");
  const persistChatIdRef = useRef<string | null>(null);
  /** 두 번째 메시지가 첫 createChat 완료 전에 시작될 때 동일 채팅 행을 재사용 */
  const pendingChatCreateRef = useRef<Promise<string> | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [chatState, setChatState] = useState<ChatState>("idle");
  // chatState가 "error"일 때 보여 줄 문구. null이면 일반 실패 안내.
  const [chatError, setChatError] = useState<string | null>(null);

  // ── 사이드바 상태 (SSR-safe: false로 시작, mount 후 보정) ──
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // isMobile: < 768px → 사이드바를 fixed 오버레이로 표시
  const [isMobile, setIsMobile] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(SIDEBAR_DEFAULT_WIDTH);
  const [isSidebarResizing, setIsSidebarResizing] = useState(false);

  const [contextMenuId, setContextMenuId] = useState<string | null>(null);
  const [contextMenuPos, setContextMenuPos] = useState<{ top: number; left: number } | null>(null);
  const [chatTitle, setChatTitle] = useState("새 채팅");
  const [renameId, setRenameId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [userMenuPos, setUserMenuPos] = useState<{ bottom: number; left: number } | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = (msg: string) => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setToast(msg);
    toastTimerRef.current = setTimeout(() => setToast(null), 3000);
  };

  // ── 답변 피드백 저장 ──────────────────────────────────────────
  // 화면을 먼저 바꾸고(낙관적 업데이트) 저장이 실패하면 이전 상태로 되돌린다.
  // 같은 답변에 요청이 겹치면 응답 순서가 뒤바뀌어 화면과 DB가 어긋날 수 있어,
  // 저장 중인 답변의 추가 클릭은 무시한다.
  const feedbackInFlightRef = useRef<Set<string>>(new Set());
  const handleFeedbackChange = async (localId: string, next: MessageFeedback | null) => {
    const target = messages.find((m) => m.id === localId);
    if (!target?.dbId || feedbackInFlightRef.current.has(localId)) return;
    const previous = target.feedback ?? null;
    const setFeedback = (feedback: MessageFeedback | null) =>
      setMessages((prev) => prev.map((m) => (m.id === localId ? { ...m, feedback } : m)));

    feedbackInFlightRef.current.add(localId);
    setFeedback(next);
    try {
      if (next) await saveFeedback(target.dbId, next);
      else await deleteFeedback(target.dbId);
      if (next?.rating === "down") showToast("피드백 감사합니다");
    } catch (err) {
      console.error("[chat] save feedback failed:", err);
      setFeedback(previous);
      showToast("피드백 저장에 실패했습니다.");
    } finally {
      feedbackInFlightRef.current.delete(localId);
    }
  };

  const closeContextMenu = () => {
    setContextMenuId(null);
    setContextMenuPos(null);
  };

  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const contextMenuRef = useRef<HTMLDivElement>(null);
  const userMenuRef = useRef<HTMLDivElement>(null);
  // 지금 보고 있는 화면의 번호. 새 채팅·기록 불러오기마다 1씩 올라간다.
  // 답변을 기다리는 중에 다른 채팅으로 가도 요청은 취소하지 않고, 도착한 답변은
  // 원래 채팅에 저장한다. 화면에 붙이는 건 그 채팅을 보고 있을 때뿐이다.
  const viewRef = useRef(0);
  // 저장된 대화 불러오기 번호 (showStoredMessages 참고).
  const loadSeqRef = useRef(0);
  // 답변을 기다리는 채팅 id. 그 채팅으로 돌아오면 로딩 표시를 다시 보여준다.
  const pendingChatsRef = useRef<Set<string>>(new Set());
  const [pendingChats, setPendingChats] = useState<Set<string>>(() => new Set());
  const setChatPending = (chatId: string, pending: boolean) => {
    if (pending) pendingChatsRef.current.add(chatId);
    else pendingChatsRef.current.delete(chatId);
    setPendingChats(new Set(pendingChatsRef.current));
  };

  // ── 반응형: mount 시 viewport 크기 감지 ────────────────────────
  useEffect(() => {
    const update = () => {
      const mobile = window.innerWidth < 768;
      setIsMobile(mobile);
      // 768 미만: 항상 닫힘 / 1024 이상: 기본 열림
      if (mobile) {
        setSidebarOpen(false);
      } else {
        // 처음 열릴 때만 desktop 기본값 적용
        setSidebarOpen(window.innerWidth >= 1024);
      }
    };
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  // ── 로그인 시 사이드바 목록을 DB에서, 비로그인 시 JSON sidebar ─────
  useEffect(() => {
    if (authLoading) return;

    if (!user) {
      setHistory(sidebarHistory);
      setActiveId(sidebarHistory[0]?.id ?? "");
      persistChatIdRef.current = null;
      pendingChatCreateRef.current = null;
      return;
    }

    let cancelled = false;
    void (async () => {
      try {
        const rows = await listChats();
        if (cancelled) return;
        setHistory(rows.map((r) => ({ id: r.id, title: r.title })));
        setActiveId("");
        persistChatIdRef.current = null;
        pendingChatCreateRef.current = null;
      } catch (err) {
        console.error("[chat] listChats failed:", err);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [user, authLoading]);

  // ── Context 질문 소비: 인증 준비 후.
  // takePendingQuery()는 읽음과 동시에 지우는 원자적 연산이라(QueryContext 참고),
  // 이 effect가 React StrictMode로 두 번 호출되거나 /chat이 두 번 마운트되는
  // 레이스가 있어도 실제로 값을 가져가는 쪽은 항상 한 번뿐이라 cleanup이 필요 없다.
  useEffect(() => {
    if (authLoading) return;
    const q = takePendingQuery().trim();
    if (!q) return;
    startConversation(q);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authLoading, takePendingQuery]);

  useEffect(() => {
    if (!isSidebarResizing) return;

    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    return () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
    };
  }, [isSidebarResizing]);

  // ── 외부 클릭 / ESC → 컨텍스트 메뉴 + 사용자 메뉴 닫기 ───────
  useEffect(() => {
    const onMouse = (e: MouseEvent) => {
      if (contextMenuRef.current && !contextMenuRef.current.contains(e.target as Node)) {
        setContextMenuId(null);
        setContextMenuPos(null);
      }
      if (userMenuRef.current && !userMenuRef.current.contains(e.target as Node)) {
        setUserMenuOpen(false);
        setUserMenuPos(null);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setContextMenuId(null);
        setContextMenuPos(null);
        setUserMenuOpen(false);
        setUserMenuPos(null);
      }
    };
    document.addEventListener("mousedown", onMouse);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onMouse);
      document.removeEventListener("keydown", onKey);
    };
  }, []);

  // ── 자동 스크롤 ───────────────────────────────────────────────
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, chatState]);

  // ── AI 응답 요청 (공통) ───────────────────────────────────────
  // history: 이 질문 앞까지의 같은 채팅 대화(오래된 것부터). 이어지는 질문 이해용.
  const requestAIReply = async (question: string, history: ChatTurn[] = []) => {
    const view = viewRef.current;
    // 질문한 화면 그대로인지, 아니면 다른 채팅에 갔다가 이 채팅으로 돌아왔는지.
    const sameView = () => viewRef.current === view;
    const returnedTo = (chatId: string | null) =>
      !sameView() && chatId !== null && persistChatIdRef.current === chatId;

    setChatState("loading");

    let persistedChatId: string | null = user?.id ? persistChatIdRef.current : null;

    if (user?.id) {
      try {
        if (!persistedChatId) {
          if (!pendingChatCreateRef.current) {
            const title = chatTitleFromQuestion(question);
            pendingChatCreateRef.current = createChat(title, user.id)
              .then((row) => {
                // 만드는 사이에 다른 채팅으로 갔다면 화면을 이 채팅으로 되돌리지 않는다.
                if (viewRef.current === view) {
                  persistChatIdRef.current = row.id;
                  setActiveId(row.id);
                  setChatTitle(title);
                }
                return row.id;
              })
              .finally(() => {
                pendingChatCreateRef.current = null;
              });
          }
          persistedChatId = await pendingChatCreateRef.current;
        }
        setChatPending(persistedChatId, true);
        await insertChatMessage(persistedChatId, "user", question, null);
        const refreshed = await listChats();
        setHistory(refreshed.map((r) => ({ id: r.id, title: r.title })));
      } catch (err) {
        console.error("[chat] persist user message failed:", err);
        showToast("대화 저장에 실패했습니다.");
      }
    }

    try {
      const { answer, sources } = await askBackend(question, history);
      const finalContent = answer || "관련 정보를 찾을 수 없습니다.";
      const assistantLocalId = uid();
      // 돌아온 채팅은 기록을 불러오는 중일 수 있어 화면에 직접 붙이지 않고,
      // 아래에서 저장이 끝난 뒤 다시 불러온다(붙인 답변을 늦게 온 기록이 덮어쓰지 않게).
      const viewing = sameView();
      if (viewing) {
        setMessages((prev) => [
          ...prev,
          { id: assistantLocalId, role: "assistant", content: finalContent, attachments: [], sources },
        ]);
        setChatState("success");
      }

      if (user?.id && persistedChatId) {
        try {
          const assistantDbId = await insertChatMessage(persistedChatId, "assistant", finalContent, sources ?? null);
          // 저장이 끝나야 피드백 버튼이 활성화된다(피드백은 DB 메시지 id 에 연결).
          setMessages((prev) =>
            prev.map((m) => (m.id === assistantLocalId ? { ...m, dbId: assistantDbId } : m)),
          );
          const refreshed = await listChats();
          setHistory(refreshed.map((r) => ({ id: r.id, title: r.title })));
        } catch (err) {
          console.error("[chat] persist assistant message failed:", err);
          showToast("답변 저장에 실패했습니다.");
        }
      }
      if (!viewing && persistedChatId) {
        if (returnedTo(persistedChatId)) {
          setChatPending(persistedChatId, false);
          const current = viewRef.current;
          try {
            await showStoredMessages(persistedChatId, current);
          } catch (err) {
            console.error("[chat] fetchChatMessages failed:", err);
          }
          if (viewRef.current === current) setChatState("success");
        } else {
          showToast("이전 채팅의 답변이 도착했어요");
        }
      }
    } catch (err) {
      console.error("[chat] askBackend failed:", err);
      if (sameView() || returnedTo(persistedChatId)) {
        // 횟수 제한(429)만 서버 안내 문구를 보여 준다. 나머지는 내부 오류 문구라 일반 안내로.
        setChatError(err instanceof AskError && err.status === 429 ? err.message : null);
        setChatState("error");
      }
    } finally {
      if (persistedChatId) setChatPending(persistedChatId, false);
    }
  };

  // ── 대화 시작 ─────────────────────────────────────────────────
  const startConversation = (q: string) => {
    setMessages([{ id: uid(), role: "user", content: q }]);
    void requestAIReply(q);
  };

  // ── SEND_MESSAGE ──────────────────────────────────────────────
  const handleSend = () => {
    const q = inputValue.trim();
    if (!q || chatState === "loading") return;
    setInputValue("");
    const history = messages.map(({ role, content }) => ({ role, content }));
    setMessages((prev) => [...prev, { id: uid(), role: "user", content: q }]);
    void requestAIReply(q, history);
  };

  // ── NEW_CHAT ──────────────────────────────────────────────────
  const handleNewChat = () => {
    // 답변을 기다리던 요청은 계속 진행된다(requestAIReply가 원래 채팅에 저장).
    viewRef.current += 1;
    setMessages([]);
    setChatState("idle");
    setInputValue("");
    persistChatIdRef.current = null;
    pendingChatCreateRef.current = null;
    setActiveId(user ? "" : sidebarHistory[0]?.id ?? "");
    setChatTitle("새 채팅");
    closeContextMenu();
    if (isMobile) setSidebarOpen(false);
    router.replace("/chat");
  };

  // ── LOAD_HISTORY ──────────────────────────────────────────────
  const handleLoadHistory = async (item: HistoryItem) => {
    viewRef.current += 1;
    const view = viewRef.current;
    pendingChatCreateRef.current = null;
    setActiveId(item.id);
    setChatTitle(item.title);
    closeContextMenu();
    if (isMobile) setSidebarOpen(false);

    if (user && isPersistedChatId(item.id)) {
      setMessages([]);
      setChatState(pendingChatsRef.current.has(item.id) ? "loading" : "idle");
      persistChatIdRef.current = item.id;
      try {
        await showStoredMessages(item.id, view);
      } catch (err) {
        console.error("[chat] fetchChatMessages failed:", err);
        showToast("대화를 불러오지 못했습니다.");
      }
      return;
    }
  };

  // 저장된 대화를 불러와 화면에 띄운다. 불러오기가 겹치면(기록을 연달아 클릭,
  // 또는 돌아온 채팅의 답변이 저장된 직후 다시 불러오기) 마지막에 시작한 것만
  // 반영한다 — 먼저 시작한 조회가 늦게 도착해 새 답변을 덮어쓰지 않도록.
  const showStoredMessages = async (chatId: string, view: number) => {
    const seq = ++loadSeqRef.current;
    const rows = await fetchChatMessages(chatId);
    // 내가 남긴 피드백을 함께 불러와 버튼 상태를 복원한다. 실패해도 대화는 보여준다.
    let feedbackByMessage: Record<string, MessageFeedback> = {};
    try {
      feedbackByMessage = await fetchFeedbackForMessages(
        rows.filter((r) => r.role === "assistant").map((r) => r.id),
      );
    } catch (err) {
      console.error("[chat] fetchFeedbackForMessages failed:", err);
    }
    if (seq !== loadSeqRef.current || viewRef.current !== view) return;
    setMessages(
      rows.map((r) => ({
        id: uid(),
        dbId: r.id,
        role: r.role,
        content: r.content,
        attachments: [],
        sources: (r.role === "assistant" ? parseStoredSources(r.sources) : undefined) as Source[] | undefined,
        feedback: feedbackByMessage[r.id] ?? null,
      })),
    );
  };

  // ── DELETE ────────────────────────────────────────────────────
  const handleDelete = (id: string) => {
    closeContextMenu();
    void (async () => {
      if (user && isPersistedChatId(id)) {
        try {
          await deleteChat(id);
          const rows = await listChats();
          setHistory(rows.map((r) => ({ id: r.id, title: r.title })));
        } catch (err) {
          console.error("[chat] deleteChat failed:", err);
          showToast("삭제에 실패했습니다.");
          return;
        }
      } else {
        setHistory((prev) => prev.filter((h) => h.id !== id));
      }
      if (activeId === id) handleNewChat();
    })();
  };

  // ── RENAME ────────────────────────────────────────────────────
  const handleRenameStart = (item: HistoryItem) => {
    setRenameId(item.id);
    setRenameValue(item.title);
    closeContextMenu();
  };
  const handleRenameConfirm = (id: string) => {
    const title = renameValue.trim();
    if (!title) {
      setRenameId(null);
      return;
    }
    if (user && isPersistedChatId(id)) {
      void (async () => {
        try {
          await updateChatTitle(id, title);
          const rows = await listChats();
          setHistory(rows.map((r) => ({ id: r.id, title: r.title })));
          if (activeId === id) setChatTitle(title);
        } catch (err) {
          console.error("[chat] updateChatTitle failed:", err);
          showToast("제목 변경에 실패했습니다.");
        } finally {
          setRenameId(null);
        }
      })();
      return;
    }
    setHistory((prev) => prev.map((h) => (h.id === id ? { ...h, title } : h)));
    if (activeId === id) setChatTitle(title);
    setRenameId(null);
  };

  // ── 사이드바 너비 ─────────────────────────────────────────────
  const renderedSidebarWidth = sidebarOpen
    ? isMobile
      ? "min(280px, 80vw)"
      : `${sidebarWidth}px`
    : "0px";

  const resizeSidebar = (clientX: number) => {
    const viewportMax = Math.floor(window.innerWidth * 0.45);
    const maxWidth = Math.max(
      SIDEBAR_MIN_WIDTH,
      Math.min(SIDEBAR_MAX_WIDTH, viewportMax),
    );
    setSidebarWidth(Math.min(maxWidth, Math.max(SIDEBAR_MIN_WIDTH, clientX)));
  };

  const handleSidebarResizeKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const direction = e.key === "ArrowRight" ? 1 : -1;
    const step = e.shiftKey ? 25 : 10;
    setSidebarWidth((width) =>
      Math.min(
        SIDEBAR_MAX_WIDTH,
        Math.max(SIDEBAR_MIN_WIDTH, width + direction * step),
      ),
    );
  };

  // ── 사용자 메뉴 토글 ──────────────────────────────────────────
  const handleUserMenuToggle = (e: React.MouseEvent<HTMLButtonElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    // 팝업이 오른쪽 화면 밖으로 나가지 않도록 left 클램핑
    const left = Math.min(rect.left, window.innerWidth - 204);
    setUserMenuOpen((prev) => {
      if (prev) {
        setUserMenuPos(null);
        return false;
      }
      setUserMenuPos({ bottom: window.innerHeight - rect.top + 6, left });
      return true;
    });
  };

  return (
    <div className="bg-app flex h-[100dvh] overflow-hidden relative">

      {/* ── 모바일 백드롭 (사이드바 오버레이 시 배경 어둡게) ── */}
      {isMobile && sidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/20 backdrop-blur-[1px] transition-opacity"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* ── 사이드바 ─────────────────────────────────────────── */}
      <aside
        className={`flex flex-col flex-shrink-0 overflow-hidden ${
          isSidebarResizing ? "" : "transition-all duration-300"
        }`}
        style={{
          // 모바일: fixed overlay / 데스크톱: 일반 flow
          position: isMobile ? "fixed" : "relative",
          top: isMobile ? 0 : undefined,
          left: isMobile ? 0 : undefined,
          bottom: isMobile ? 0 : undefined,
          zIndex: isMobile ? 40 : undefined,
          width: renderedSidebarWidth,
          background: "rgba(255,255,255,0.48)",
          backdropFilter: "blur(18px)",
          WebkitBackdropFilter: "blur(18px)",
          borderRight: "1px solid rgba(255,255,255,0.7)",
        }}
      >
        {sidebarOpen && (
          <div
            className="flex flex-col h-full px-3 py-4 overflow-hidden"
            style={{
              minWidth: isMobile ? "min(280px, 80vw)" : `${sidebarWidth}px`,
            }}
          >
            {/* ── 상단 로고 ── */}
            <div
              className="pb-3 mb-2 flex-shrink-0"
              style={{ borderBottom: "1px solid rgba(255,255,255,0.5)" }}
            >
              {/* 홈으로 (홈에서만 관리자 페이지로 갈 수 있다). ?from=chat 이 있으면
                  홈이 로그인 사용자를 /chat 으로 되돌려 보내지 않는다 (app/page.tsx) */}
              <Link
                href="/?from=chat"
                title="홈으로"
                className="text-xs sm:text-sm font-bold leading-snug block w-fit transition-opacity hover:opacity-70"
                style={{ color: "var(--clr-navy)" }}
              >
                unira | 부경대학교
              </Link>
            </div>

            {/* 새 채팅 */}
            <button
              onClick={handleNewChat}
              className="flex items-center gap-2 px-3 py-2 rounded-xl text-sm font-medium transition-colors hover:bg-white/50"
              style={{ color: "var(--clr-text)" }}
            >
              <IconPlus />
              새 채팅
            </button>

            {/* 검색 */}
            <button
              className="flex items-center gap-2 px-3 py-2 rounded-xl text-sm transition-colors hover:bg-white/50"
              style={{ color: "var(--clr-text-muted)" }}
            >
              <IconSearch />
              검색
            </button>

            {/* 최근 항목 */}
            <p
              className="px-3 pt-3 pb-1 text-xs font-semibold uppercase tracking-wide"
              style={{ color: "var(--clr-text-muted)" }}
            >
              {sidebar.recentLabel}
            </p>

            {/* 히스토리 목록 */}
            <div className="flex flex-col gap-0.5 flex-1 overflow-y-auto">
              {!authLoading && !user ? (
                <p
                  className="px-3 py-2 text-xs"
                  style={{ color: "var(--clr-text-muted)" }}
                >
                  로그인하면 채팅 기록을 저장할 수 있어요
                </p>
              ) : (
                history.map((item) => (
                <div key={item.id} className="relative group">
                  {renameId === item.id ? (
                    <input
                      autoFocus
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onBlur={() => handleRenameConfirm(item.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") handleRenameConfirm(item.id);
                        if (e.key === "Escape") setRenameId(null);
                      }}
                      className="w-full px-3 py-2 text-sm rounded-xl outline-none"
                      style={{
                        background: "rgba(255,255,255,0.7)",
                        color: "var(--clr-text)",
                        border: "1px solid rgba(255,255,255,0.9)",
                      }}
                    />
                  ) : (
                    <button
                      onClick={() => handleLoadHistory(item)}
                      className="w-full flex items-center justify-between px-3 py-2 rounded-xl text-sm text-left transition-colors hover:bg-white/50"
                      style={{
                        background: activeId === item.id ? "rgba(255,255,255,0.6)" : "transparent",
                        color: "var(--clr-text)",
                        fontWeight: activeId === item.id ? 600 : 400,
                      }}
                    >
                      <span className="truncate flex-1">{item.title}</span>
                      {pendingChats.has(item.id) && (
                        <span
                          className="ml-1 w-1.5 h-1.5 rounded-full animate-pulse flex-shrink-0"
                          style={{ background: "var(--clr-navy)" }}
                          aria-label="답변 작성 중"
                          title="답변 작성 중"
                        />
                      )}

                      {/* ··· 버튼 — 메뉴가 열린 항목은 항상 표시 */}
                      <span
                        className={`transition-opacity ml-1 px-1 py-0.5 rounded text-sm leading-none flex-shrink-0 ${
                          contextMenuId === item.id
                            ? "opacity-100 bg-white/40"
                            : "opacity-0 group-hover:opacity-100"
                        }`}
                        style={{ color: "var(--clr-text-muted)" }}
                        onMouseDown={(e) => e.stopPropagation()}
                        onClick={(e) => {
                          e.stopPropagation();
                          if (contextMenuId === item.id) {
                            closeContextMenu();
                            return;
                          }
                          const itemEl = (e.currentTarget as HTMLElement).closest("div.group");
                          const rect = itemEl
                            ? itemEl.getBoundingClientRect()
                            : (e.currentTarget as HTMLElement).getBoundingClientRect();
                          const menuWidth = 148;
                          const left = Math.min(
                            rect.left,
                            window.innerWidth - menuWidth - 8
                          );
                          setContextMenuPos({ top: rect.bottom + 2, left });
                          setContextMenuId(item.id);
                        }}
                      >
                        ···
                      </span>
                    </button>
                  )}
                </div>
                ))
              )}
            </div>

            {/* ── 하단 사용자 영역 ── */}
            <div
              className="flex-shrink-0 pt-2 mt-1 flex flex-col gap-0.5"
              style={{ borderTop: "1px solid rgba(255,255,255,0.5)" }}
            >
              <button
                onMouseDown={(e) => e.stopPropagation()}
                onClick={handleUserMenuToggle}
                className="w-full flex items-center gap-2.5 px-3 py-2 rounded-xl text-sm transition-colors hover:bg-white/50"
                style={{
                  color: "var(--clr-text)",
                  background: userMenuOpen ? "rgba(255,255,255,0.5)" : "transparent",
                }}
              >
                <div
                  className="w-6 h-6 rounded-full flex items-center justify-center flex-shrink-0 text-white"
                  style={{ background: "var(--clr-navy)" }}
                >
                  <IconUser />
                </div>
                <span className="font-medium text-sm truncate min-w-0">
                  {authLoading ? "" : user ? displayNameOf(user) : "Guest"}
                </span>
              </button>
            </div>
          </div>
        )}

        {sidebarOpen && !isMobile && (
          <div
            role="separator"
            aria-label="사이드바 너비 조절"
            aria-orientation="vertical"
            aria-valuemin={SIDEBAR_MIN_WIDTH}
            aria-valuemax={SIDEBAR_MAX_WIDTH}
            aria-valuenow={sidebarWidth}
            tabIndex={0}
            className="absolute inset-y-0 right-0 z-10 w-2 cursor-col-resize touch-none outline-none group"
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture(e.pointerId);
              setIsSidebarResizing(true);
            }}
            onPointerMove={(e) => {
              if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
              resizeSidebar(e.clientX);
            }}
            onPointerUp={(e) => {
              if (e.currentTarget.hasPointerCapture(e.pointerId)) {
                e.currentTarget.releasePointerCapture(e.pointerId);
              }
              setIsSidebarResizing(false);
            }}
            onPointerCancel={() => setIsSidebarResizing(false)}
            onKeyDown={handleSidebarResizeKeyDown}
          >
            <span
              className={`absolute inset-y-0 right-0 w-0.5 transition-colors group-hover:bg-[var(--clr-navy)] group-focus:bg-[var(--clr-navy)] ${
                isSidebarResizing ? "bg-[var(--clr-navy)]" : "bg-transparent"
              }`}
            />
          </div>
        )}
      </aside>

      {/* ── 컨텍스트 메뉴 (fixed — aside overflow:hidden 클리핑 회피) ── */}
      {contextMenuId && contextMenuPos && (() => {
        const menuItem = history.find((h) => h.id === contextMenuId);
        if (!menuItem) return null;
        return (
          <div
            ref={contextMenuRef}
            className="fixed z-[9999] rounded-xl overflow-hidden"
            style={{
              top: contextMenuPos.top,
              left: contextMenuPos.left,
              minWidth: "148px",
              background: "rgba(255,255,255,0.96)",
              backdropFilter: "blur(20px)",
              WebkitBackdropFilter: "blur(20px)",
              border: "1px solid rgba(200,210,230,0.7)",
              boxShadow: "0 8px 28px rgba(0,0,0,0.13), 0 1.5px 6px rgba(0,0,0,0.07)",
            }}
          >
            <button
              onClick={closeContextMenu}
              className="flex items-center gap-2.5 w-full px-4 py-2.5 text-sm transition-colors hover:bg-gray-50"
              style={{ color: "var(--clr-text)" }}
            >
              <IconStar /> 즐겨찾기
            </button>
            <div style={{ height: "1px", background: "rgba(0,0,0,0.06)", margin: "0 12px" }} />
            <button
              onClick={() => handleRenameStart(menuItem)}
              className="flex items-center gap-2.5 w-full px-4 py-2.5 text-sm transition-colors hover:bg-gray-50"
              style={{ color: "var(--clr-text)" }}
            >
              <IconPencil /> 이름 변경
            </button>
            <div style={{ height: "1px", background: "rgba(0,0,0,0.06)", margin: "0 12px" }} />
            <button
              onClick={() => handleDelete(menuItem.id)}
              className="flex items-center gap-2.5 w-full px-4 py-2.5 text-sm transition-colors hover:bg-red-50"
              style={{ color: "#e53e3e" }}
            >
              <IconTrash /> 삭제
            </button>
          </div>
        );
      })()}

      {/* ── 사용자 팝업 메뉴 (fixed — 버튼 위쪽에 표시) ── */}
      {userMenuOpen && userMenuPos && (
        <>
          {/* 투명 백드롭 — 외부 클릭 시 메뉴 닫기 (ref 체크보다 신뢰성 높음) */}
          <div
            className="fixed inset-0 z-[9998]"
            onMouseDown={() => { setUserMenuOpen(false); setUserMenuPos(null); }}
          />
          <div
            ref={userMenuRef}
            className="fixed z-[9999] rounded-xl overflow-hidden"
            style={{
              bottom: userMenuPos.bottom,
              left: userMenuPos.left,
              minWidth: "180px",
              maxWidth: "min(220px, calc(100vw - 16px))",
              background: "rgba(255,255,255,0.97)",
              backdropFilter: "blur(20px)",
              WebkitBackdropFilter: "blur(20px)",
              border: "1px solid rgba(200,210,230,0.7)",
              boxShadow: "0 -4px 24px rgba(0,0,0,0.10), 0 8px 28px rgba(0,0,0,0.10)",
            }}
          >
            {/* 계정 헤더 — 로그인 시엔 이름/이메일, 비로그인 시엔 라벨만 */}
            <div
              className="px-4 py-2.5 flex flex-col gap-0.5"
              style={{
                borderBottom: "1px solid rgba(0,0,0,0.06)",
              }}
            >
              <span
                className="text-[10px] font-semibold uppercase tracking-wider"
                style={{ color: "var(--clr-text-muted)" }}
              >
                계정
              </span>
              {user ? (
                <>
                  <span
                    className="text-xs font-semibold truncate"
                    style={{ color: "var(--clr-navy)" }}
                  >
                    {displayNameOf(user)}
                  </span>
                  {user.email && user.email !== displayNameOf(user) && (
                    <span
                      className="text-[11px] truncate"
                      style={{ color: "var(--clr-text-muted)" }}
                    >
                      {user.email}
                    </span>
                  )}
                </>
              ) : (
                <span className="text-[11px]" style={{ color: "var(--clr-text-muted)" }}>
                  로그인하면 대화 기록이 저장됩니다
                </span>
              )}
            </div>

            {/* 액션 영역 — 로그인/비로그인 분기 */}
            <div className="p-2 flex flex-col gap-1">
              {user ? (
                <>
                  <Link
                    href="/auth/change-password"
                    prefetch={false}
                    onClick={() => {
                      setUserMenuOpen(false);
                      setUserMenuPos(null);
                    }}
                    className="w-full px-3 py-2 rounded-lg text-xs font-semibold text-center transition-colors hover:bg-black/5"
                    style={{ color: "var(--clr-navy)", textDecoration: "none" }}
                  >
                    비밀번호 변경
                  </Link>
                  <button
                    type="button"
                    onClick={async () => {
                      setUserMenuOpen(false);
                      setUserMenuPos(null);
                      await signOut();
                      router.refresh();
                    }}
                    className="w-full px-3 py-2 rounded-lg text-xs font-semibold transition-colors hover:bg-red-50"
                    style={{ color: "#c53030", background: "transparent" }}
                  >
                    로그아웃
                  </button>
                </>
              ) : (
                <>
                  <Link
                    href="/auth?mode=signup"
                    prefetch={false}
                    onClick={() => {
                      setUserMenuOpen(false);
                      setUserMenuPos(null);
                    }}
                    className="w-full px-3 py-2 rounded-lg text-xs font-semibold transition-opacity hover:opacity-75 text-center"
                    style={{
                      color: "var(--clr-navy)",
                      border: "1.5px solid var(--clr-navy)",
                      background: "transparent",
                      textDecoration: "none",
                    }}
                  >
                    Sign Up
                  </Link>
                  <Link
                    href="/auth?mode=signin"
                    prefetch={false}
                    onClick={() => {
                      setUserMenuOpen(false);
                      setUserMenuPos(null);
                    }}
                    className="w-full px-3 py-2 rounded-lg text-xs font-semibold text-white transition-opacity hover:opacity-80 text-center"
                    style={{ background: "var(--clr-navy)", textDecoration: "none" }}
                  >
                    Login
                  </Link>
                </>
              )}
            </div>
          </div>
        </>
      )}

      {/* ── 채팅 영역 ────────────────────────────────────────── */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">

        {/* 채팅 툴바 — 사이드바 토글 + 현재 대화 제목 */}
        <div
          className="flex items-center gap-2.5 sm:gap-3 px-3 py-2.5 sm:px-5 sm:py-3 flex-shrink-0"
        >
          {/* 사이드바 토글 — 항상 표시 (모바일: 열기 버튼) */}
          <button
            onClick={() => setSidebarOpen((v) => !v)}
            className="p-1.5 rounded-lg hover:bg-white/40 transition-colors flex-shrink-0"
            style={{ color: "var(--clr-text-muted)" }}
          >
            <IconSidebar />
          </button>

          <span
            className="min-w-0 flex-1 text-xs sm:text-sm font-semibold truncate"
            style={{ color: "var(--clr-text)" }}
          >
            {chatTitle}
          </span>

          <Link
            href="/"
            aria-label="메인 홈페이지로 돌아가기"
            title="메인 홈페이지로 돌아가기"
            className="ml-auto flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg transition-colors hover:bg-white/40"
            style={{ color: "var(--clr-text-muted)", textDecoration: "none" }}
          >
            <IconArrowLeft />
          </Link>
        </div>

        {/* 메시지 영역 */}
        <div className="flex-1 overflow-y-auto px-3 py-4 sm:px-5 sm:py-6 flex flex-col gap-4 sm:gap-5">

          {/* 빈 상태 */}
          {messages.length === 0 && chatState === "idle" && (
            <div className="flex-1 flex flex-col items-center justify-center gap-4 px-4 text-center">
              <p className="text-xs sm:text-sm" style={{ color: "var(--clr-text-muted)" }}>
                궁금한 내용을 아래 입력창에 입력해 보세요.
              </p>
              <PopularQuestions onSelect={startConversation} />
            </div>
          )}

          {messages.map((msg) =>
            msg.role === "user" ? (
              /* ── 사용자 말풍선 ── */
              <div key={msg.id} className="flex justify-end">
                <span
                  className="max-w-[80%] sm:max-w-xs md:max-w-md px-3 sm:px-4 py-2 sm:py-2.5 rounded-2xl text-xs sm:text-sm leading-relaxed"
                  style={{
                    background: "rgba(255,255,255,0.55)",
                    color: "var(--clr-text)",
                    border: "1px solid rgba(255,255,255,0.75)",
                    backdropFilter: "blur(10px)",
                    WebkitBackdropFilter: "blur(10px)",
                  }}
                >
                  {msg.content}
                </span>
              </div>
            ) : (
              <AssistantMessage
                key={msg.id}
                msg={msg}
                onFeedbackChange={(next) => void handleFeedbackChange(msg.id, next)}
              />
            )
          )}

          {/* JSON 답변이 완성될 때까지 로딩 표시 */}
          {chatState === "loading" && (
            <div className="flex justify-start">
              <div className="glass-card rounded-2xl px-4 sm:px-5 py-3 sm:py-4 shadow-sm">
                <div className="flex items-center gap-2">
                  <div className="flex gap-1.5">
                    <span className="typing-dot" />
                    <span className="typing-dot" />
                    <span className="typing-dot" />
                  </div>
                  <span
                    className="status-fade-in text-[11px] sm:text-xs"
                    style={{ color: "var(--clr-text-muted)" }}
                  >
                    답변과 관련 자료를 준비하고 있어요...
                  </span>
                </div>
              </div>
            </div>
          )}

          {/* 에러 */}
          {chatState === "error" && (
            <div className="flex justify-start">
              <div className="glass-card rounded-2xl px-4 sm:px-5 py-3 sm:py-4 shadow-sm flex flex-wrap items-center gap-2 sm:gap-3">
                <p className="text-xs sm:text-sm" style={{ color: "#c0392b" }}>
                  {chatError ?? "답변 생성에 실패했어요."}
                </p>
                <button
                  onClick={() => setChatState("idle")}
                  className="text-xs px-3 py-1.5 rounded-full border font-medium"
                  style={{ color: "var(--clr-navy)", borderColor: "var(--clr-navy)" }}
                >
                  다시 시도하기
                </button>
              </div>
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {/* ── 입력창 + 면책 문구 ──────────────────────────────── */}
        <div className="flex-shrink-0 px-3 pb-3 sm:px-5 sm:pb-4 flex flex-col gap-1.5 sm:gap-2">
          <div className="glass-input flex items-center rounded-xl sm:rounded-2xl px-3 sm:px-4 py-1.5 sm:py-2 shadow-sm gap-1.5 sm:gap-2">
            {/* 텍스트 입력 */}
            <input
              ref={inputRef}
              type="text"
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
              placeholder=""
              disabled={chatState === "loading"}
              className="flex-1 min-w-0 bg-transparent outline-none text-xs sm:text-sm py-1.5 sm:py-2 font-semibold placeholder:text-gray-400 disabled:opacity-60"
              style={{ color: "var(--clr-text)" }}
            />

            {/* 전송 버튼 */}
            <button
              onClick={handleSend}
              disabled={!inputValue.trim() || chatState === "loading"}
              className="flex-shrink-0 w-7 h-7 sm:w-8 sm:h-8 rounded-full flex items-center justify-center text-white transition-opacity disabled:opacity-35"
              style={{ background: "var(--clr-navy)" }}
            >
              <IconSend />
            </button>
          </div>

          {/* 면책 문구 */}
          <p
            className="text-center text-[10px] sm:text-xs leading-relaxed"
            style={{ color: "var(--clr-text-muted)" }}
          >
            {chatMeta.disclaimer}
          </p>
        </div>
      </div>

      {/* ── 토스트 ── */}
      {toast && (
        <div
          className="fixed bottom-20 left-1/2 -translate-x-1/2 z-[9999] px-4 py-2.5 rounded-xl text-xs sm:text-[13px] font-semibold pointer-events-none"
          style={{
            background: "rgba(37,52,139,0.92)",
            color: "white",
            backdropFilter: "blur(12px)",
            WebkitBackdropFilter: "blur(12px)",
            boxShadow: "0 4px 20px rgba(0,0,0,0.18)",
            animation: "fadeInUp 0.2s ease",
          }}
        >
          {toast}
        </div>
      )}
    </div>
  );
}
