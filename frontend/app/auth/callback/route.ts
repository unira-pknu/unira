// OAuth / 이메일 확인 redirect 후 도착하는 콜백 라우트.
//
// Supabase 가 ?code=... 쿼리로 돌려보내면 그것을 세션 쿠키로 교환한다.
// 교환이 끝나면 next 쿼리(원래 가려던 곳) 또는 / 로 redirect.

import { NextResponse, type NextRequest } from "next/server";
import { cookies } from "next/headers";

import { safeNextPath, type AuthErrorCode } from "@/app/lib/safeRedirect";
import { createServerSupabaseClient } from "@/app/lib/supabase/server";

// 로그인 화면에는 오류 원문 대신 코드만 넘긴다(화면이 정해 둔 문구로 바꿔 보여준다).
function errorCode(message: string): AuthErrorCode {
  return /expired|invalid/i.test(message) ? "link_expired" : "callback_failed";
}

function toAuthPage(origin: string, code: AuthErrorCode) {
  const url = new URL("/auth", origin);
  url.searchParams.set("error", code);
  return NextResponse.redirect(url);
}

export async function GET(request: NextRequest) {
  const { searchParams, origin } = new URL(request.url);
  const code = searchParams.get("code");
  // open redirect 방어: 외부 URL 로 못 튀게 이 사이트 안의 경로만 허용.
  const nextPath = safeNextPath(searchParams.get("next"));

  // OAuth provider 가 에러를 직접 돌려주는 케이스 (사용자가 동의 거부, 만료된 메일 링크 등)
  const oauthError = searchParams.get("error_description") ?? searchParams.get("error");
  if (oauthError) {
    return toAuthPage(origin, errorCode(oauthError));
  }

  if (code) {
    const cookieStore = await cookies();
    const supabase = createServerSupabaseClient(cookieStore);
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (error) {
      return toAuthPage(origin, errorCode(error.message));
    }
  }

  return NextResponse.redirect(new URL(nextPath, origin));
}
