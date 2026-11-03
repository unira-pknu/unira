import { Suspense } from "react";
import AuthContent from "../AuthContent";

// 비밀번호 재설정 메일의 링크 → /auth/callback(세션 교환) → 여기서 새 비밀번호를 정한다.
// AuthContent 가 useSearchParams() 를 사용하므로 Suspense 경계가 필수다.
export default function ResetPasswordPage() {
  return (
    <Suspense fallback={null}>
      <AuthContent update />
    </Suspense>
  );
}
