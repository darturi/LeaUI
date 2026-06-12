import assert from 'node:assert/strict';
import test from 'node:test';

import { buildStepTimeline } from './stepTimeline.mjs';
import { timelineIndexForTarget, timelineItemMatchesTarget } from './timelineTarget.mjs';

const baseMessage = {
  session_id: 'session-1',
  run_id: 'run-1',
  created_at: '2026-06-05T19:00:00.000Z',
};

const baseStep = {
  session_id: 'session-1',
  run_id: 'run-1',
  path: 'workspace/proofs/demo.lean',
  code: 'theorem demo : True := by\n  trivial',
  kind: 'code',
  created_at: '2026-06-05T19:00:01.000Z',
};

function assistant(id, content, extra = {}) {
  return {
    ...baseMessage,
    id,
    role: 'assistant',
    content,
    ...extra,
  };
}

function user(id, content) {
  return {
    ...baseMessage,
    id,
    role: 'user',
    content,
  };
}

function codeStep(id, stepNumber, extra = {}) {
  return {
    ...baseStep,
    id,
    step_number: stepNumber,
    ...extra,
  };
}

function statusEvent(id, message, stepNumber = null, extra = {}) {
  return {
    id,
    session_id: 'session-1',
    run_id: 'run-1',
    step_number: stepNumber,
    status: stepNumber ? 'code_step' : 'running',
    message,
    created_at: '2026-06-05T19:00:02.000Z',
    ...extra,
  };
}

test('live assistant text becomes a provisional step immediately', () => {
  const timeline = buildStepTimeline({
    messages: [
      user('user-1', 'Prove this'),
      assistant('live-run-1', 'We can prove this by induction.', {
        live_started_after_assistant_steps: 0,
        live_started_after_code_steps: 0,
      }),
    ],
    codeSteps: [],
    statusEvents: [],
    terminalMessageId: null,
  });

  assert.equal(timeline.stepItems.length, 1);
  assert.equal(timeline.stepItems[0].stepNumber, 1);
  assert.equal(timeline.stepItems[0].message.content, 'We can prove this by induction.');
});

test('final terminal assistant message does not become a phantom numbered step', () => {
  const timeline = buildStepTimeline({
    messages: [
      user('user-1', 'Prove this'),
      assistant('assistant-1', 'First, set up the induction.'),
      assistant('assistant-2', 'The proof is complete.'),
    ],
    codeSteps: [codeStep('code-1', 1)],
    statusEvents: [],
    terminalMessageId: 'assistant-2',
  });

  assert.equal(timeline.stepItems.length, 1);
  assert.equal(timeline.stepItems[0].message.id, 'assistant-1');
  assert.deepEqual(timeline.terminalMessages.map((message) => message.id), ['assistant-2']);
});

test('code-only updates render as fallback steps after narrated steps', () => {
  const timeline = buildStepTimeline({
    messages: [user('user-1', 'Prove this'), assistant('assistant-1', 'I wrote the first version.')],
    codeSteps: [codeStep('code-1', 1), codeStep('code-2', 2)],
    statusEvents: [],
    terminalMessageId: null,
  });

  assert.equal(timeline.stepItems.length, 2);
  assert.equal(timeline.stepItems[0].message.id, 'assistant-1');
  assert.equal(timeline.stepItems[1].message, undefined);
  assert.equal(timeline.stepItems[1].codeStep.id, 'code-2');
});

test('status events attach to matching steps and global setup logs stay separate', () => {
  const timeline = buildStepTimeline({
    messages: [user('user-1', 'Prove this'), assistant('assistant-1', 'I wrote the file.')],
    codeSteps: [codeStep('code-1', 1)],
    statusEvents: [
      statusEvent('status-setup', 'Starting Lea API run'),
      statusEvent('status-step', 'Captured Lean file update', 1),
    ],
    terminalMessageId: null,
  });

  assert.deepEqual(timeline.globalLogs.map((log) => log.id), ['status-setup']);
  assert.deepEqual(timeline.stepItems[0].logs.map((log) => log.id), ['status-step']);
});

test('consecutive duplicate status events are collapsed per step', () => {
  const timeline = buildStepTimeline({
    messages: [assistant('assistant-1', 'I wrote the file.')],
    codeSteps: [codeStep('code-1', 1)],
    statusEvents: [
      statusEvent('status-1', 'write_file', 1),
      statusEvent('status-2', 'write_file', 1),
      statusEvent('status-3', 'lean_check', 1),
    ],
    terminalMessageId: null,
  });

  assert.deepEqual(
    timeline.stepItems[0].logs.map((log) => log.message),
    ['write_file', 'lean_check'],
  );
});

