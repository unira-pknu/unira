"use client";

import { useState, useMemo, useRef, useEffect } from "react";
import { useRouter } from "next/navigation";
import docsData from "@/data/routes/docs.json";

const NAVY = "#25348B";
const NAVY_MUTED = "rgba(37,52,139,0.45)";

type CollectedDocument = {
  id: string;
  title: string;
  category: string;
  date: string;
  url: string;
  attachments: { name: string; url: string }[];
};

const documents: CollectedDocument[] = docsData.documents;

const CATEGORY_OPTIONS = ["공지사항", "학부안내", "학사안내"] as const;
const ATTACHMENT_OPTIONS = ["전체", "첨부파일 있음", "첨부파일 없음"] as const;
const SORT_OPTIONS = ["최신순", "오래된순"] as const;

const CATEGORY_COLORS: Record<string, string> = {
  공지사항: "rgba(37,52,139,0.12)",
  학부안내: "rgba(37,120,139,0.12)",
  학사안내: "rgba(120,37,139,0.1)",
};

// ── 아이콘 ─────────────────────────────────────────────────────────────────────
const IconArrowLeft = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <line x1="13" y1="8" x2="3" y2="8" />
    <polyline points="7,3 2,8 7,13" />
  </svg>
);
const IconSearch = () => (
  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8">
    <circle cx="6" cy="6" r="4.5" />
    <line x1="9.5" y1="9.5" x2="13" y2="13" />
  </svg>
);
const IconExternalLink = () => (
  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8">
    <path d="M5 2H2a1 1 0 00-1 1v7a1 1 0 001 1h7a1 1 0 001-1V7" />
    <polyline points="8,1 11,1 11,4" />
    <line x1="6" y1="6" x2="11" y2="1" />
  </svg>
);
const IconDocument = () => (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6">
    <path d="M3 1h6l3 3v9a1 1 0 01-1 1H3a1 1 0 01-1-1V2a1 1 0 011-1z" />
    <polyline points="9,1 9,4 12,4" />
    <line x1="4" y1="7" x2="10" y2="7" />
    <line x1="4" y1="9.5" x2="7.5" y2="9.5" />
  </svg>
);
const IconDownload = () => (
  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8">
    <line x1="6" y1="1" x2="6" y2="8" />
    <polyline points="3,5.5 6,8 9,5.5" />
    <line x1="2" y1="11" x2="10" y2="11" />
  </svg>
);
const IconChevronDown = ({ open }: { open: boolean }) => (
  <svg
    width="11" height="11" viewBox="0 0 12 12" fill="none"
    stroke="currentColor" strokeWidth="2" strokeLinecap="round"
    style={{ transition: "transform 0.2s ease", transform: open ? "rotate(180deg)" : "rotate(0deg)", flexShrink: 0 }}
  >
    <polyline points="2,4 6,8 10,4" />
  </svg>
);
const IconX = () => (
  <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <line x1="2" y1="2" x2="8" y2="8" />
    <line x1="8" y1="2" x2="2" y2="8" />
  </svg>
);
const IconRefresh = () => (
  <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
    <path d="M12 7A5 5 0 1 1 7 2" />
    <polyline points="7,1 9,3 7,5" />
  </svg>
);

function CheckBox({ checked }: { checked: boolean }) {
  return (
    <span
      className="flex-shrink-0 w-3.5 h-3.5 rounded flex items-center justify-center"
      style={{
        background: checked ? NAVY : "transparent",
        border: checked ? "none" : "1.5px solid rgba(0,0,0,0.22)",
        transition: "background 0.15s ease",
      }}
    >
      {checked && (
        <svg width="8" height="8" viewBox="0 0 10 10" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="1.5,5 4,7.5 8.5,2.5" />
        </svg>
      )}
    </span>
  );
}

