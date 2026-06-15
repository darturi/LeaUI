import { useMemo } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { CodeStep } from '../api';
import { diffForStep } from '../codeDiff';
import { projectTagClass } from '../projectTags.js';
import type { RunTimelineSection } from '../runAttempts';

export function CodeViewer({
  codeSteps,
  runTimelineSections,
  timelineStepCount,
  isPaused,
  isRunning,
  currentStepIndex,
  onStepChange,
}: {
  codeSteps: CodeStep[];
  runTimelineSections: RunTimelineSection[];
  timelineStepCount: number;
  isPaused: boolean;
  isRunning: boolean;
  currentStepIndex: number;
  onStepChange: (index: number) => void;
}) {
  const totalSteps = Math.max(timelineStepCount, codeSteps.length);
  const safeIndex = Math.min(Math.max(currentStepIndex, 0), Math.max(totalSteps - 1, 0));
  const flattenedTimelineItems = runTimelineSections.flatMap((section) =>
    section.timeline.stepItems.map((item) => ({
      section,
      item,
      globalIndex: section.stepOffset + item.stepIndex,
    })),
  );
  const currentTimelineItem = flattenedTimelineItems.find((entry) => entry.globalIndex === safeIndex);
  const exactCodeStep = currentTimelineItem?.item.codeStep;
  const currentStep =
    exactCodeStep ||
    [...flattenedTimelineItems]
      .reverse()
      .find((entry) => entry.globalIndex <= safeIndex && entry.item.codeStep)
      ?.item.codeStep;
  const currentCodeStepIndex = currentStep
    ? codeSteps.findIndex((step) => step.id === currentStep.id)
    : -1;
  const hasCodeChangeForTimelineStep = !!exactCodeStep;
  const currentSection =
    currentTimelineItem?.section ||
    runTimelineSections.find((section) => {
      const stepCount = section.timeline.stepItems.length;
      return safeIndex >= section.stepOffset && safeIndex < section.stepOffset + stepCount;
    }) ||
    (currentStep
      ? runTimelineSections.find((section) => section.id === currentStep.run_id)
      : undefined);
  const currentSectionStepCount = currentSection?.timeline.stepItems.length ?? 0;
  const currentSectionStepIndex =
    currentSection && currentSectionStepCount > 0
      ? Math.min(
          Math.max(safeIndex - currentSection.stepOffset, 0),
          currentSectionStepCount - 1,
        )
      : -1;
  const stepLabel =
    currentSection?.attemptNumber && currentSectionStepIndex >= 0
      ? `Attempt ${currentSection.attemptNumber} · Step ${currentSectionStepIndex + 1} of ${currentSectionStepCount}`
      : `Step ${safeIndex + 1} of ${totalSteps}`;

  const diffedLines = useMemo(() => {
    if (!currentStep) {
      return [];
    }
    if (!hasCodeChangeForTimelineStep) {
      return currentStep.code
        .split('\n')
        .filter((_, index, lines) => !(index === lines.length - 1 && lines[index] === ''))
        .map((line, index) => ({
          kind: 'unchanged' as const,
          line,
          oldLineNumber: index + 1,
          newLineNumber: index + 1,
        }));
    }
    return diffForStep(codeSteps, currentCodeStepIndex);
  }, [codeSteps, currentStep, currentCodeStepIndex, hasCodeChangeForTimelineStep]);
  const usedProjectFormalizations = currentStep?.used_project_formalizations || [];
  const usedByProjectFormalizations = currentStep?.used_by_project_formalizations || [];

  const handlePrevious = () => {
    if (safeIndex > 0) {
      onStepChange(safeIndex - 1);
    }
  };

  const handleNext = () => {
    if (safeIndex < totalSteps - 1) {
      onStepChange(safeIndex + 1);
    }
  };

  return (
    <div className="flex flex-col h-full bg-background border-l border-border">
      <div className="p-4 border-b border-border">
        <div className="flex items-center justify-between gap-3">
          <h2 className="shrink-0 text-foreground">Lean Code</h2>
          {totalSteps > 0 && (
            <div className="flex shrink-0 items-center gap-2">
              <button
                onClick={handlePrevious}
                disabled={safeIndex === 0}
                className="p-1 rounded hover:bg-accent disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <ChevronLeft className="w-5 h-5" />
              </button>
              <span className="text-sm text-muted-foreground whitespace-nowrap">
                {stepLabel}
              </span>
              <button
                onClick={handleNext}
                disabled={safeIndex === totalSteps - 1}
                className="p-1 rounded hover:bg-accent disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <ChevronRight className="w-5 h-5" />
              </button>
            </div>
          )}
        </div>
        {currentStep && (
          <div className="mt-2 space-y-1">
            <p className="break-all text-sm text-muted-foreground">
              {currentStep.turn ? `Turn ${currentStep.turn} · ` : ''}
              {currentStep.path}
            </p>
            {currentStep.summary && (
              <p className="text-xs text-muted-foreground">
                {currentStep.summary}
              </p>
            )}
            {currentStep && !hasCodeChangeForTimelineStep && (
              <p className="text-xs text-muted-foreground">
                No Lean file changes were captured for this step; showing the latest available snapshot.
              </p>
            )}
          </div>
        )}
      </div>

      <div className="flex-1 overflow-auto p-4">
        {!currentStep ? (
          <div className="h-full flex items-center justify-center text-center text-muted-foreground">
            Lean code will appear here as Lea edits files.
          </div>
        ) : (
          <div className="space-y-3">
            {(currentStep.kind === 'no_code' || !hasCodeChangeForTimelineStep) && (
              <div className="rounded-md border border-border bg-accent px-3 py-2 text-sm text-accent-foreground">
                {currentStep.kind === 'no_code'
                  ? currentStep.summary || 'No Lean file changes in this step.'
                  : 'No Lean file changes in this step.'}
              </div>
            )}
            {diffedLines.length === 0 ? (
              <div className="rounded-md bg-muted p-4 text-sm text-muted-foreground">
                No Lean code has been written yet.
              </div>
            ) : (
              <pre className="overflow-x-hidden rounded-md bg-muted p-2 font-mono text-sm">
                <code>
                  {diffedLines.map((item, index) => (
                    <div
                      key={index}
                      className={`grid grid-cols-[1ch_3ch_minmax(0,1fr)] gap-x-2 px-1 ${
                        item.kind === 'added'
                          ? 'bg-green-500/20 text-foreground'
                          : item.kind === 'removed'
                          ? 'bg-red-500/15 text-foreground'
                          : 'text-foreground'
                      }`}
                    >
                      <span className={item.kind === 'added' ? 'text-green-700' : item.kind === 'removed' ? 'text-red-700' : 'text-muted-foreground'}>
                        {item.kind === 'added' ? '+' : item.kind === 'removed' ? '-' : ' '}
                      </span>
                      <span className="text-right text-muted-foreground select-none">
                        {item.kind === 'removed' ? item.oldLineNumber ?? '' : item.newLineNumber ?? ''}
                      </span>
                      <span className="whitespace-pre-wrap break-words">{item.line || ' '}</span>
                    </div>
                  ))}
                </code>
              </pre>
            )}
            {usedProjectFormalizations.length > 0 && (
              <div className="rounded-md border border-border bg-accent px-3 py-2 text-sm text-accent-foreground">
                <div className="font-medium">
                  {usedProjectFormalizations.length === 1
                    ? 'User-defined formalization used'
                    : 'User-defined formalizations used'}
                </div>
                <div className="mt-1 space-y-1 text-xs leading-relaxed">
                  {usedProjectFormalizations.map((item) => {
                    const projectTitle = item.project_title || item.project_slug || 'project';
                    return (
                      <p key={`${item.name}:${item.proof_path}`} className="break-words">
                        Theorem <code className="font-mono">{item.name}</code> at{' '}
                        <code className="font-mono">{item.proof_path}</code> from project{' '}
                        <span
                          className={`inline-flex max-w-full items-center rounded-full border px-2 py-0.5 font-medium ${projectTagClass(projectTitle)}`}
                          title={projectTitle}
                        >
                          <span className="truncate">{projectTitle}</span>
                        </span>{' '}
                        was used in this formalization.
                      </p>
                    );
                  })}
                </div>
              </div>
            )}
            {usedByProjectFormalizations.length > 0 && (
              <div className="rounded-md border border-border bg-accent px-3 py-2 text-sm text-accent-foreground">
                <div className="font-medium">
                  {usedByProjectFormalizations.length === 1
                    ? 'Used by another project formalization'
                    : 'Used by other project formalizations'}
                </div>
                <div className="mt-1 space-y-1 text-xs leading-relaxed">
                  {usedByProjectFormalizations.map((item) => {
                    const projectTitle = item.project_title || item.project_slug || 'project';
                    return (
                      <p key={`${item.name}:${item.proof_path}`} className="break-words">
                        Used by theorem <code className="font-mono">{item.name}</code> at{' '}
                        <code className="font-mono">{item.proof_path}</code> as part of project{' '}
                        <span
                          className={`inline-flex max-w-full items-center rounded-full border px-2 py-0.5 font-medium ${projectTagClass(projectTitle)}`}
                          title={projectTitle}
                        >
                          <span className="truncate">{projectTitle}</span>
                        </span>
                        .
                      </p>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {!isPaused && currentStep && safeIndex === totalSteps - 1 && (
        <div className="p-4 border-t border-border bg-accent">
          <p className="text-sm text-accent-foreground">
            {isRunning
              ? 'Lea is still working. Use the arrows to review available steps.'
              : 'Showing latest code. Use the arrows to review earlier steps.'}
          </p>
        </div>
      )}
    </div>
  );
}
