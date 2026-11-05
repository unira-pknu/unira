import type { NextRequest } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";
const UPSTREAM_TIMEOUT_MS = 60_000;

export async function POST(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return Response.json(
      { detail: "request body must be valid JSON" },
      { status: 400 },
    );
  }

  const question = (body as { question?: unknown } | null)?.question;
  if (typeof question !== "string" || !question.trim()) {
    return Response.json(
      { detail: "`question` must be a non-empty string" },
      { status: 400 },
    );
  }

  const chatHistory = (body as { chat_history?: unknown } | null)?.chat_history;
  // 채팅 화면은 단계 안내·답 글자를 받으려고 stream: true 로 묻는다(백엔드 _ask_events).
  const wantsStream = (body as { stream?: unknown } | null)?.stream === true;

  const authHeader = request.headers.get("authorization");
  if (!authHeader) {
    return Response.json({ detail: "로그인이 필요합니다." }, { status: 401 });
  }

  // AbortController bridges the browser-aborted request to the upstream fetch
  // and also enforces a hard timeout so a hung backend can't hang Next.js.
  const controller = new AbortController();
  const onClientAbort = () => controller.abort();
  request.signal.addEventListener("abort", onClientAbort);
  const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
  const cleanup = () => {
    clearTimeout(timeout);
    request.signal.removeEventListener("abort", onClientAbort);
  };
  // 흘려보내는 중에는 응답을 돌려준 뒤에도 시간 제한·취소 연결이 살아 있어야 한다.
  let streaming = false;

  try {
    const upstream = await fetch(`${BACKEND_URL}/ask`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: wantsStream ? "text/event-stream" : "application/json",
        Authorization: authHeader,
      },
      body: JSON.stringify({
        question: question.trim(),
        stream: wantsStream,
        ...(Array.isArray(chatHistory) ? { chat_history: chatHistory } : {}),
      }),
      cache: "no-store",
      signal: controller.signal,
    });

    const contentType = upstream.headers.get("Content-Type") ?? "";
    if (wantsStream && upstream.ok && upstream.body && contentType.includes("text/event-stream")) {
      // 모아 두지 않고 받는 대로 넘긴다. 끝나거나 끊기면 정리한다.
      const passthrough = new TransformStream({ flush: cleanup });
      upstream.body.pipeTo(passthrough.writable).catch(cleanup);
      streaming = true;
      return new Response(passthrough.readable, {
        status: 200,
        headers: {
          "Content-Type": "text/event-stream; charset=utf-8",
          "Cache-Control": "no-cache, no-transform",
          "X-Accel-Buffering": "no",
        },
      });
    }

    // 로그인·횟수 제한 같은 오류, 또는 흘려보내기를 모르는 예전 백엔드는 JSON 그대로.
    if (!contentType.includes("application/json")) {
      await upstream.body?.cancel();
      return Response.json(
        { detail: "백엔드 응답 형식이 올바르지 않습니다. JSON 응답이 필요합니다." },
        { status: 502 },
      );
    }
    const data = await upstream.json();
    return Response.json(data, { status: upstream.status });
  } catch (err) {
    if (controller.signal.aborted && !request.signal.aborted) {
      return Response.json(
        { detail: "백엔드 응답이 너무 오래 걸립니다." },
        { status: 504 },
      );
    }
    console.error("[/api/ask] proxy failed:", err);
    return Response.json(
      { detail: "백엔드에 연결할 수 없습니다." },
      { status: 502 },
    );
  } finally {
    if (!streaming) cleanup();
  }
}
