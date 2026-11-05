import { Suspense } from "react";
import ConfirmContent from "./ConfirmContent";

// 인증 메일 링크 → 여기서 버튼을 눌러야 토큰을 쓴다(메일 보안 검사의 미리 열기 대비).
// ConfirmContent 가 useSearchParams() 를 사용하므로 Suspense 경계가 필수다.
export default function ConfirmPage() {
  return (
    <Suspense fallback={null}>
      <ConfirmContent />
    </Suspense>
  );
}
