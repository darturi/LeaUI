import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Send, Pause, Play, BarChart3, Loader2, Settings, RotateCcw, FolderPlus, Unlink, Link2, ArrowDown } from 'lucide-react';
import { ApprovalDecision, ApprovalEvent, ChatMessage, CodeStep, PendingApproval, Project, ProjectTheoremEntry, SafeVerifyResult, SessionStatus, StatusEvent } from '../api';
import { codeStepFallbackContent } from '../stepTimeline.mjs';
import { timelineItemMatchesTarget } from '../timelineTarget.mjs';
import { buildRunTimelineSections } from '../runAttempts';
import { MarkdownMessage } from './MarkdownMessage';
import { SafeVerifyBadge } from './SafeVerifyBadge';
import { TheoremApprovalPanel } from './TheoremApprovalPanel';

type ActiveTimelineTarget = {
  runId?: string;
  codeStepId?: string;
  messageId?: string;
  provisionalKey?: string;
} | null;

const BOTTOM_SCROLL_THRESHOLD_PX = 48;

export function ChatInterface({
  sessionId,
  error,
  isPaused,
  isRunning,
  messages,
  codeSteps,
  statusEvents,
  approvalEvents,
  input,
  sessionStatus,
  onSubmit,
  onInputChange,
  onStepSelect,
  onTogglePause,
  onOpenStats,
  onOpenSettings,
  onRequestProjectAssignment,
  onRequestProjectUnassignment,
  onSubmitApproval,
  onRetry,
  theoremName,
  currentStepIndex,
  activeTimelineTarget,
  pendingApproval,
  isSubmittingApproval,
  approvalError,
  projectTheorem,
  isProjectAssociated,
  isAssigningProject,
  canUnassignProjectTheorem,
  unassignmentDisabledReason,
  isUnassigningProject,
  projects,
  selectedProjectId,
  onProjectChange,
  onCreateProject,
  safeVerify,
}: {
  sessionId?: string;
  error?: string;
  isPaused: boolean;
  isRunning: boolean;
  safeVerify?: SafeVerifyResult | null;
  pendingApproval?: PendingApproval;
  isSubmittingApproval: boolean;
  approvalError?: string;
  projects: Project[];
  selectedProjectId?: string;
  onProjectChange: (projectId: string | undefined) => void;
  onCreateProject: (title: string) => Promise<Project>;
  messages: ChatMessage[];
  codeSteps: CodeStep[];
  sessionStatus?: SessionStatus;
  statusEvents: StatusEvent[];
  approvalEvents: ApprovalEvent[];
  input: string;
  onSubmit: (content: string) => Promise<boolean>;
  onRetry: (content: string) => Promise<boolean>;
  onInputChange: (content: string) => void;
  onSubmitApproval: (decision: ApprovalDecision, feedback?: string) => Promise<void>;
  onRequestProjectAssignment: () => Promise<void>;
  onRequestProjectUnassignment: () => Promise<void>;
  onStepSelect: (stepIndex: number) => void;
  onTogglePause: () => void;
  onOpenStats: () => void;
  onOpenSettings: () => void;
  theoremName: string;
  projectTheorem?: ProjectTheoremEntry;
  isProjectAssociated: boolean;
  isAssigningProject: boolean;
  canUnassignProjectTheorem: boolean;
  unassignmentDisabledReason?: string;
  isUnassigningProject: boolean;
  currentStepIndex: number;
  activeTimelineTarget: ActiveTimelineTarget;
}) {
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const bottomAnchorRef = useRef<HTMLDivElement | null>(null);
  const [isAutoScrollEnabled, setIsAutoScrollEnabled] = useState(true);
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);
  const [newProjectTitle, setNewProjectTitle] = useState('');
  const [isCreatingProject, setIsCreatingProject] = useState(false);
  const terminalMessageId = useMemo(() => {
    if (isRunning || !sessionStatus || sessionStatus === 'running') {
      return null;
    }
    const terminalMessage = [...messages]
      .reverse()
      .find(
        (message) =>
          (message.role === 'assistant' || message.role === 'system') && message.kind !== 'chat',
      );
    return terminalMessage?.id ?? null;
  }, [isRunning, messages, sessionStatus]);
  const runSections = useMemo(() => {
    return buildRunTimelineSections({
      messages,
      codeSteps,
      statusEvents,
      approvalEvents,
      pendingApproval,
      terminalMessageId,
    }).map((section) => {
      const runApprovals = approvalEvents.filter(
        (approval) => approval.run_id === section.id && approval.approval_id !== pendingApproval?.approval_id,
      );
      return {
        ...section,
        approvals: runApprovals,
        pendingApproval: pendingApproval?.run_id === section.id ? pendingApproval : undefined,
      };
    });
  }, [messages, codeSteps, statusEvents, approvalEvents, pendingApproval, terminalMessageId]);
  const lastUserMessage = useMemo(
    () => [...messages].reverse().find((message) => message.role === 'user'),
    [messages],
  );
  const canRetry =
    Boolean(lastUserMessage) &&
    !isRunning &&
    !isSubmitting &&
    !isRetrying &&
    (sessionStatus === 'failed' || sessionStatus === 'max_turns' || sessionStatus === 'max_spend');

  const isNearBottom = useCallback((element: HTMLDivElement) => {
    return (
      element.scrollHeight - element.scrollTop - element.clientHeight <=
      BOTTOM_SCROLL_THRESHOLD_PX
    );
  }, []);

  const scrollToBottom = useCallback(() => {
    const container = scrollContainerRef.current;
    if (!container) {
      return;
    }
    container.scrollTo({
      top: container.scrollHeight,
      behavior: 'auto',
    });
  }, []);

  const handleChatScroll = useCallback(() => {
    const container = scrollContainerRef.current;
    if (!container) {
      return;
    }
    const atBottom = isNearBottom(container);
    setIsAutoScrollEnabled(atBottom);
    setShowScrollToBottom(!atBottom);
  }, [isNearBottom]);

  const resumeAutoScroll = useCallback(() => {
    setIsAutoScrollEnabled(true);
    setShowScrollToBottom(false);
    window.requestAnimationFrame(scrollToBottom);
  }, [scrollToBottom]);

  useEffect(() => {
    setIsAutoScrollEnabled(true);
    setShowScrollToBottom(false);
    const frame = window.requestAnimationFrame(scrollToBottom);
    return () => window.cancelAnimationFrame(frame);
  }, [sessionId, scrollToBottom]);

  useEffect(() => {
    if (!isAutoScrollEnabled) {
      return;
    }
    const frame = window.requestAnimationFrame(scrollToBottom);
    return () => window.cancelAnimationFrame(frame);
  }, [
    isAutoScrollEnabled,
    scrollToBottom,
    messages,
    codeSteps,
    statusEvents,
    approvalEvents,
    pendingApproval,
    error,
    isSubmitting,
    runSections,
  ]);

  const submitInput = async () => {
    const content = input.trim();
    if (!content || isRunning || isSubmitting || isRetrying) {
      return;
    }
    setIsSubmitting(true);
    const succeeded = await onSubmit(content);
    if (succeeded) {
      onInputChange('');
    }
    setIsSubmitting(false);
  };

  const retryLastQuery = async () => {
    const content = lastUserMessage?.content.trim();
    if (!content || !canRetry) {
      return;
    }
    setIsRetrying(true);
    await onRetry(content);
    setIsRetrying(false);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    await submitInput();
  };

  const handleInputKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void submitInput();
    }
  };

  const createProjectFromInput = async () => {
    const title = newProjectTitle.trim();
    if (!title || isCreatingProject) {
      return;
    }
    setIsCreatingProject(true);
    try {
      await onCreateProject(title);
      setNewProjectTitle('');
    } finally {
      setIsCreatingProject(false);
    }
  };

  const renderLogList = (logs: StatusEvent[]) => {
    if (logs.length === 0) {
      return null;
    }
    return (
      <div className="min-w-0 text-xs leading-relaxed text-muted-foreground md:max-w-[34%] md:pt-9">
        <div className="mb-1 font-semibold uppercase tracking-wide text-muted-foreground/80">
          Activity
        </div>
        <div className="space-y-1">
          {logs.map((item) => (
            <div key={item.id} className="break-words">
              <span className="tabular-nums opacity-80">
                {new Date(item.created_at).toLocaleTimeString()}
              </span>
              <span> - {item.message}</span>
            </div>
          ))}
        </div>
      </div>
    );
  };

  const terminalClass =
    sessionStatus === 'success'
      ? 'border border-green-500/30 bg-green-500/15 text-foreground'
      : sessionStatus === 'failed' || sessionStatus === 'max_turns' || sessionStatus === 'max_spend'
      ? 'border border-destructive/30 bg-destructive/10 text-foreground'
      : 'bg-muted text-muted-foreground';

  const renderMessageBubble = (message: ChatMessage) => {
    const isUser = message.role === 'user';
    const isSystem = message.role === 'system';
    const baseMessageClass = isUser
      ? 'bg-primary text-primary-foreground'
      : isSystem
      ? 'bg-accent text-accent-foreground'
      : 'bg-muted text-muted-foreground';

    return (
      <div
        key={message.id}
        className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}
      >
        <div
          className={`max-w-[80%] rounded-lg p-4 transition-all ${baseMessageClass}`}
        >
          {isUser ? (
            <p className="whitespace-pre-wrap">{message.content}</p>
          ) : (
            <MarkdownMessage content={message.content} />
          )}
          <p className="text-xs opacity-70 mt-2">
            {new Date(message.created_at).toLocaleTimeString()}
          </p>
        </div>
      </div>
    );
  };

  const renderGlobalLogs = (logs: StatusEvent[], label = 'Run setup') => {
    if (logs.length === 0) {
      return null;
    }
    return (
      <div className="rounded-md bg-accent/70 p-3 text-xs leading-relaxed text-muted-foreground">
        <div className="mb-1 font-semibold uppercase tracking-wide text-foreground/70">
          {label}
        </div>
        <div className="space-y-1">
          {logs.map((item: StatusEvent) => (
            <div key={item.id}>
              {new Date(item.created_at).toLocaleTimeString()} - {item.message}
            </div>
          ))}
        </div>
      </div>
    );
  };

  const renderStepItem = (item: any, globalStepIndex: number) => {
    const step = item.codeStep;
    const message = item.message;
    const isActiveStep = isRunning
      ? timelineItemMatchesTarget(item, activeTimelineTarget)
      : globalStepIndex === currentStepIndex;
    const activeStepClass = isActiveStep
      ? 'ring-2 ring-foreground ring-offset-2 ring-offset-background bg-muted/80'
      : '';
    const content =
      message?.content ||
      (step ? codeStepFallbackContent(step) : 'Lea is preparing this step.');
    const createdAt = message?.created_at || step?.created_at || item.createdAt;

    return (
      <div key={item.id} className="flex justify-start">
        <div className="flex w-full flex-col gap-2 md:flex-row md:items-start">
          <div
            className={`max-w-[80%] cursor-pointer rounded-lg bg-muted p-4 text-muted-foreground transition-all hover:ring-2 hover:ring-foreground/40 md:max-w-[66%] ${activeStepClass}`}
            onClick={() => onStepSelect(globalStepIndex)}
          >
            <div className="mb-3 text-xs font-semibold uppercase tracking-wide text-foreground/70">
              <span>Step {item.stepNumber}</span>
              {isRunning && isActiveStep && (
                <Loader2 className="ml-2 inline h-3.5 w-3.5 animate-spin align-[-2px]" />
              )}
            </div>
            <MarkdownMessage content={content} />
            <p className="text-xs opacity-70 mt-2">
              {createdAt ? new Date(createdAt).toLocaleTimeString() : ''}
            </p>
          </div>
          {renderLogList(item.logs)}
        </div>
      </div>
    );
  };

  const renderTerminalMessage = (message: ChatMessage) => {
    const isSystem = message.role === 'system';
    const baseMessageClass = isSystem
      ? 'bg-accent text-accent-foreground'
      : terminalClass;

    return (
      <div key={message.id} className="flex justify-start">
        <div className={`max-w-[80%] rounded-lg p-4 transition-all ${baseMessageClass}`}>
          <MarkdownMessage content={message.content} />
          <p className="text-xs opacity-70 mt-2">
            {new Date(message.created_at).toLocaleTimeString()}
          </p>
        </div>
      </div>
    );
  };

  return (
    <div className="flex flex-col h-full bg-background">
      <div className="p-4 border-b border-border flex items-center justify-between gap-3">
        <div className="min-w-0 flex items-center gap-3">
          <h2 className="truncate text-foreground">{theoremName}</h2>
          {isRunning && (
            <div className="flex shrink-0 items-center gap-2 rounded-md bg-accent px-2 py-1 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Lea is working
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onTogglePause}
            className={[
              'flex items-center gap-2 rounded-md bg-secondary px-3 py-2',
              'text-secondary-foreground transition-opacity hover:opacity-90',
            ].join(' ')}
          >
            {isPaused ? (
              <>
                <Play className="w-4 h-4" />
                Resume
              </>
            ) : (
              <>
                <Pause className="w-4 h-4" />
                Pause
              </>
            )}
          </button>
          <button
            onClick={onOpenStats}
            className={[
              'flex items-center gap-2 rounded-md bg-secondary px-3 py-2',
              'text-secondary-foreground transition-opacity hover:opacity-90',
            ].join(' ')}
          >
            <BarChart3 className="w-4 h-4" />
            Statistics
          </button>
          <button
            onClick={onOpenSettings}
            className={[
              'flex items-center gap-2 rounded-md bg-secondary px-3 py-2',
              'text-secondary-foreground transition-opacity hover:opacity-90',
            ].join(' ')}
          >
            <Settings className="w-4 h-4" />
            Settings
          </button>
          {projectTheorem && (
            <button
              type="button"
              onClick={() => void onRequestProjectUnassignment()}
              disabled={isRunning || isUnassigningProject || !canUnassignProjectTheorem}
              title={
                canUnassignProjectTheorem
                  ? `Unassign ${projectTheorem.name} from this project`
                  : unassignmentDisabledReason || 'This theorem cannot be unassigned from the project.'
              }
              className={[
                'flex items-center gap-2 rounded-md bg-secondary px-3 py-2',
                'text-secondary-foreground transition-opacity hover:opacity-90',
                'disabled:cursor-not-allowed disabled:opacity-50',
              ].join(' ')}
            >
              {isUnassigningProject ? <Loader2 className="h-4 w-4 animate-spin" /> : <Unlink className="h-4 w-4" />}
              Unassign
            </button>
          )}
        </div>
      </div>

      <div className="relative min-h-0 flex-1">
        <div
          ref={scrollContainerRef}
          onScroll={handleChatScroll}
          className="h-full overflow-y-auto p-4"
        >
          <div className="flex min-h-full flex-col space-y-4">
            {messages.length === 0 && (
              <div className="flex flex-1 items-center justify-center text-center text-muted-foreground">
                Enter a theorem or natural-language proof task to start Lea.
              </div>
            )}

            {runSections.map((section) => (
              <div key={section.id} className="space-y-4">
                {section.attemptNumber !== null && (
                  <div className="flex items-center gap-3 py-2">
                    <div className="h-px flex-1 bg-border" />
                    <span className="shrink-0 rounded-md border border-border bg-background px-2 py-1 font-mono text-[0.65rem] uppercase tracking-widest text-muted-foreground">
                      Attempt {section.attemptNumber}
                    </span>
                    <div className="h-px flex-1 bg-border" />
                  </div>
                )}

                {section.timeline.userAndSystemMessages.map((message: ChatMessage) =>
                  renderMessageBubble(message),
                )}

                {renderGlobalLogs(section.timeline.globalLogs)}

                {section.approvals.map((approval) => (
                  <TheoremApprovalPanel
                    key={approval.id}
                    approval={approval}
                    resolved={Boolean(approval.decision)}
                  />
                ))}

                {section.pendingApproval && (
                  <TheoremApprovalPanel
                    approval={section.pendingApproval}
                    isSubmitting={isSubmittingApproval}
                    error={approvalError}
                    onSubmit={onSubmitApproval}
                  />
                )}

                {section.timeline.stepItems.map((item: any) =>
                  renderStepItem(item, section.stepOffset + item.stepIndex),
                )}

                {section.timeline.terminalMessages.map((message: ChatMessage) =>
                  renderTerminalMessage(message),
                )}
              </div>
            ))}

            {isSubmitting && !isRunning && (
              <div className="text-sm text-muted-foreground">
                Submitting to Lea...
              </div>
            )}

            {error && (
              <div className="flex flex-col gap-3 rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
                <div>{error}</div>
                {canRetry && (
                  <button
                    type="button"
                    onClick={retryLastQuery}
                    className={[
                      'inline-flex w-fit items-center gap-2 rounded-md bg-primary px-3 py-2',
                      'text-primary-foreground transition-opacity hover:opacity-90',
                      'disabled:cursor-not-allowed disabled:opacity-50',
                    ].join(' ')}
                  >
                    <RotateCcw className="h-4 w-4" />
                    Retry
                  </button>
                )}
              </div>
            )}
            <div ref={bottomAnchorRef} aria-hidden="true" />
          </div>
        </div>

        {safeVerify && !isRunning && <SafeVerifyBadge result={safeVerify} />}

        {showScrollToBottom && (
          <button
            type="button"
            onClick={resumeAutoScroll}
            className={[
              'absolute bottom-4 left-1/2 z-10 inline-flex -translate-x-1/2 items-center gap-2',
              'rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground shadow-md',
              'transition-opacity hover:opacity-90',
            ].join(' ')}
          >
            <ArrowDown className="h-4 w-4" />
            Latest
          </button>
        )}
      </div>

      <div className="p-4 border-t border-border">
        {canRetry && !error && (
          <div className="mb-3 flex justify-end">
            <button
              type="button"
              onClick={retryLastQuery}
              className={[
                'inline-flex items-center gap-2 rounded-md border border-border bg-secondary px-3 py-2',
                'text-sm text-secondary-foreground transition-opacity hover:opacity-90',
                'disabled:cursor-not-allowed disabled:opacity-50',
              ].join(' ')}
            >
              <RotateCcw className="h-4 w-4" />
              Retry
            </button>
          </div>
        )}
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <select
            value={selectedProjectId || ''}
            onChange={(event) => onProjectChange(event.target.value || undefined)}
            disabled={isRunning}
            className="h-9 rounded-md border border-border bg-input-background px-3 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">No project</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.title}
              </option>
            ))}
          </select>
          <input
            value={newProjectTitle}
            onChange={(event) => setNewProjectTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                void createProjectFromInput();
              }
            }}
            disabled={isRunning || isCreatingProject}
            placeholder="New project"
            className="h-9 w-40 rounded-md border border-border bg-input-background px-3 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
          />
          <button
            type="button"
            onClick={createProjectFromInput}
            disabled={isRunning || isCreatingProject || !newProjectTitle.trim()}
            className={[
              'inline-flex h-9 items-center gap-2 rounded-md bg-secondary px-3 text-sm',
              'text-secondary-foreground transition-opacity hover:opacity-90',
              'disabled:cursor-not-allowed disabled:opacity-50',
            ].join(' ')}
          >
            {isCreatingProject ? <Loader2 className="h-4 w-4 animate-spin" /> : <FolderPlus className="h-4 w-4" />}
            Create
          </button>
          <button
            type="button"
            onClick={() => void onRequestProjectAssignment()}
            disabled={isRunning || isAssigningProject || !selectedProjectId || isProjectAssociated}
            title={
              isProjectAssociated
                ? 'This formalization is already associated with a project. Reassignment is not supported yet.'
                : selectedProjectId
                ? 'Assign this formalization to the selected project'
                : 'Select a project before assigning'
            }
            className={[
              'inline-flex h-9 items-center gap-2 rounded-md bg-secondary px-3 text-sm',
              'text-secondary-foreground transition-opacity hover:opacity-90',
              'disabled:cursor-not-allowed disabled:opacity-50',
            ].join(' ')}
          >
            {isAssigningProject ? <Loader2 className="h-4 w-4 animate-spin" /> : <Link2 className="h-4 w-4" />}
            Assign
          </button>
        </div>
        <form onSubmit={handleSubmit} className="flex items-end gap-2">
          <textarea
            value={input}
            onChange={(e) => onInputChange(e.target.value)}
            onKeyDown={handleInputKeyDown}
            placeholder="Enter your theorem in LaTeX or natural language..."
            rows={2}
            className={[
              'max-h-32 min-h-12 flex-1 resize-none overflow-y-auto whitespace-pre-wrap',
              'rounded-md border border-border bg-input-background px-4 py-2',
              'focus:outline-none focus:ring-2 focus:ring-ring',
            ].join(' ')}
          />
          <button
            type="submit"
            disabled={isRunning || isSubmitting || isRetrying}
            className={[
              'flex items-center gap-2 rounded-md bg-primary px-4 py-2',
              'text-primary-foreground transition-opacity hover:opacity-90',
              'disabled:cursor-not-allowed disabled:opacity-50',
            ].join(' ')}
          >
            <Send className="w-4 h-4" />
            {isSubmitting || isRetrying ? 'Sending' : 'Send'}
          </button>
        </form>
      </div>
    </div>
  );
}
