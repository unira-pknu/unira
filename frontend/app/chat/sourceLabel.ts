/**
 * 출처 카드에 붙는 종류 표시(학교 공지사항·학칙·학과 홈페이지 …).
 *
 * 전에는 모든 카드가 "학과 홈페이지 · 자료"로 나와 학교 공지도 학과 글처럼 보였다. 백엔드가 출처
 * 종류를 따로 주지 않지만 수집처마다 주소 모양이 정해져 있어 주소로 가른다. 저장된 옛 대화의
 * 출처에도 그대로 쓸 수 있다.
 */
export function sourceKindLabel(url: string): string {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "자료";
  }
  const host = parsed.hostname.toLowerCase();
  const path = parsed.pathname;
  if (host === "www.pknu.ac.kr" || host === "pknu.ac.kr") {
    if (path.startsWith("/main/163")) return "학교 공지사항";
    if (path.startsWith("/rule/")) return "학칙·규정";
    return "학교 홈페이지";
  }
  // 학과·단과대학 사이트는 학교 도메인의 하위 주소다(ce.pknu.ac.kr 등).
  if (host.endsWith(".pknu.ac.kr")) return "학과 홈페이지";
  return "자료";
}
