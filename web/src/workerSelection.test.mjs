import assert from 'node:assert/strict'
import test from 'node:test'
import { canonicalWorkerSessionId, parseWorkerSessionMap } from './workerSelection.js'

test('localStorage JSON must be a plain string map', () => {
  assert.deepEqual(parseWorkerSessionMap(null), {})
  assert.deepEqual(parseWorkerSessionMap('null'), {})
  assert.deepEqual(parseWorkerSessionMap('[]'), {})
  assert.deepEqual(parseWorkerSessionMap('"nope"'), {})
  assert.deepEqual(parseWorkerSessionMap('{'), {})
  assert.deepEqual(parseWorkerSessionMap('{"a": 1, "b": null, "c": "sess"}'), { c: 'sess' })
})

test('one canonical session per agent, remembered over primary', () => {
  const agent = {
    id: 'alpha',
    session_id: 'primary',
    sessions: [
      { id: 'primary', kind: 'main' },
      { id: 'secondary', kind: 'created' },
    ],
  }
  assert.equal(canonicalWorkerSessionId(agent, { alpha: 'secondary' }), 'secondary')
  assert.equal(canonicalWorkerSessionId(agent, { alpha: 'missing' }), 'primary')
  assert.equal(canonicalWorkerSessionId(agent, {}), 'primary')
  assert.equal(canonicalWorkerSessionId({ id: 'beta', session_id: 'only' }, {}), 'only')
  assert.equal(
    canonicalWorkerSessionId(agent, { alpha: 'brand-new' }, 'brand-new'),
    'brand-new',
  )
  assert.equal(canonicalWorkerSessionId(agent, { alpha: 'brand-new' }), 'primary')
})