function formatDate(iso: string) {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// ── 일반 드롭다운 ──────────────────────────────────────────────────────────────
function FilterDropdown({
  label,
  value,
  defaultValue,
  options,
  onChange,
}: {
  label: string;
  value: string;
  defaultValue: string;
  options: readonly string[];
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const isActive = value !== defaultValue;

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <div ref={ref} className="relative flex-shrink-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-semibold transition-all whitespace-nowrap"
        style={{
          background: isActive ? NAVY : "rgba(255,255,255,0.72)",
          color: isActive ? "white" : NAVY,
          border: `1px solid ${isActive ? NAVY : "rgba(255,255,255,0.9)"}`,
          backdropFilter: "blur(12px)",
          WebkitBackdropFilter: "blur(12px)",
        }}
      >
        {isActive ? value : label}
        <IconChevronDown open={open} />
      </button>
      {open && (
        <div
          className="absolute top-full left-0 z-50 mt-1.5 rounded-xl overflow-hidden"
          style={{
            minWidth: "130px",
            background: "rgba(255,255,255,0.97)",
            backdropFilter: "blur(20px)",
            WebkitBackdropFilter: "blur(20px)",
            border: "1px solid rgba(200,210,230,0.7)",
            boxShadow: "0 8px 28px rgba(0,0,0,0.13), 0 1.5px 6px rgba(0,0,0,0.07)",
          }}
        >
          {options.map((opt, i) => (
            <button
              key={opt}
              onClick={() => { onChange(opt); setOpen(false); }}
              className="w-full text-left px-4 py-2.5 text-xs transition-colors hover:bg-gray-50"
              style={{
                color: opt === value ? NAVY : "#555",
                fontWeight: opt === value ? 700 : 400,
                borderTop: i > 0 ? "1px solid rgba(0,0,0,0.05)" : "none",
              }}
            >
              {opt}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ── 카테고리 멀티셀렉트 드롭다운 ──────────────────────────────────────────────
function CategoryDropdown({
  selected,
  onChange,
}: {
  selected: Set<string>;
  onChange: (v: Set<string>) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const isActive = selected.size > 0;

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const toggle = (cat: string) => {
    const next = new Set(selected);
    if (next.has(cat)) next.delete(cat);
    else next.add(cat);
    onChange(next);
  };

  const buttonLabel = isActive ? `카테고리 (${selected.size})` : "카테고리";

  return (
    <div ref={ref} className="relative flex-shrink-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-semibold transition-all whitespace-nowrap"
        style={{
          background: isActive ? NAVY : "rgba(255,255,255,0.72)",
          color: isActive ? "white" : NAVY,
          border: `1px solid ${isActive ? NAVY : "rgba(255,255,255,0.9)"}`,
          backdropFilter: "blur(12px)",
          WebkitBackdropFilter: "blur(12px)",
        }}
      >
        {buttonLabel}
        <IconChevronDown open={open} />
      </button>
      {open && (
        <div
          className="absolute top-full left-0 z-50 mt-1.5 rounded-xl overflow-hidden"
          style={{
            minWidth: "150px",
            background: "rgba(255,255,255,0.97)",
            backdropFilter: "blur(20px)",
            WebkitBackdropFilter: "blur(20px)",
            border: "1px solid rgba(200,210,230,0.7)",
            boxShadow: "0 8px 28px rgba(0,0,0,0.13), 0 1.5px 6px rgba(0,0,0,0.07)",
          }}
        >
          {/* 전체 (전체 해제) */}
          <button
            onClick={() => onChange(new Set())}
            className="w-full flex items-center gap-2.5 px-4 py-2.5 text-xs transition-colors hover:bg-gray-50"
            style={{
              color: !isActive ? NAVY : "#555",
              fontWeight: !isActive ? 700 : 400,
              borderBottom: "1px solid rgba(0,0,0,0.05)",
            }}
          >
            <CheckBox checked={!isActive} />
            전체
          </button>
          {CATEGORY_OPTIONS.map((cat, i) => {
            const checked = selected.has(cat);
            return (
              <button
                key={cat}
                onClick={() => toggle(cat)}
                className="w-full flex items-center gap-2.5 px-4 py-2.5 text-xs transition-colors hover:bg-gray-50"
                style={{
                  color: checked ? NAVY : "#555",
                  fontWeight: checked ? 700 : 400,
                  borderTop: i > 0 ? "1px solid rgba(0,0,0,0.05)" : "none",
                }}
              >
                <CheckBox checked={checked} />
                {cat}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── 날짜 범위 드롭다운 ─────────────────────────────────────────────────────────
function DateRangeDropdown({
  dateStart,
  dateEnd,
  onChangeStart,
  onChangeEnd,
}: {
  dateStart: string;
  dateEnd: string;
  onChangeStart: (v: string) => void;
  onChangeEnd: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const isActive = !!(dateStart || dateEnd);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const label = isActive
    ? [dateStart || "시작일", dateEnd || "종료일"].join(" ~ ")
    : "날짜 범위";

  return (
    <div ref={ref} className="relative flex-shrink-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-semibold transition-all whitespace-nowrap"
        style={{
          background: isActive ? NAVY : "rgba(255,255,255,0.72)",
          color: isActive ? "white" : NAVY,
          border: `1px solid ${isActive ? NAVY : "rgba(255,255,255,0.9)"}`,
          backdropFilter: "blur(12px)",
          WebkitBackdropFilter: "blur(12px)",
        }}
      >
        {label}
        <IconChevronDown open={open} />
      </button>
      {open && (
        <div
          className="absolute top-full left-0 z-50 mt-1.5 rounded-xl p-3 flex flex-col gap-2"
          style={{
            minWidth: "260px",
            background: "rgba(255,255,255,0.97)",
            backdropFilter: "blur(20px)",
            WebkitBackdropFilter: "blur(20px)",
            border: "1px solid rgba(200,210,230,0.7)",
            boxShadow: "0 8px 28px rgba(0,0,0,0.13), 0 1.5px 6px rgba(0,0,0,0.07)",
          }}
        >
          <p className="text-[10px] font-semibold px-1" style={{ color: NAVY_MUTED }}>날짜 범위 선택</p>
          <div className="flex items-center gap-2">
            <input
              type="date"
              value={dateStart}
              onChange={(e) => onChangeStart(e.target.value)}
              max={dateEnd || undefined}
              className="flex-1 px-2.5 py-1.5 rounded-lg text-[11px] outline-none font-semibold"
              style={{
                background: "rgba(37,52,139,0.05)",
                border: "1.5px solid rgba(37,52,139,0.15)",
                color: NAVY,
                colorScheme: "light",
              }}
            />
            <span className="text-xs flex-shrink-0" style={{ color: NAVY_MUTED }}>~</span>
            <input
              type="date"
              value={dateEnd}
              onChange={(e) => onChangeEnd(e.target.value)}
              min={dateStart || undefined}
              className="flex-1 px-2.5 py-1.5 rounded-lg text-[11px] outline-none font-semibold"
              style={{
                background: "rgba(37,52,139,0.05)",
                border: "1.5px solid rgba(37,52,139,0.15)",
                color: NAVY,
                colorScheme: "light",
              }}
            />
          </div>
          {isActive && (
            <button
              onClick={() => { onChangeStart(""); onChangeEnd(""); }}
              className="text-[10px] font-medium text-left px-1 transition-opacity hover:opacity-70"
              style={{ color: NAVY_MUTED }}
            >
              날짜 초기화
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ── 활성 필터 태그 ─────────────────────────────────────────────────────────────
function FilterTag({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <span
      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px] font-semibold"
      style={{
        background: "rgba(37,52,139,0.1)",
        color: NAVY,
        border: "1px solid rgba(37,52,139,0.2)",
      }}
    >
      {label}
      <button
        onClick={onRemove}
        className="flex items-center justify-center hover:opacity-60 transition-opacity"
        style={{ color: NAVY }}
        aria-label={`${label} 필터 해제`}
      >
        <IconX />
      </button>
    </span>
  );
}

// ── 메인 페이지 ────────────────────────────────────────────────────────────────
export default function DocsPage() {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [filterCategories, setFilterCategories] = useState<Set<string>>(new Set());
  const [filterDateStart, setFilterDateStart] = useState("");
  const [filterDateEnd, setFilterDateEnd] = useState("");
  const [filterAttachment, setFilterAttachment] = useState("전체");
  const [filterSort, setFilterSort] = useState("최신순");

  const resetFilters = () => {
    setQuery("");
    setFilterCategories(new Set());
    setFilterDateStart("");
    setFilterDateEnd("");
    setFilterAttachment("전체");
    setFilterSort("최신순");
  };

  const hasActiveFilters =
    filterCategories.size > 0 ||
    !!filterDateStart ||
    !!filterDateEnd ||
    filterAttachment !== "전체" ||
    filterSort !== "최신순";

  const filtered = useMemo(() => {
    const normalize = (s: string) => s.toLowerCase().replace(/\s+/g, "");
    let docs = documents.filter((doc) => {
      if (filterCategories.size > 0 && !filterCategories.has(doc.category)) return false;
      if (query && !normalize(doc.title).includes(normalize(query))) return false;
      if (filterDateStart && doc.date && doc.date < filterDateStart) return false;
      if (filterDateEnd && doc.date && doc.date > filterDateEnd) return false;
      if (filterAttachment === "첨부파일 있음" && (!doc.attachments || doc.attachments.length === 0)) return false;
      if (filterAttachment === "첨부파일 없음" && doc.attachments && doc.attachments.length > 0) return false;
      return true;
    });

    docs = [...docs].sort((a, b) => {
      const da = a.date || "";
      const db = b.date || "";
      return filterSort === "최신순" ? db.localeCompare(da) : da.localeCompare(db);
    });

    return docs;
  }, [query, filterCategories, filterDateStart, filterDateEnd, filterAttachment, filterSort]);

  // 활성 필터 태그 목록
  const activeTags: { key: string; label: string; onRemove: () => void }[] = [];
  filterCategories.forEach((cat) => {
    activeTags.push({
      key: `cat-${cat}`,
      label: cat,
      onRemove: () => {
        const next = new Set(filterCategories);
        next.delete(cat);
        setFilterCategories(next);
      },
    });
  });
  if (filterDateStart || filterDateEnd) {
    const dateLabel = [filterDateStart || "시작일", filterDateEnd || "종료일"].join(" ~ ");
    activeTags.push({
      key: "date",
      label: dateLabel,
      onRemove: () => { setFilterDateStart(""); setFilterDateEnd(""); },
    });
  }
  if (filterAttachment !== "전체") {
    activeTags.push({ key: "att", label: filterAttachment, onRemove: () => setFilterAttachment("전체") });
  }
  if (filterSort !== "최신순") {
    activeTags.push({ key: "sort", label: filterSort, onRemove: () => setFilterSort("최신순") });
  }

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
      <main className="flex-1 w-full max-w-3xl mx-auto px-4 py-6 sm:px-6 sm:py-8 flex flex-col gap-6">

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

          <div className="flex flex-col gap-1">
            <h1 className="text-xl sm:text-2xl font-bold" style={{ color: NAVY }}>
              수집 문서 현황
            </h1>
            <p className="text-[11px] sm:text-xs" style={{ color: NAVY_MUTED }}>
              {docsData.meta.lastCrawledAt ? (
                <>
                  마지막 업데이트: {formatDate(docsData.meta.lastCrawledAt)}
                  &nbsp;·&nbsp;총 {docsData.meta.totalCount}건
                </>
              ) : "문서 목록을 준비 중입니다."}
            </p>
          </div>
        </div>

        {/* 검색창 */}
        <div
          className="flex items-center gap-2 px-3 py-2.5 rounded-xl text-sm w-full"
          style={{
            background: "rgba(255,255,255,0.72)",
            border: "1px solid rgba(255,255,255,0.9)",
            backdropFilter: "blur(12px)",
            WebkitBackdropFilter: "blur(12px)",
          }}
        >
          <span style={{ color: NAVY_MUTED }}><IconSearch /></span>
          <input
            type="text"
            placeholder="문서 제목 검색"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="flex-1 bg-transparent outline-none text-xs sm:text-sm placeholder:text-[rgba(37,52,139,0.35)] font-semibold"
            style={{ color: NAVY }}
          />
          {query && (
            <button
              onClick={() => setQuery("")}
              className="flex items-center justify-center hover:opacity-60 transition-opacity"
              style={{ color: NAVY_MUTED }}
              aria-label="검색어 지우기"
            >
              <IconX />
            </button>
          )}
        </div>

        {/* 필터 드롭다운 행 */}
        <div className="flex flex-wrap items-center gap-2">
          <CategoryDropdown
            selected={filterCategories}
            onChange={setFilterCategories}
          />
          <DateRangeDropdown
            dateStart={filterDateStart}
            dateEnd={filterDateEnd}
            onChangeStart={setFilterDateStart}
            onChangeEnd={setFilterDateEnd}
          />
          <FilterDropdown
            label="첨부파일"
            value={filterAttachment}
            defaultValue="전체"
            options={ATTACHMENT_OPTIONS}
            onChange={setFilterAttachment}
          />
          <FilterDropdown
            label="정렬"
            value={filterSort}
            defaultValue="최신순"
            options={SORT_OPTIONS}
            onChange={setFilterSort}
          />

          {/* 필터 초기화 */}
          {hasActiveFilters && (
            <button
              onClick={resetFilters}
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-semibold transition-all hover:opacity-75"
              style={{
                color: NAVY_MUTED,
                background: "rgba(255,255,255,0.6)",
                border: "1px solid rgba(255,255,255,0.85)",
                backdropFilter: "blur(12px)",
                WebkitBackdropFilter: "blur(12px)",
              }}
            >
              <IconRefresh />
              초기화
            </button>
          )}
        </div>

        {/* 활성 필터 태그 */}
        {activeTags.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 -mt-3">
            {activeTags.map((tag) => (
              <FilterTag key={tag.key} label={tag.label} onRemove={tag.onRemove} />
            ))}
          </div>
        )}

        {/* 결과 카운트 */}
        <div className="flex items-center justify-between -mt-2">
          <p className="text-[11px] sm:text-xs font-medium" style={{ color: NAVY_MUTED }}>
            {filtered.length}건 표시 중
          </p>
        </div>

        {/* 문서 목록 */}
        <div className="flex flex-col gap-2.5 -mt-2">
          {filtered.length === 0 ? (
            <div
              className="rounded-2xl p-8 text-center text-sm"
              style={{ background: "rgba(255,255,255,0.45)", color: NAVY_MUTED }}
            >
              {documents.length === 0 ? "표시할 문서 목록을 준비 중입니다." : "검색 결과가 없습니다."}
            </div>
          ) : (
            filtered.map((doc) => (
              <div
                key={doc.id}
                className="rounded-2xl px-4 py-3.5 sm:px-5 sm:py-4 flex items-center gap-3 sm:gap-4 transition-all hover:shadow-md"
                style={{
                  background: "rgba(255,255,255,0.58)",
                  backdropFilter: "blur(16px)",
                  WebkitBackdropFilter: "blur(16px)",
                  border: "1px solid rgba(255,255,255,0.88)",
                }}
              >
                {/* 아이콘 */}
                <div
                  className="flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center"
                  style={{ background: "rgba(37,52,139,0.08)", color: NAVY }}
                >
                  <IconDocument />
                </div>

                {/* 정보 */}
                <div className="flex-1 min-w-0 flex flex-col gap-1">
                  <span className="text-xs sm:text-sm font-semibold leading-snug" style={{ color: NAVY }}>
                    {doc.title}
                  </span>
                  <div className="flex items-center gap-2 flex-wrap">
                    <span
                      className="rounded-full px-2 py-0.5 text-[9px] sm:text-[10px] font-semibold"
                      style={{
                        background: CATEGORY_COLORS[doc.category] ?? "rgba(37,52,139,0.1)",
                        color: NAVY,
                      }}
                    >
                      {doc.category}
                    </span>
                    {doc.date && (
                      <span className="text-[10px] sm:text-[11px]" style={{ color: NAVY_MUTED }}>
                        {doc.date}
                      </span>
                    )}
                    {doc.attachments && doc.attachments.length > 0 && (
                      <span className="flex items-center gap-0.5" style={{ color: NAVY_MUTED }}>
                        <IconDownload />
                        {doc.attachments.map((att, i) => (
                          <span key={att.name} className="flex items-center">
                            {i > 0 && (
                              <span className="text-[10px] mx-0.5" style={{ color: NAVY_MUTED }}>|</span>
                            )}
                            <a
                              href={att.url}
                              className="text-[10px] sm:text-[11px] font-bold hover:underline"
                              style={{ color: NAVY }}
                            >
                              {att.name}
                            </a>
                          </span>
                        ))}
                      </span>
                    )}
                  </div>
                </div>

                {/* 원문 보기 */}
                <a
                  href={doc.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex-shrink-0 flex items-center gap-1 text-[11px] sm:text-xs font-semibold px-3 py-1.5 rounded-lg transition-colors hover:bg-white/80"
                  style={{
                    color: NAVY,
                    background: "rgba(255,255,255,0.55)",
                    border: "1px solid rgba(255,255,255,0.85)",
                  }}
                >
                  <span className="hidden sm:inline">원문 보기</span>
                  <IconExternalLink />
                </a>
              </div>
            ))
          )}
        </div>
      </main>

      {/* ── 푸터 ── */}
      <footer className="py-4 text-center text-[10px] sm:text-xs flex-shrink-0" style={{ color: NAVY_MUTED }}>
        공식 문서 기반 답변 · 첨부 파일 · 최신 공지 반영
      </footer>
    </div>
  );
}
