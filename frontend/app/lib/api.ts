/**
 * Client-side helper for talking to the Next.js Route Handler at /api/ask,
 * which in turn proxies to the FastAPI backend.
 *
 * Also adapts the backend's lean Source shape ({title, uri, content})
 * into the richer Source shape the chat UI renders.
 */

import { supabase } from "@/app/lib/supabase/client";

export type Attachment = { name: string; url: string };

export type ChatSource = {
  id: number;
  title: string;
  category: string;
  chipMeta?: string;
  quote: string;
  quoteSource?: string;
  url: string;
  attachments: Attachment[];
};

export type AskResult = {
  answer: string;
  sources: ChatSource[];
  answerable: boolean;
  retrievalStatus?: "complete" | "partial";
};

type BackendSource = {
  title?: string;
  uri?: string;
  content?: string;
  attachments?: Array<{ name?: unknown; url?: unknown }>;
};

type BackendAskResponse = {
  answer?: string;
  sources?: BackendSource[];
  answerable?: boolean;
  retrieval_status?: "complete" | "partial";
};

const DEFAULT_CATEGORY = "자료";

function adaptSource(src: BackendSource, index: number): ChatSource {
  return {
    // 1-based id keeps the answer's [1][2] markers aligned with the
    // backend's source order.
    id: index + 1,
    title: (src.title ?? "").trim() || "(제목 없음)",
    category: DEFAULT_CATEGORY,
    quote: (src.content ?? "").trim(),
    url: (src.uri ?? "").trim() || "#",
    attachments: (src.attachments ?? []).flatMap((attachment) => {
      const name = typeof attachment.name === "string" ? attachment.name.trim() : "";
      const url = typeof attachment.url === "string" ? attachment.url.trim() : "";
      return name && url ? [{ name, url }] : [];
    }),
  };
}

async function readErrorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown; error?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
    if (typeof body.error === "string" && body.error.trim()) return body.error;
  } catch {
    // body wasn't JSON — fall through to status-based message
  }
  return `요청 실패 (HTTP ${res.status})`;
}

/** /api/ask failure; preserve limits (429) and search availability (503)
 * so the chat can show their user-facing guidance. */
export class AskError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export type ChatTurn = { role: "user" | "assistant"; content: string };

// 이어지는 질문("그럼 거기 전화번호는?")을 알아듣는 데는 최근 몇 턴이면 충분하다.
// 턴이 늘수록 매 질문의 프롬프트(요금·속도)도 늘어서 3번 주고받은 만큼만 보낸다.
const HISTORY_TURNS = 6;
const HISTORY_TURN_MAX_CHARS = 4000; // 백엔드 ChatMessage.content 상한

/** 백엔드가 실제로 들어간 단계(backend routers/ask.py _ask_events). 문구는 화면이 정한다. */
export type AskStage = "planning" | "searching" | "writing" | "revising";

export type AskProgress = {
  onStage?: (stage: AskStage, count?: number) => void;
  /** 지금까지 쓰인 답 전체(조각을 이어 붙인 것). 숫자 확인으로 고쳐 쓰면 고친 답으로 바뀐다. */
  onText?: (text: string) => void;
};

/** progress 를 넘기면 단계 안내·답 글자를 받는 대로 알려 주고, 끝나면 같은 AskResult 를 돌려준다. */
export async function askBackend(
  question: string,
  history: ChatTurn[] = [],
  signal?: AbortSignal,
  progress?: AskProgress,
): Promise<AskResult> {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) {
    throw new Error("로그인이 필요합니다.");
  }

  const res = await fetch("/api/ask", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: progress ? "text/event-stream" : "application/json",
      Authorization: `Bearer ${session.access_token}`,
    },
    body: JSON.stringify({
      question,
      stream: !!progress,
      chat_history: history
        .filter((turn) => turn.content.trim())
        .slice(-HISTORY_TURNS)
        .map((turn) => ({ role: turn.role, content: turn.content.slice(0, HISTORY_TURN_MAX_CHARS) })),
    }),
    signal,
  });

  if (!res.ok) {
    throw new AskError(await readErrorMessage(res), res.status);
  }

  const data = res.headers.get("Content-Type")?.includes("text/event-stream")
    ? await readAskStream(res, progress ?? {})
    : ((await res.json()) as BackendAskResponse);
  if (typeof data?.answer !== "string" || !Array.isArray(data.sources)) {
    throw new Error("답변 응답 형식이 올바르지 않습니다.");
  }
  // Older backends omit the flag; treat that as answerable.
  const answerable = data.answerable !== false;
  return {
    answer: (data.answer ?? "").trim(),
    // A "no info" refusal cites nothing, so don't show source chips under it.
    sources: answerable ? (data.sources ?? []).map(adaptSource) : [],
    answerable,
    retrievalStatus: data.retrieval_status,
  };
}

