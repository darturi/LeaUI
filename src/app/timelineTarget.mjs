export function timelineItemMatchesTarget(item, target) {
  if (!item || !target) {
    return false;
  }
  if (target.runId) {
    const itemRunId = item.codeStep?.run_id || item.message?.run_id;
    if (itemRunId && itemRunId !== target.runId) {
      return false;
    }
  }
  if (target.codeStepId && item.codeStep?.id === target.codeStepId) {
    return true;
  }
  if (target.messageId && item.message?.id === target.messageId) {
    return true;
  }
  if (target.provisionalKey && item.id === target.provisionalKey) {
    return true;
  }
  return false;
}

export function timelineIndexForTarget(sections, target) {
  if (!target) {
    return null;
  }
  for (const section of sections) {
    if (target.runId && section.id !== target.runId) {
      continue;
    }
    const itemIndex = section.timeline.stepItems.findIndex((item) =>
      timelineItemMatchesTarget(item, target),
    );
    if (itemIndex >= 0) {
      return section.stepOffset + itemIndex;
    }
  }
  return null;
}
