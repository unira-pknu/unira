import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import "./globals.css";
import { QueryProvider } from "./context/QueryContext";
import { AuthProvider } from "./context/AuthContext";

export const metadata: Metadata = {
  title: "unira",
  description: "학과 문서와 학사 정보를 AI로 빠르게 찾아보세요.",
};

// themeColor: 모바일 상단 상태표시줄/주소창 색을 앱 배경(.bg-app 그라데이션 시작색)과 맞춘다.
// globals.css의 --clr-app-bg와 같은 값이어야 한다.
// maximumScale: 아이폰은 글자가 16px보다 작은 입력 칸을 누르면 화면을 저절로 확대한다(우리 칸은
// 모바일에서 약 11px). 아이폰은 maximum-scale=1 이 있어도 손가락 확대는 그대로 되고 자동 확대만
// 꺼진다. 안드로이드는 손가락 확대까지 막히므로 아이폰에만 붙인다(요청 기기를 봐야 해서 함수로).
export async function generateViewport(): Promise<Viewport> {
  const userAgent = (await headers()).get("user-agent") ?? "";
  const isIOS = /iPhone|iPad|iPod/.test(userAgent);
  return { themeColor: "#dde7f5", ...(isIOS ? { maximumScale: 1 } : {}) };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko" className="h-full">
      <body className="h-full antialiased" style={{ fontFamily: "'LaundryGothic', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif" }}>
        <AuthProvider>
          <QueryProvider>{children}</QueryProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
