/**
 * 답변 아래 출처를 정리해 보여 준다: 답변에 인용된 것만, 같은 공지(같은 URL)는 하나로.
 *
 * - 백엔드는 검색한 조각을 최대 10개 돌려주지만 답변이 인용하는 건 그중 몇 개다. 인용하지 않은
 *   출처까지 ①–⑩로 다 보여 주면 답과 상관없는 자료가 섞여 보인다. 답변에 [n]이
 *   하나도 없으면 고를 근거가 없으니 전부 보여 준다.
 * - 긴 공지는 여러 조각으로 저장돼, 한 공지의 두 조각이 [1]·[2]로 따로 온다. 출처 카드는
 *   제목·링크·첨부만 보여 주므로(발췌는 길어서 뺐다) 똑같은 카드가 두 장 보였다(10-14 점검:
 *   답변의 92%). 같은 URL은 하나로 합친다.
 *
 * 남긴 출처는 답변에 처음 나오는 순서로 1부터 다시 매기고 답변 속 [n]도 같은 번호로 바꾼다.
 * 화면 표시용이라 저장된 답변·출처는 그대로다.
 */

type DisplaySource = {
  id: number;
  title: string;
  url: string;
  attachments: { name: string; url: string }[];
};

function sourceKey(source: DisplaySource): string {
  const url = source.url.trim();
  // 링크가 없는 출처("#")는 제목으로 묶는다.
  return url && url !== "#" ? url : `title:${source.title.trim()}`;
}

const MARKER_RE = /\[(\d+)\]/g;

export function prepareSources<S extends DisplaySource>(
  content: string,
  sources: S[],
): { content: string; sources: S[] } {
  // 1) 같은 URL끼리 묶는다(처음 나온 순서 유지).
  const groups: { sources: S[]; oldIds: number[] }[] = [];
  const groupByKey = new Map<string, number>();
  for (const source of sources) {
    const key = sourceKey(source);
    const index = groupByKey.get(key);
    if (index === undefined) {
      groupByKey.set(key, groups.length);
      groups.push({ sources: [source], oldIds: [source.id] });
    } else {
      groups[index].sources.push(source);
      groups[index].oldIds.push(source.id);
    }
  }

  // 2) 답변이 인용한 묶음만 남긴다(인용이 하나도 없으면 전부).
  // 번호는 답변에 처음 나오는 순서대로 매겨, 답변이 [1]부터 읽히게 한다.
  const knownIds = new Set(sources.map((s) => s.id));
  const citationOrder = [...content.matchAll(MARKER_RE)]
    .map((m) => Number(m[1]))
    .filter((id) => knownIds.has(id));
  const firstCited = (group: { oldIds: number[] }) =>
    Math.min(...group.oldIds.map((id) => citationOrder.indexOf(id)).filter((i) => i >= 0));
  const kept = citationOrder.length
    ? groups.filter((g) => g.oldIds.some((id) => citationOrder.includes(id))).sort((a, b) => firstCited(a) - firstCited(b))
    : groups;

  // 3) 다시 번호 매기고 첨부를 모은다(같은 파일은 한 번만).
  const newIdByOldId = new Map<number, number>();
  const shown = kept.map((group, index) => {
    const [first, ...rest] = group.sources;
    const attachments = [...first.attachments];
    for (const attachment of rest.flatMap((s) => s.attachments)) {
      if (!attachments.some((a) => a.url === attachment.url)) attachments.push(attachment);
    }
    for (const oldId of group.oldIds) newIdByOldId.set(oldId, index + 1);
    return { ...first, id: index + 1, attachments };
  });

  const renumbered = content
    .replace(MARKER_RE, (marker, digits: string) => {
      const next = newIdByOldId.get(Number(digits));
      return next === undefined ? marker : `[${next}]`;
    })
    // 합친 뒤 붙어 있는 번호 묶음에 같은 번호가 또 나오면("[1][1]", "[1][2][1]") 한 번만 남긴다.
    .replace(/\[\d+\](?:\s*\[\d+\])+/g, (run) =>
      [...new Set(run.match(/\[\d+\]/g))].join(""),
    );

  return { content: renumbered, sources: shown };
}
