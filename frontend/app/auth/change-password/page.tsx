import { Suspense } from "react";
import AuthContent from "../AuthContent";

// 채팅 계정 메뉴 → 로그인한 채로 현재 비밀번호를 넣고 바꾼다(잊어버렸으면 /auth?mode=reset).
// AuthContent 가 useSearchParams() 를 사용하므로 Suspense 경계가 필수다.
export default function ChangePasswordPage() {
  return (
    <Suspense fallback={null}>
      <AuthContent change />
    </Suspense>
  );
}
