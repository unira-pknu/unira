/**
 * Where to go after login, limited to this site.
 *
 * `?next=` comes from the URL, so anyone can put `//evil.example` there; it
 * passes a `startsWith("/")` check but browsers read it as another host.
 * Resolving against a placeholder origin catches that and the variants
 * browsers normalize (`/\evil.example`, tabs or newlines inside the URL).
 */
const PLACEHOLDER_ORIGIN = "https://unira.invalid";

export function safeNextPath(raw: string | null | undefined): string {
  if (!raw || !raw.startsWith("/")) return "/";
  try {
    const url = new URL(raw, PLACEHOLDER_ORIGIN);
    if (url.origin !== PLACEHOLDER_ORIGIN) return "/";
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return "/";
  }
}

/**
 * Login-page messages, by the code the callback route puts in `?error=`.
 * Only these are shown: the page must not display text taken from the URL.
 */
export const AUTH_ERROR_MESSAGES = {
  link_expired: "인증 링크가 만료되었거나 이미 사용되었습니다. 다시 시도해 주세요.",
  callback_failed: "로그인 처리 중 문제가 생겼습니다. 다시 시도해 주세요.",
} as const;

export type AuthErrorCode = keyof typeof AUTH_ERROR_MESSAGES;

export function authErrorMessage(code: string | null | undefined): string | null {
  if (!code) return null;
  return code in AUTH_ERROR_MESSAGES
    ? AUTH_ERROR_MESSAGES[code as AuthErrorCode]
    : AUTH_ERROR_MESSAGES.callback_failed;
}
