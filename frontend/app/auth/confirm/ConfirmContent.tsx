"use client";

// 인증 메일(가입 확인·비밀번호 재설정)의 링크가 도착하는 화면.
//
// 메일 링크가 Supabase 인증 주소를 바로 열면(/auth/callback 방식) 링크가 열리는 순간 한 번뿐인
// 토큰이 쓰인다. 학교 메일(Google)이 보안 검사로 링크를 먼저 열어 보면 학생이 누를 땐 이미
// '만료'가 된다(2026-11-02 재설정 시험에서 otp_expired 두 번). 그래서 링크는 이 화면만 열고,
// 토큰은 사람이 버튼을 눌렀을 때 verifyOtp로 쓴다. token_hash 방식이라 메일을 요청한 브라우저가
// 아니어도(휴대폰 메일 앱 등) 된다.
//
// Supabase 메일 템플릿 링크:
//   재설정  {{ .RedirectTo }}&token_hash={{ .TokenHash }}&type=recovery
//   가입    {{ .RedirectTo }}&token_hash={{ .TokenHash }}&type=email
// (.RedirectTo 는 AuthContext 가 넘기는 /auth/confirm?next=... 이다)

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import type { EmailOtpType } from "@supabase/supabase-js";

import { supabase } from "@/app/lib/supabase/client";
import { safeNextPath } from "@/app/lib/safeRedirect";

const NAVY = "#25348B";
const NAVY_MUTED = "rgba(37,52,139,0.45)";

const COPY: Record<"recovery" | "email", { title: string; body: string; button: string }> = {
  recovery: {
    title: "비밀번호 재설정",
    body: "아래 버튼을 누르면 새 비밀번호를 정하는 화면으로 넘어가요.",
    button: "새 비밀번호 정하기",
  },
  email: {
    title: "이메일 인증",
    body: "아래 버튼을 누르면 이메일 인증이 끝나고 UNIRA를 이용할 수 있어요.",
    button: "이메일 인증 완료하기",
  },
};

export default function ConfirmContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const tokenHash = searchParams?.get("token_hash") ?? "";
  const nextPath = safeNextPath(searchParams?.get("next"));
  // type 이 없으면(옛 템플릿 링크가 Supabase 를 거쳐 돌아온 경우) 갈 곳으로 짐작한다.
  const type =
    searchParams?.get("type") === "recovery" || nextPath === "/auth/reset-password" ? "recovery" : "email";
  const copy = COPY[type];
  // 옛 템플릿({{ .ConfirmationURL }})이면 Supabase 가 토큰을 먼저 쓰고 실패를 ?error= 로 돌려준다.
  const expired = /expired|invalid/i.test(searchParams?.get("error_code") ?? "") ||
    searchParams?.get("error") === "access_denied";

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(
    tokenHash
      ? null
      : expired
        ? "링크가 만료되었거나 이미 사용되었어요. 메일을 다시 요청해 주세요."
        : "링크 주소가 올바르지 않아요. 메일의 링크를 다시 눌러 주세요.",
  );

  const confirm = async () => {
    if (submitting || !tokenHash) return;
    setSubmitting(true);
    setError(null);
    const { error: verifyError } = await supabase.auth.verifyOtp({
      token_hash: tokenHash,
      type: type as EmailOtpType,
    });
    if (verifyError) {
      setSubmitting(false);
      setError(
        /expired|invalid/i.test(verifyError.message)
          ? "링크가 만료되었거나 이미 사용되었어요. 메일을 다시 요청해 주세요."
          : "인증 처리 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.",
      );
      return;
    }
    router.replace(nextPath);
  };

  return (
    <div
      className="bg-app min-h-[100dvh] flex items-start justify-center p-3 sm:p-4 lg:p-5"
      style={{ paddingTop: "max(12px, calc((100dvh - 360px) / 2))" }}
    >
      <main
        className="home-card w-full flex flex-col gap-5 rounded-2xl sm:rounded-[24px] px-6 py-7 sm:px-8 sm:py-8"
        style={{ maxWidth: "440px" }}
      >
        <div className="flex flex-col gap-1">
          <h1 className="text-base sm:text-lg font-semibold" style={{ color: NAVY }}>
            {copy.title}
          </h1>
          <p className="text-xs leading-relaxed" style={{ color: NAVY_MUTED }}>
            {copy.body}
          </p>
        </div>

        {error && (
          <p
            role="alert"
            className="text-xs leading-relaxed px-3 py-2 rounded-lg"
            style={{ background: "rgba(229,62,62,0.08)", color: "#c53030", border: "1px solid rgba(229,62,62,0.18)" }}
          >
            {error}
          </p>
        )}

        <button
          type="button"
          onClick={confirm}
          disabled={submitting || !tokenHash}
          className="py-2.5 rounded-xl text-xs sm:text-sm font-semibold text-white transition-opacity enabled:hover:opacity-90 disabled:cursor-not-allowed"
          style={{ background: tokenHash ? NAVY : "rgba(37,52,139,0.32)", opacity: submitting ? 0.7 : 1 }}
        >
          {submitting ? "확인 중…" : copy.button}
        </button>

        {error && (
          <p className="text-xs text-center" style={{ color: NAVY_MUTED }}>
            <Link
              href={type === "recovery" ? "/auth?mode=reset" : "/auth?mode=signup"}
              className="font-semibold underline underline-offset-2 hover:opacity-75 transition-opacity"
              style={{ color: NAVY }}
            >
              {type === "recovery" ? "재설정 메일 다시 요청하기" : "회원가입 화면으로"}
            </Link>
          </p>
        )}
      </main>
    </div>
  );
}
