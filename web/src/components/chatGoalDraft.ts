// 「项目」讨论里「设为目标」用的草稿：把老板最后一次提的要求和这条回复拼成一段文字，
// 填进「目标」页的「描述你想要的结果」。只是草稿——老板自己改完、自己点「开始」。

/** 和服务器对目标描述的上限一致（supervisor_goals 的 outcome 最多 4000 字）。 */
export const MAX_GOAL_DRAFT_CHARS = 4000

// 老板的要求最多占这么多字，剩下的留给回复。
const MAX_REQUEST_CHARS = 1500

const ELLIPSIS = '…'

// 按字符（码点）截断，不会把一个 emoji 劈成两半；服务器也按码点数 4000。
function clip(text: string, max: number): string {
  const chars = Array.from(text)
  if (chars.length <= max) return text
  if (max <= 0) return ''
  return `${chars.slice(0, Math.max(0, max - 1)).join('').trimEnd()}${ELLIPSIS}`
}

function charCount(text: string): number {
  return Array.from(text).length
}

/** 去掉 Markdown 代码块的围栏行（``` 或 ~~~，可带语言名），里面的内容保留。 */
export function stripCodeFences(text: string): string {
  return text
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .filter(line => !/^\s*(```|~~~)/.test(line))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/**
 * 拼出目标草稿：有老板的要求时写成「我的要求：…」+「讨论结论：…」，没有就只放这条回复。
 * 结果不超过 MAX_GOAL_DRAFT_CHARS 个字符，也不含代码块围栏。
 */
export function buildGoalDraft(request: string, reply: string): string {
  const cleanReply = stripCodeFences(reply)
  const cleanRequest = stripCodeFences(request)
  if (!cleanRequest) return clip(cleanReply, MAX_GOAL_DRAFT_CHARS)
  if (!cleanReply) return clip(`我的要求：\n${cleanRequest}`, MAX_GOAL_DRAFT_CHARS)

  const head = `我的要求：\n${clip(cleanRequest, MAX_REQUEST_CHARS)}\n\n讨论结论：\n`
  const room = MAX_GOAL_DRAFT_CHARS - charCount(head)
  return `${head}${clip(cleanReply, room)}`
}