type AskStreamEvent =
  | { type: "status"; stage: AskStage; count?: number }
  | { type: "token"; text: string }
  | { type: "replace"; text: string }
  | ({ type: "done" } & BackendAskResponse)
  | { type: "error"; detail?: string; status_code?: number };

/** "data: {...}\n\n" 이벤트를 받는 대로 처리하고, done 이벤트의 내용을 돌려준다. */
async function readAskStream(res: Response, progress: AskProgress): Promise<BackendAskResponse> {
  const reader = res.body!.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let text = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let end: number;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const line = buffer.slice(0, end).trim();
      buffer = buffer.slice(end + 2);
      if (!line.startsWith("data:")) continue;
      const event = JSON.parse(line.slice(5)) as AskStreamEvent;
      if (event.type === "status") progress.onStage?.(event.stage, event.count);
      else if (event.type === "token") progress.onText?.((text += event.text));
      else if (event.type === "replace") progress.onText?.((text = event.text));
      else if (event.type === "error") throw new AskError(event.detail ?? "답변 생성에 실패했어요.", event.status_code ?? 502);
      else if (event.type === "done") {
        await reader.cancel();
        return event;
      }
    }
  }
  throw new AskError("답변이 끝나기 전에 연결이 끊겼어요.", 502);
}

// ── 관리자 대시보드 (/api/admin/dashboard → 백엔드 /admin/dashboard) ─────────────

export type AdminThumbsEntry = {
  id: string;
  kind: "up" | "down";
  question: string;
  answer: string;
  createdAt: string;
  reasons: string[];
};

export type AdminCommentEntry = {
  id: string;
  comment: string;
  relatedQuestion: string | null;
  createdAt: string;
};

export type AdminTopQuestion = { id: string; question: string; count: number };

export type AdminDashboard = {
  summary: {
    totalFeedback: number;
    upCount: number;
    downCount: number;
    /** 피드백이 0건이면 null */
    satisfactionRate: number | null;
    totalQuestions: number;
    /** 자주 묻는 질문 집계에 쓴 최근 질문 수 */
    questionSampleSize: number;
    lastUpdatedAt: string;
  };
  feedback: { thumbs: AdminThumbsEntry[]; comments: AdminCommentEntry[] };
  topQuestions: AdminTopQuestion[];
};

/** 실패 시 status(401 로그인 필요 / 403 관리자 아님 등)를 담아 던진다. */
export class AdminApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

/** 로그인한 사용자가 관리자인지. 로그아웃 상태나 오류면 false. */
export async function fetchIsAdmin(signal?: AbortSignal): Promise<boolean> {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) return false;

  try {
    const res = await fetch("/api/admin/me", {
      headers: { Accept: "application/json", Authorization: `Bearer ${session.access_token}` },
      cache: "no-store",
      signal,
    });
    if (!res.ok) return false;
    const body = (await res.json()) as { isAdmin?: unknown };
    return body.isAdmin === true;
  } catch {
    return false;
  }
}

export async function fetchAdminDashboard(signal?: AbortSignal): Promise<AdminDashboard> {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) throw new AdminApiError("로그인이 필요합니다.", 401);

  const res = await fetch("/api/admin/dashboard", {
    headers: { Accept: "application/json", Authorization: `Bearer ${session.access_token}` },
    cache: "no-store",
    signal,
  });
  if (!res.ok) throw new AdminApiError(await readErrorMessage(res), res.status);
  return (await res.json()) as AdminDashboard;
}

/** 여러 사용자가 물어본 인기 질문(없으면 빈 배열). 실패해도 던지지 않는다. */
export async function fetchPopularQuestions(signal?: AbortSignal): Promise<string[]> {
  try {
    const res = await fetch("/api/popular-questions", { headers: { Accept: "application/json" }, signal });
    if (!res.ok) return [];
    const body = (await res.json()) as { questions?: unknown };
    return Array.isArray(body.questions)
      ? body.questions.filter((q): q is string => typeof q === "string")
      : [];
  } catch {
    return [];
  }
}
