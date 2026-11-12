/**
 * 자주 묻는 질문 칩에 보일 짧은 이름. 누르면 원래 질문 전체로 묻는다.
 * "휴학은 어디에서 어떤 순서로 신청하나요?" → "휴학 신청", "졸업 요건을 알려주세요" → "졸업 요건".
 * 주제어 + 핵심어(신청·기간·기준…)를 먼저 찾고, 없으면 말끝·조사를 떼고 짧게 자른다.
 */

// 학생들이 묻는 주제. 띄어쓰기는 무시하고 찾으며, 보일 때는 여기 적힌 모양 그대로. 긴 것부터 찾는다.
const TOPICS = [
  "졸업 요건", "졸업", "수강신청", "재수강", "휴학", "복학", "전과",
  "복수전공", "부전공", "계절학기", "계절수업", "학사경고", "성적", "장학금", "등록금", "기숙사",
  "생활관", "도서관", "학생증", "증명서", "졸업인증", "학점교류", "교환학생", "강의계획서",
  "학식", "식단", "셔틀", "주차", "포털", "LMS",
].sort((a, b) => b.replace(/\s+/g, "").length - a.replace(/\s+/g, "").length);

// 무엇이 궁금한지(질문에 나온 순서대로 최대 2개).
const ASPECTS = ["운영", "변경", "신청", "취소", "기간", "일정", "기준", "조건", "자격", "요건", "방법", "절차", "시간", "위치", "비용", "주소", "연락처"];

const ENDINGS = /\s*(을|를|은|는|이|가)?\s*(알려\s*주세요|알려\s*줘|알려\s*주실래요|궁금해요|궁금합니다|뭐야|뭐예요|뭔가요|어떻게\s*(돼|되나요|해|하나요)|언제(야|예요|인가요|였나요)?|어디(야|예요|에요|인가요)?|있나요|인가요|되나요|하나요|나요|가요)\s*[.?!~]*$/u;
const PARTICLE = /(은|는|이|가|을|를|의|에|에서|으로|로)$/u;
const MAX_CHARS = 12;

export function shortLabel(question: string): string {
  const text = question.trim().replace(/[.?!~。？！]+$/u, "");
  const compact = text.replace(/\s+/g, "");
  const topic = TOPICS.find((t) => compact.includes(t.replace(/\s+/g, "")));
  if (topic) {
    const aspects = ASPECTS
      .filter((a) => compact.includes(a) && !topic.replace(/\s+/g, "").includes(a))
      .sort((a, b) => compact.indexOf(a) - compact.indexOf(b))
      .slice(0, 2);
    return [topic, ...aspects].join(" ");
  }
  // 주제어가 없으면: 말끝을 떼고, 남은 단어의 조사를 떼고, 짧게.
  const words = text.replace(ENDINGS, "").split(/\s+/).filter(Boolean).map((w) => w.replace(PARTICLE, "") || w);
  let label = "";
  for (const word of words) {
    const next = label ? `${label} ${word}` : word;
    if (next.length > MAX_CHARS) break;
    label = next;
  }
  return label || text.slice(0, MAX_CHARS);
}
