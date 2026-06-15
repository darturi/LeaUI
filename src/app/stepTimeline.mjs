function isAssistantStepMessage(message, terminalMessageId) {
  if (message.role !== 'assistant') return false;
  if (message.id === terminalMessageId) return false;
  if (message.is_live_terminal_summary) return false;
  // Conversational answers (explain/QA turns) are plain chat bubbles, not numbered
  // proof steps — they have no paired code snapshot.
  if (message.kind === 'chat') return false;
  return true;
}

function fallbackSummaryForCodeStep(step) {
  if (step.summary) return step.summary;
  if (step.kind === 'no_code') {
    return 'Lea completed this step without producing a readable Lean file update.';
  }
  return `Lea updated \`${step.path}\`. The current Lean snapshot is shown in the code pane.`;
}

function dedupeConsecutiveLogs(logs) {
  const deduped = [];
  for (const log of logs) {
    const previous = deduped[deduped.length - 1];
    const sameOperationalTool =
      previous &&
      previous.message === log.message &&
      previous.status === 'tool_called' &&
      log.status === 'tool_resulted';
    if (previous && previous.message === log.message && (previous.status === log.status || sameOperationalTool)) {
      continue;
    }
    deduped.push(log);
  }
  return deduped;
}

function eventTime(value) {
  const time = Date.parse(value || '');
  return Number.isFinite(time) ? time : Number.NaN;
}

function stepAnchorTime(stepItem) {
  const codeTime = eventTime(stepItem.codeStep?.created_at);
  if (Number.isFinite(codeTime)) return codeTime;
  return eventTime(stepItem.message?.created_at);
}

function isSetupEvent(event) {
  const status = String(event.status || '').toLowerCase();
  const message = String(event.message || '').toLowerCase();
  if (['submitted', 'running', 'api_run_started', 'stream_resume'].includes(status)) {
    return true;
  }
  return (
    message.includes('submitted theorem') ||
    message.includes('starting lea api run') ||
    message.includes('lea api run started') ||
    message.includes('resuming lea api stream')
  );
}

function attachOperationalLog(stepItems, event) {
  if (stepItems.length === 0 || isSetupEvent(event)) {
    return false;
  }

  const time = eventTime(event.created_at);
  if (!Number.isFinite(time)) {
    stepItems[stepItems.length - 1].logs.push(event);
    return true;
  }

  const target =
    stepItems.find((item) => {
      const anchor = stepAnchorTime(item);
      return Number.isFinite(anchor) && anchor >= time;
    }) || stepItems[stepItems.length - 1];
  target.logs.push(event);
  return true;
}

export function buildStepTimeline({ messages, codeSteps, statusEvents, terminalMessageId }) {
  const userAndSystemMessages = [];
  const terminalMessages = [];
  const assistantMessages = [];
  const timelineRunId =
    codeSteps.find((step) => step.run_id)?.run_id ||
    messages.find((message) => message.run_id)?.run_id ||
    statusEvents.find((event) => event.run_id)?.run_id ||
    'timeline';

  for (const message of messages) {
    if (message.id === terminalMessageId || message.is_live_terminal_summary) {
      terminalMessages.push(message);
    } else if (isAssistantStepMessage(message, terminalMessageId)) {
      assistantMessages.push(message);
    } else {
      userAndSystemMessages.push(message);
    }
  }

  const stepCount = Math.max(assistantMessages.length, codeSteps.length);
  const stepItems = Array.from({ length: stepCount }, (_, index) => ({
    id: codeSteps[index]?.id || assistantMessages[index]?.id || `${timelineRunId}:step-${index + 1}`,
    stepIndex: index,
    stepNumber: index + 1,
    message: assistantMessages[index],
    codeStep: codeSteps[index],
    createdAt: codeSteps[index]?.created_at || assistantMessages[index]?.created_at || null,
    logs: [],
  }));
  const globalLogs = [];

  for (const event of statusEvents) {
    const stepNumber = Number.isInteger(event.step_number) ? event.step_number : null;
    if (stepNumber && stepNumber > 0 && stepNumber <= stepItems.length) {
      stepItems[stepNumber - 1].logs.push(event);
    } else if (!attachOperationalLog(stepItems, event)) {
      globalLogs.push(event);
    }
  }

  for (const item of stepItems) {
    item.logs = dedupeConsecutiveLogs(item.logs);
  }

  return {
    userAndSystemMessages,
    stepItems,
    terminalMessages,
    globalLogs: dedupeConsecutiveLogs(globalLogs),
  };
}

export function codeStepFallbackContent(step) {
  return fallbackSummaryForCodeStep(step);
}

export function timelineStepCount({ messages, codeSteps, terminalMessageId }) {
  let assistantStepCount = 0;
  for (const message of messages) {
    if (isAssistantStepMessage(message, terminalMessageId)) {
      assistantStepCount += 1;
    }
  }
  return Math.max(assistantStepCount, codeSteps.length);
}
