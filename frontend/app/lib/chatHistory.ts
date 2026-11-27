// Supabase-backed chat persistence helpers (RLS: anon JWT scoped by auth.uid()).

import { supabase } from "@/app/lib/supabase/client";
import type { ChatSource } from "@/app/lib/api";

export interface ChatRow {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ChatMessageRow {
  id: string;
  chat_id: string;
  role: "user" | "assistant";
  content: string;
  sources: unknown;
  created_at: string;
}

/** Sidebar 등에서 사용하는 형태로 채팅 목록 조회 */
export async function listChats(): Promise<ChatRow[]> {
  const { data, error } = await supabase
    .from("chats")
    .select("id, title, created_at, updated_at")
    .order("updated_at", { ascending: false });

  if (error) throw error;
  return (data ?? []) as ChatRow[];
}

export interface ChatSearchHit {
  chat_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

/** 내 대화(질문·답) 내용에서 글자를 찾는다. RLS 로 본인 채팅 메시지만 조회된다. 최신 순. */
export async function searchChatMessages(query: string, limit = 200): Promise<ChatSearchHit[]> {
  const q = query.trim();
  if (!q) return [];
  // LIKE 특수문자(%, _, \)는 글자 그대로 찾도록 막는다.
  const escaped = q.replace(/[\\%_]/g, (c) => `\\${c}`);
  const { data, error } = await supabase
    .from("chat_messages")
    .select("chat_id, role, content, created_at")
    .ilike("content", `%${escaped}%`)
    .order("created_at", { ascending: false })
    .limit(limit);

  if (error) throw error;
  return (data ?? []) as ChatSearchHit[];
}

export async function createChat(title: string, userId: string): Promise<ChatRow> {
  const { data, error } = await supabase
    .from("chats")
    .insert({ user_id: userId, title })
    .select("id, title, created_at, updated_at")
    .single();

  if (error) throw error;
  return data as ChatRow;
}

/** 저장된 메시지의 DB id 를 돌려준다 (답변 피드백을 이 id 에 연결). */
export async function insertChatMessage(
  chatId: string,
  role: "user" | "assistant",
  content: string,
  sources: ChatSource[] | null,
): Promise<string> {
  const { data, error } = await supabase
    .from("chat_messages")
    .insert({
      chat_id: chatId,
      role,
      content,
      sources,
    })
    .select("id")
    .single();

  if (error) throw error;
  return (data as { id: string }).id;
}

// ── 답변 피드백 (message_feedback, 015 마이그레이션) ─────────────────────────
// RLS: 본인 행만, 본인 채팅의 assistant 메시지에만 작성 가능. (message_id, user_id) 유니크.

export type FeedbackRating = "up" | "down";

export interface MessageFeedback {
  rating: FeedbackRating;
  /** 싫어요 사유 (좋아요면 빈 배열) */
  reasons: string[];
  /** 싫어요 추가 의견 (좋아요면 null) */
  comment: string | null;
}

/** 여러 메시지에 대해 내가 남긴 피드백을 message_id → 피드백 으로 조회 */
export async function fetchFeedbackForMessages(
  messageIds: string[],
): Promise<Record<string, MessageFeedback>> {
  if (messageIds.length === 0) return {};
  const { data, error } = await supabase
    .from("message_feedback")
    .select("message_id, rating, reasons, comment")
    .in("message_id", messageIds);

  if (error) throw error;
  const byMessage: Record<string, MessageFeedback> = {};
  for (const row of (data ?? []) as Array<MessageFeedback & { message_id: string }>) {
    byMessage[row.message_id] = { rating: row.rating, reasons: row.reasons ?? [], comment: row.comment };
  }
  return byMessage;
}

/** 피드백 저장. 같은 답변에 이미 있으면 덮어쓴다(좋아요 ↔ 싫어요 전환). */
export async function saveFeedback(messageId: string, feedback: MessageFeedback): Promise<void> {
  const isDown = feedback.rating === "down";
  const { error } = await supabase.from("message_feedback").upsert(
    {
      message_id: messageId,
      rating: feedback.rating,
      // DB 제약: 사유·의견은 싫어요에만 허용
      reasons: isDown ? feedback.reasons : [],
      comment: isDown ? feedback.comment : null,
    },
    { onConflict: "message_id,user_id" },
  );

  if (error) throw error;
}

/** 피드백 취소 (내 행만 지워진다 — RLS) */
export async function deleteFeedback(messageId: string): Promise<void> {
  const { error } = await supabase.from("message_feedback").delete().eq("message_id", messageId);

  if (error) throw error;
}

export async function fetchChatMessages(chatId: string): Promise<ChatMessageRow[]> {
  const { data, error } = await supabase
    .from("chat_messages")
    .select("id, chat_id, role, content, sources, created_at")
    .eq("chat_id", chatId)
    .order("created_at", { ascending: true });

  if (error) throw error;
  return (data ?? []) as ChatMessageRow[];
}

export async function updateChatTitle(chatId: string, title: string): Promise<void> {
  const { error } = await supabase.from("chats").update({ title }).eq("id", chatId);

  if (error) throw error;
}

export async function deleteChat(chatId: string): Promise<void> {
  const { error } = await supabase.from("chats").delete().eq("id", chatId);

  if (error) throw error;
}

/** DB jsonb → UI 출처 배열 (역직렬화 실패 시 빈 배열) */
export function parseStoredSources(raw: unknown): ChatSource[] | undefined {
  if (raw == null) return undefined;
  if (!Array.isArray(raw)) return undefined;
  const out: ChatSource[] = [];
  for (let i = 0; i < raw.length; i++) {
    const s = raw[i];
    if (!s || typeof s !== "object") continue;
    const o = s as Record<string, unknown>;
    out.push({
      id: typeof o.id === "number" ? o.id : i + 1,
      title: typeof o.title === "string" ? o.title : "(제목 없음)",
      category: typeof o.category === "string" ? o.category : "자료",
      chipMeta: typeof o.chipMeta === "string" ? o.chipMeta : undefined,
      quote: typeof o.quote === "string" ? o.quote : "",
      quoteSource: typeof o.quoteSource === "string" ? o.quoteSource : undefined,
      url: typeof o.url === "string" ? o.url : "#",
      attachments: Array.isArray(o.attachments)
        ? (o.attachments as { name: string; url: string }[])
        : [],
    });
  }
  return out.length > 0 ? out : undefined;
}
