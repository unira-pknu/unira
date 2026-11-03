"use client";

// 전역 인증 상태 컨텍스트.
// - 마운트 시 현재 세션을 한 번 읽고, 그 뒤로는 onAuthStateChange 구독으로 동기화한다.
// - 액션(signIn / signUp / OAuth / signOut)은 Supabase SDK 호출을 얇게 래핑해서
//   호출부가 await 한 번으로 결과(에러/성공/이메일확인필요)를 알 수 있게 한다.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import type { Session, User } from "@supabase/supabase-js";

import { supabase } from "@/app/lib/supabase/client";

// ── 액션 결과 타입 ────────────────────────────────────────────────────────────
// 성공/실패만 구분하는 단순 형태. UI 가 에러 메시지를 그대로 띄울 수 있게 message 를 둠.
export interface AuthActionResult {
  ok: boolean;
  /** 사용자에게 보여줄 짧은 메시지 (성공 안내 또는 에러). */
  message?: string;
  /** 회원가입 시 이메일 확인 메일 발송 여부 — UI 분기에 사용. */
  emailConfirmationRequired?: boolean;
}

interface AuthContextValue {
  user: User | null;
  session: Session | null;
  /** 초기 세션 로딩 중 플래그 — 헤더 깜빡임 방지에 사용. */
  loading: boolean;

