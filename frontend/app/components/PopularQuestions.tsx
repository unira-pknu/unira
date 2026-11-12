"use client";

import { useEffect, useState } from "react";
import homeData from "@/data/routes/home.json";
import { fetchPopularQuestions } from "../lib/api";
import { shortLabel } from "./popularLabel";

const { title, defaults } = homeData.page.popular;
const MAX_ITEMS = 5;

type PopularItem = { label: string; question: string };

/** 띄어쓰기·끝 문장부호만 다른 질문, 짧은 이름이 같은 질문은 하나만 둔다("졸업 요건을 알려주세요" = "…알려주세요."). */
function dedupeQuestions(items: PopularItem[]): PopularItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const keys = [item.question.replace(/\s+/g, "").replace(/[.?!。？！~]+$/u, ""), `label:${item.label}`];
    if (keys.some((key) => seen.has(key))) return false;
    keys.forEach((key) => seen.add(key));
    return true;
  });
}

/**
 * 자주 묻는 질문 목록. 칩에는 짧은 이름("휴학 신청"), 누르면 원래 질문 전체로 바로 묻는다(onSelect).
 * 여러 사용자가 물어본 질문(백엔드 /popular-questions)을 먼저, 모자라면 기본 질문으로 채운다.
 */
export default function PopularQuestions({
  onSelect,
  align = "center",
}: {
  onSelect: (question: string) => void;
  align?: "center" | "start";
}) {
  const [items, setItems] = useState<PopularItem[]>(defaults.slice(0, MAX_ITEMS));

  useEffect(() => {
    const controller = new AbortController();
    fetchPopularQuestions(controller.signal).then((popular) => {
      if (controller.signal.aborted || popular.length === 0) return;
      const asked = popular.map((question) => ({ label: shortLabel(question), question }));
      setItems(dedupeQuestions([...asked, ...defaults]).slice(0, MAX_ITEMS));
    });
    return () => controller.abort();
  }, []);

  // 제목("자주 묻는 질문")은 화면에 쓰지 않고 화면 읽기 프로그램용 이름으로만 둔다.
  // PC는 한 줄, 휴대폰은 여러 줄.
  return (
    <div
      aria-label={title}
      className={`flex flex-wrap sm:flex-nowrap gap-1.5 sm:gap-2 ${align === "center" ? "justify-center" : ""}`}
    >
      {items.map((item) => (
        <button
          key={item.question}
          onClick={() => onSelect(item.question)}
          title={item.question}
          aria-label={item.question}
          className="glass-chip whitespace-nowrap px-3 py-1 sm:px-3.5 sm:py-1.5 rounded-full text-[11px] sm:text-xs font-semibold"
          style={{ color: "var(--clr-navy)" }}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
