import type { NextRequest } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export async function GET(request: NextRequest) {
  const authHeader = request.headers.get("authorization");
  const headers = { "Cache-Control": "no-store" };
  if (!authHeader) {
    return Response.json({ detail: "로그인이 필요합니다." }, { status: 401, headers });
  }

  const params = new URLSearchParams();
  for (const key of ["after_id", "limit"]) {
    const value = request.nextUrl.searchParams.get(key);
    if (value !== null) params.set(key, value);
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30_000);
  try {
    const upstream = await fetch(`${BACKEND_URL}/admin/feedback/training-candidates?${params}`, {
      headers: { Accept: "application/json", Authorization: authHeader },
      cache: "no-store",
      signal: controller.signal,
    });
    if (upstream.status === 404) {
      await upstream.body?.cancel();
      return Response.json(
        { detail: "운영 서버에 학습 후보 기능이 아직 반영되지 않았습니다. 서버 업데이트 후 다시 시도해 주세요." },
        { status: 503, headers },
      );
    }
    if (!upstream.headers.get("Content-Type")?.includes("application/json")) {
      await upstream.body?.cancel();
      return Response.json({ detail: "백엔드 응답 형식이 올바르지 않습니다." }, { status: 502, headers });
    }
    return Response.json(await upstream.json(), { status: upstream.status, headers });
  } catch {
    return Response.json(
      { detail: controller.signal.aborted ? "학습 후보 조회 시간이 초과되었습니다." : "백엔드에 연결할 수 없습니다." },
      { status: controller.signal.aborted ? 504 : 502, headers },
    );
  } finally {
    clearTimeout(timeout);
  }
}
