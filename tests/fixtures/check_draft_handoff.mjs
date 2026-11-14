import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { ChatRequestError, describeNewConversationError } from '../../web/src/components/chatFormat.ts'
import {
  applyTaskSuggestion,
  handoffNotice,
  prepareButtonLabel,
  prepareRetryDraft,
} from '../../web/src/components/taskDraftHandoff.ts'

const here = dirname(fileURLToPath(import.meta.url))
const fixture = JSON.parse(readFileSync(join(here, 'supervisor_draft_handoff.json'), 'utf8'))
const source = readFileSync(join(here, '../../web/src/components/taskDraftHandoff.ts'), 'utf8')

assert.equal(source.includes('fetch('), false, 'draft handoff must not call the network')
assert.equal(source.includes('/dispatch'), false, 'draft handoff must not dispatch')
assert.equal(source.includes('/api/supervisor/tasks'), false, 'draft handoff must not create a task')

for (const item of fixture.apply) {
  const applied = applyTaskSuggestion(item.suggestion)
  assert.equal(Object.hasOwn(applied, 'dispatch'), false, item.name)
  for (const [key, value] of Object.entries(item.expect)) {
    assert.deepEqual(applied[key], value, `${item.name} ${key}`)
  }
  assert.equal(prepareButtonLabel(item.suggestion), item.button, item.name)
  if (item.notice) {
    assert.equal(handoffNotice(applied.handoffMode, item.notice.narrow), item.notice.text, item.name)
  }
  if (item.expect.handoffMode === 'read_only') {
    assert.notEqual(applied.goal, '先不要执行、不要改代码', item.name)
  }
}

for (const item of fixture.retry) {
  assert.deepEqual(prepareRetryDraft(item.input), item.expect, item.name)
}

for (const item of fixture.newConversation) {
  const human = describeNewConversationError(new ChatRequestError(item.message, item.status))
  if (item.messageEquals) assert.equal(human.message, item.messageEquals, item.name)
  for (const part of item.includes || []) {
    assert.equal(human.message.includes(part), true, `${item.name} missing ${part}`)
  }
  for (const part of item.excludes || []) {
    assert.equal(human.message.includes(part), false, `${item.name} leaked ${part}`)
    assert.equal((human.detail || '').includes(part), false, `${item.name} detail leaked ${part}`)
  }
  for (const part of item.detailIncludes || []) {
    assert.equal((human.detail || '').includes(part), true, `${item.name} detail missing ${part}: ${human.detail}`)
  }
}

console.log(`draft handoff fixture ok (${fixture.apply.length} apply, ${fixture.retry.length} retry)`)