  signInWithPassword: (email: string, password: string) => Promise<AuthActionResult>;
  signUpWithPassword: (email: string, password: string) => Promise<AuthActionResult>;
  /** 비밀번호 재설정 메일 발송. 메일 링크는 /auth/confirm → /auth/reset-password 로 온다. */
  sendPasswordReset: (email: string) => Promise<AuthActionResult>;
  /** 로그인된 사용자의 비밀번호 변경. 재설정 링크로 들어왔으면 현재 비밀번호 없이, 설정 메뉴에서는
   *  현재 비밀번호와 함께(Supabase 'Require current password' 켜짐). */
  updatePassword: (password: string, currentPassword?: string) => Promise<AuthActionResult>;
  signOut: () => Promise<AuthActionResult>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const ALREADY_REGISTERED_MESSAGE = "이미 가입된 계정입니다. 로그인해 주세요.";

// 인증 메일은 Gmail SMTP(unira.pknu@gmail.com)로 보내 학교 메일에서 스팸함에 들어가기 쉽다
// (2026-11-02 첫 시험에서 '위험한 메일'로 분류되고 링크가 막힘). 메일을 기다리는 화면에서 먼저 알린다.
// 받는 주소를 함께 보여 줘 도메인을 잘못 고른 실수가 바로 보이게 한다. (줄마다 화면에서 나눠 그린다)
export const MAIL_SENT_HINT = (email: string, detail: string) =>
  `메일을 확인해 주세요\n${email}\n${detail} 안 보이면 스팸함도 확인해 주세요.`;

// ── 에러 메시지 정규화 ────────────────────────────────────────────────────────
// Supabase 에러 메시지는 영어라 한국어로 짧게 매핑. 매칭 안 되면 원문 그대로.
function normalizeAuthError(message: string | undefined | null): string {
  if (!message) return "알 수 없는 오류가 발생했습니다.";
  const m = message.toLowerCase();
  if (m.includes("invalid login credentials")) {
    return "이메일 또는 비밀번호가 올바르지 않습니다.";
  }
  if (m.includes("email not confirmed")) {
    return "이메일 인증이 완료되지 않았습니다. 받은 메일을 확인해 주세요.";
  }
  if (m.includes("user already registered")) {
    return ALREADY_REGISTERED_MESSAGE;
  }
  // Supabase Auth 비밀번호 기준(Password requirements)에 걸린 경우
  if (m.includes("password should be at least") || m.includes("password should contain")) {
    return "비밀번호는 8자 이상, 영문과 숫자를 모두 포함해야 합니다.";
  }
  // 'Require current password' — Supabase는 틀린 현재 비밀번호에도 "...required..."로 답한다
  // (11-03 실제 계정 확인). 변경 화면은 칸이 비면 버튼이 꺼지므로 여기 오면 틀린 것이다.
  if (m.includes("current password")) {
    return "현재 비밀번호가 맞지 않아요.";
  }
  if (m.includes("reauthentication")) {
    return "보안을 위해 다시 로그인한 뒤 비밀번호를 바꿔 주세요.";
  }
  if (m.includes("should be different from the old password")) {
    return "지금 쓰는 비밀번호와 다른 비밀번호를 입력해 주세요.";
  }
  if (m.includes("weak") || m.includes("pwned") || m.includes("known to be")) {
    return "너무 흔하거나 유출된 적 있는 비밀번호예요. 다른 비밀번호를 써 주세요.";
  }
  // 같은 주소로 메일(가입 확인·재설정)을 60초 안에 다시 요청한 경우
  if (m.includes("for security purposes")) {
    return "메일은 1분에 한 번만 요청할 수 있어요. 잠시 후 다시 시도해 주세요.";
  }
  if (m.includes("failed to fetch") || m.includes("network")) {
    return "서버에 연결하지 못했습니다. 인터넷 연결을 확인하고 다시 시도해 주세요.";
  }
  if (m.includes("rate limit") || m.includes("too many")) {
    return "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.";
  }
  return message;
}

// 인증 메일 링크가 도착할 화면(/auth/confirm) 주소. next 는 인증 뒤 갈 곳.
function confirmUrl(next: string): string | undefined {
  if (typeof window === "undefined") return undefined;
  const url = new URL("/auth/confirm", window.location.origin);
  url.searchParams.set("next", next);
  return url.toString();
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  // 마운트 시 현재 세션 로드 + 변경 구독.
  useEffect(() => {
    let active = true;

    supabase.auth
      .getSession()
      .then(({ data }) => {
        if (!active) return;
        setSession(data.session ?? null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    const { data: sub } = supabase.auth.onAuthStateChange((_event, newSession) => {
      setSession(newSession ?? null);
    });

    return () => {
      active = false;
      sub.subscription.unsubscribe();
    };
  }, []);

  // ── 액션 ────────────────────────────────────────────────────────────────────
  const signInWithPassword = useCallback(
    async (email: string, password: string): Promise<AuthActionResult> => {
      const { error } = await supabase.auth.signInWithPassword({ email, password });
      if (error) return { ok: false, message: normalizeAuthError(error.message) };
      return { ok: true };
    },
    [],
  );

  const signUpWithPassword = useCallback(
    async (email: string, password: string): Promise<AuthActionResult> => {
      const { data, error } = await supabase.auth.signUp({
        email,
        password,
        options: {
          // 메일 템플릿이 이 주소 뒤에 &token_hash=…&type=email 을 붙인다(/auth/confirm 참고).
          emailRedirectTo: confirmUrl("/"),
        },
      });
      if (error) return { ok: false, message: normalizeAuthError(error.message) };

      // 이미 가입(인증 완료)된 이메일: Supabase 는 이메일 열거 공격 방지 때문에 에러 대신
      // identities 가 빈 배열인 가짜 user 를 돌려주고 메일도 보내지 않는다. 이걸 성공으로
      // 처리하면 "확인 메일을 보냈습니다"가 떠서 사용자가 오지 않는 메일을 기다리게 된다.
      if (data.user && Array.isArray(data.user.identities) && data.user.identities.length === 0) {
        return { ok: false, message: ALREADY_REGISTERED_MESSAGE };
      }

      // Supabase 는 이메일 확인이 켜져 있으면 session 을 null 로 반환하고 confirmation 메일을 보낸다.
      const emailConfirmationRequired = !data.session;
      return {
        ok: true,
        emailConfirmationRequired,
        message: emailConfirmationRequired
          ? MAIL_SENT_HINT(email, "인증 링크를 보냈어요.")
          : undefined,
      };
    },
    [],
  );

  const sendPasswordReset = useCallback(async (email: string): Promise<AuthActionResult> => {
    const { error } = await supabase.auth.resetPasswordForEmail(email, {
      // 메일 템플릿이 이 주소 뒤에 &token_hash=…&type=recovery 를 붙인다(/auth/confirm 참고).
      redirectTo: confirmUrl("/auth/reset-password"),
    });
    // 없는 주소여도 Supabase 는 성공으로 답한다(가입 여부를 알려 주지 않으려고). 화면도 같은 문구.
    if (error) return { ok: false, message: normalizeAuthError(error.message) };
    return { ok: true };
  }, []);

  const updatePassword = useCallback(async (password: string, currentPassword?: string): Promise<AuthActionResult> => {
    const { error } = await supabase.auth.updateUser(
      currentPassword ? { password, current_password: currentPassword } : { password },
    );
    if (error) return { ok: false, message: normalizeAuthError(error.message) };
    return { ok: true };
  }, []);

  const signOut = useCallback(async (): Promise<AuthActionResult> => {
    const { error } = await supabase.auth.signOut();
    if (error) return { ok: false, message: normalizeAuthError(error.message) };
    return { ok: true };
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user: session?.user ?? null,
      session,
      loading,
      signInWithPassword,
      signUpWithPassword,
      sendPasswordReset,
      updatePassword,
      signOut,
    }),
    [session, loading, signInWithPassword, signUpWithPassword, sendPasswordReset, updatePassword, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an <AuthProvider>");
  }
  return ctx;
}

// ── 표시용 헬퍼 ───────────────────────────────────────────────────────────────
// Google/Kakao 는 user_metadata 에 name/full_name 이 있고, 이메일 가입은 email 만 있다.
export function displayNameOf(user: User | null): string {
  if (!user) return "Guest";
  const meta = user.user_metadata ?? {};
  const candidate =
    (typeof meta.full_name === "string" && meta.full_name) ||
    (typeof meta.name === "string" && meta.name) ||
    (typeof meta.user_name === "string" && meta.user_name) ||
    user.email ||
    "사용자";
  return String(candidate);
}