test('tool call and result pair is displayed as one operational log', () => {
  const timeline = buildStepTimeline({
    messages: [assistant('assistant-1', 'I checked the file.')],
    codeSteps: [codeStep('code-1', 1)],
    statusEvents: [
      statusEvent('status-1', 'lean_check', 1, { status: 'tool_called' }),
      statusEvent('status-2', 'lean_check', 1, { status: 'tool_resulted' }),
    ],
    terminalMessageId: null,
  });

  assert.deepEqual(timeline.stepItems[0].logs.map((log) => log.id), ['status-1']);
});

test('unnumbered operational logs attach to the nearest step instead of setup', () => {
  const timeline = buildStepTimeline({
    messages: [assistant('assistant-1', 'I wrote the file.')],
    codeSteps: [
      codeStep('code-1', 1, {
        created_at: '2026-06-05T19:00:05.000Z',
      }),
    ],
    statusEvents: [
      statusEvent('status-setup', 'Starting Lea API run', null, {
        status: 'running',
        created_at: '2026-06-05T19:00:00.000Z',
      }),
      statusEvent('status-tool', 'lean_check', null, {
        status: 'tool_call',
        created_at: '2026-06-05T19:00:04.000Z',
      }),
    ],
    terminalMessageId: null,
  });

  assert.deepEqual(timeline.globalLogs.map((log) => log.id), ['status-setup']);
  assert.deepEqual(timeline.stepItems[0].logs.map((log) => log.id), ['status-tool']);
});

test('code step target stays active when narration arrives later', () => {
  const codeOnlyTimeline = buildStepTimeline({
    messages: [user('user-1', 'Prove this')],
    codeSteps: [codeStep('code-1', 1)],
    statusEvents: [],
    terminalMessageId: null,
  });
  const target = { runId: 'run-1', codeStepId: 'code-1' };
  assert.equal(timelineItemMatchesTarget(codeOnlyTimeline.stepItems[0], target), true);

  const narratedTimeline = buildStepTimeline({
    messages: [user('user-1', 'Prove this'), assistant('assistant-1', 'I wrote the Lean proof.')],
    codeSteps: [codeStep('code-1', 1)],
    statusEvents: [],
    terminalMessageId: null,
  });
  const sections = [{ id: 'run-1', attemptNumber: 1, stepOffset: 0, timeline: narratedTimeline }];

  assert.equal(timelineIndexForTarget(sections, target), 0);
  assert.equal(timelineItemMatchesTarget(narratedTimeline.stepItems[0], target), true);
});

test('live assistant replacement can retarget to persisted assistant message', () => {
  const liveTimeline = buildStepTimeline({
    messages: [
      user('user-1', 'Prove this'),
      assistant('live-run-1', 'I will first simplify the goal.', {
        live_started_after_assistant_steps: 0,
        live_started_after_code_steps: 0,
      }),
    ],
    codeSteps: [],
    statusEvents: [],
    terminalMessageId: null,
  });

  assert.equal(
    timelineItemMatchesTarget(liveTimeline.stepItems[0], { runId: 'run-1', messageId: 'live-run-1' }),
    true,
  );

  const persistedTimeline = buildStepTimeline({
    messages: [
      user('user-1', 'Prove this'),
      assistant('assistant-1', 'I will first simplify the goal.', {
        live_started_after_assistant_steps: 0,
        live_started_after_code_steps: 0,
      }),
    ],
    codeSteps: [],
    statusEvents: [],
    terminalMessageId: null,
  });
  const sections = [{ id: 'run-1', attemptNumber: 1, stepOffset: 0, timeline: persistedTimeline }];

  assert.equal(timelineIndexForTarget(sections, { runId: 'run-1', messageId: 'assistant-1' }), 0);
});

test('interleaved code targets resolve to their own step instead of a previous step', () => {
  const timeline = buildStepTimeline({
    messages: [
      user('user-1', 'Prove this'),
      assistant('assistant-1', 'First attempt.'),
      assistant('assistant-2', 'Second attempt.'),
    ],
    codeSteps: [
      codeStep('code-1', 1, { created_at: '2026-06-05T19:00:03.000Z', turn: 1 }),
      codeStep('code-2', 2, { created_at: '2026-06-05T19:00:02.000Z', turn: 2 }),
    ],
    statusEvents: [
      statusEvent('status-2', 'Captured second update', 2, {
        created_at: '2026-06-05T19:00:04.000Z',
      }),
      statusEvent('status-1', 'Captured first update', 1, {
        created_at: '2026-06-05T19:00:05.000Z',
      }),
    ],
    terminalMessageId: null,
  });
  const sections = [{ id: 'run-1', attemptNumber: 1, stepOffset: 0, timeline }];

  assert.equal(timelineIndexForTarget(sections, { runId: 'run-1', codeStepId: 'code-2' }), 1);
  assert.equal(
    timelineItemMatchesTarget(timeline.stepItems[0], { runId: 'run-1', codeStepId: 'code-2' }),
    false,
  );
});
