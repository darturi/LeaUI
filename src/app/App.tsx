import { useEffect, useMemo, useRef, useState } from 'react';
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels';
import { SessionList } from './components/SessionList';
import { ChatInterface } from './components/ChatInterface';
import { CodeViewer } from './components/CodeViewer';
import { StatsPage } from './components/StatsPage';
import { SettingsPage } from './components/SettingsPage';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from './components/ui/alert-dialog';
import { timelineStepCount } from './stepTimeline.mjs';
import { hasResolvedProjectAssociation } from './projectAssociation.mjs';
import { buildRunTimelineSections, timelineIndexForCodeStep } from './runAttempts';
import { timelineIndexForTarget } from './timelineTarget.mjs';
import {
  ChatMessage,
  CodeStep,
  ApprovalDecision,
  ApprovalEvent,
  PendingApproval,
  ProjectTheoremEntry,
  ProjectAssignmentCheck,
  ProjectUnassignmentCheck,
  SessionSummary,
  SessionDetail,
  StatusEvent,
  Project,
  assignProject,
  checkProjectAssignment,
  checkProjectTheoremUnassignment,
  createProject,
  createRun,
  getSession,
  listProjects,
  listSessions,
  submitApproval,
  unassignProjectTheorem,
} from './api';

export type ActiveTimelineTarget = {
  runId?: string;
  codeStepId?: string;
  messageId?: string;
  provisionalKey?: string;
} | null;

export default function App() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string>();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatDraft, setChatDraft] = useState('');
  const [codeSteps, setCodeSteps] = useState<CodeStep[]>([]);
  const [isPaused, setIsPaused] = useState(false);
  const [currentStepIndex, setCurrentStepIndex] = useState(0);
  const [activeTimelineTarget, setActiveTimelineTarget] = useState<ActiveTimelineTarget>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [currentRunId, setCurrentRunId] = useState<string>();
  const [pendingApproval, setPendingApproval] = useState<PendingApproval>();
  const [isSubmittingApproval, setIsSubmittingApproval] = useState(false);
  const [approvalError, setApprovalError] = useState<string>();
  const [error, setError] = useState<string>();
  const [statusEvents, setStatusEvents] = useState<StatusEvent[]>([]);
  const [approvalEvents, setApprovalEvents] = useState<ApprovalEvent[]>([]);
  const [view, setView] = useState<'main' | 'stats' | 'settings'>('main');
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>();
  const [projectTheorem, setProjectTheorem] = useState<ProjectTheoremEntry>();
  const [pendingAssignment, setPendingAssignment] = useState<{ projectId: string; projectTitle: string }>();
  const [assignmentPlan, setAssignmentPlan] = useState<ProjectAssignmentCheck>();
  const [isAssigningProject, setIsAssigningProject] = useState(false);
  const [pendingUnassignment, setPendingUnassignment] = useState<ProjectUnassignmentCheck>();
  const [availableUnassignment, setAvailableUnassignment] = useState<ProjectUnassignmentCheck>();
  const [unassignmentBlockedReason, setUnassignmentBlockedReason] = useState<string>();
  const [isUnassigningProject, setIsUnassigningProject] = useState(false);
  const eventSourceRef = useRef<EventSource | null>(null);
  const selectedSessionIdRef = useRef<string | undefined>(undefined);
  const codeStepCountRef = useRef(0);
  const codeStepsRef = useRef<CodeStep[]>([]);
  const activeTimelineTargetRef = useRef<ActiveTimelineTarget>(null);
  const messagesRef = useRef<ChatMessage[]>([]);
  const statusEventsRef = useRef<StatusEvent[]>([]);
  const approvalEventsRef = useRef<ApprovalEvent[]>([]);
  const pendingApprovalRef = useRef<PendingApproval | undefined>(undefined);
  const selectedTerminalMessageIdRef = useRef<string | null>(null);

  const setActiveTimelineStep = (target: ActiveTimelineTarget) => {
    activeTimelineTargetRef.current = target;
    setActiveTimelineTarget(target);
  };

  const terminalMessageIdForDetail = (
    detail: { messages: ChatMessage[]; status?: string },
  ) => {
    if (!detail.status || detail.status === 'running') {
      return null;
    }
    const terminalMessage = [...detail.messages]
      .reverse()
      .find((message) => message.role === 'assistant' || message.role === 'system');
    return terminalMessage?.id ?? null;
  };

  const lastTimelineStepIndex = (detail: { messages: ChatMessage[]; code_steps: CodeStep[]; status?: string }) => {
    const count = timelineStepCount({
      messages: detail.messages,
      codeSteps: detail.code_steps,
      terminalMessageId: terminalMessageIdForDetail(detail),
    });
    return Math.max(0, count - 1);
  };

  const refreshSessions = async () => {
    const loaded = await listSessions();
    setSessions(loaded);
    return loaded;
  };

  const refreshProjects = async () => {
    const loaded = await listProjects();
    setProjects(loaded);
    return loaded;
  };

  const refreshUnassignmentAvailability = async (detail: SessionDetail) => {
    setAvailableUnassignment(undefined);
    setUnassignmentBlockedReason(undefined);
    if (!detail.project?.id || !detail.project_theorem || detail.active_run) {
      return;
    }
    const sessionId = detail.id;
    try {
      const plan = await checkProjectTheoremUnassignment(detail.project.id, detail.project_theorem.name);
      setAvailableUnassignment(selectedSessionIdRef.current === sessionId ? plan : undefined);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'This theorem cannot be unassigned.';
      if (selectedSessionIdRef.current === sessionId) {
        setUnassignmentBlockedReason(message);
      }
    }
  };

  useEffect(() => {
    const restoreInitialSession = async () => {
      const loaded = await refreshSessions();
      await refreshProjects();
      const savedSessionId = window.localStorage.getItem('lea:selectedSessionId');
      const savedSession = savedSessionId
        ? loaded.find((session) => session.id === savedSessionId)
        : undefined;
      const activeSession = loaded.find((session) => session.status === 'running');
      const sessionToRestore = savedSession || activeSession;
      if (sessionToRestore) {
        await loadSession(sessionToRestore.id);
      }
    };
    restoreInitialSession().catch((err) => setError(err.message));
    return () => {
      eventSourceRef.current?.close();
    };
  }, []);

  const applySessionDetail = (detail: SessionDetail) => {
    selectedSessionIdRef.current = detail.id;
    setSelectedSessionId(detail.id);
    setMessages(detail.messages);
    setCodeSteps(detail.code_steps);
    setStatusEvents(detail.status_events || []);
    setApprovalEvents(detail.approval_events || []);
    setSelectedProjectId(detail.project?.id || detail.project_id || undefined);
    setProjectTheorem(detail.project_theorem || undefined);
    setPendingAssignment(undefined);
    setAssignmentPlan(undefined);
    setPendingUnassignment(undefined);
    setAvailableUnassignment(undefined);
    setUnassignmentBlockedReason(undefined);
    codeStepCountRef.current = detail.code_steps.length;
    setCurrentStepIndex(lastTimelineStepIndex(detail));
    setActiveTimelineStep(null);
    setCurrentRunId(detail.active_run?.id);
    setPendingApproval(
      detail.active_run?.pending_approval
        ? {
            ...detail.active_run.pending_approval,
            session_id: detail.id,
            run_id: detail.active_run.id,
          }
        : undefined,
    );
    setIsRunning(Boolean(detail.active_run));
    setApprovalError(undefined);
  };

  const loadSession = async (sessionId: string) => {
    const detail = await getSession(sessionId);
    applySessionDetail(detail);
    await refreshUnassignmentAvailability(detail);
    window.localStorage.setItem('lea:selectedSessionId', detail.id);
  };

  const reconcileSession = async (sessionId: string) => {
    const detail = await getSession(sessionId);
    applySessionDetail(detail);
    await refreshUnassignmentAvailability(detail);
  };

  const selectedSession = useMemo(
    () => sessions.find((session) => session.id === selectedSessionId),
    [sessions, selectedSessionId],
  );

  const title = selectedSession?.title || 'New theorem session';
  const selectedTerminalMessageId = useMemo(() => {
    if (isRunning || !selectedSession?.status || selectedSession.status === 'running') {
      return null;
    }
    const terminalMessage = [...messages]
      .reverse()
      .find((message) => message.role === 'assistant' || message.role === 'system');
    return terminalMessage?.id ?? null;
  }, [isRunning, messages, selectedSession?.status]);
  messagesRef.current = messages;
  codeStepsRef.current = codeSteps;
  statusEventsRef.current = statusEvents;
  approvalEventsRef.current = approvalEvents;
  pendingApprovalRef.current = pendingApproval;
  selectedTerminalMessageIdRef.current = selectedTerminalMessageId;
  const currentTimelineStepCount = useMemo(
    () =>
      timelineStepCount({
        messages,
        codeSteps,
        terminalMessageId: selectedTerminalMessageId,
      }),
    [messages, codeSteps, selectedTerminalMessageId],
  );
  const runTimelineSections = useMemo(
    () =>
      buildRunTimelineSections({
        messages,
        codeSteps,
        statusEvents,
        approvalEvents,
        pendingApproval,
        terminalMessageId: selectedTerminalMessageId,
      }),
    [messages, codeSteps, statusEvents, approvalEvents, pendingApproval, selectedTerminalMessageId],
  );
  const activeTimelineStepIndex = useMemo(
    () => timelineIndexForTarget(runTimelineSections, activeTimelineTarget),
    [runTimelineSections, activeTimelineTarget],
  );

  const appendMessage = (message: ChatMessage) => {
    setMessages((current) => {
      if (current.some((item) => item.id === message.id)) {
        messagesRef.current = current;
        return current;
      }
      const next = [...current, message];
      messagesRef.current = next;
      return next;
    });
  };

  const handleSubmit = async (content: string): Promise<boolean> => {
    setError(undefined);
    setApprovalError(undefined);
    setPendingApproval(undefined);
    setProjectTheorem(undefined);
    setPendingUnassignment(undefined);
    setAvailableUnassignment(undefined);
    setUnassignmentBlockedReason(undefined);
    eventSourceRef.current?.close();

    try {
      const run = await createRun(content, selectedSessionId, selectedProjectId);
      setSelectedSessionId(run.session_id);
      setCurrentRunId(run.run_id);
      appendMessage(run.message);
      setStatusEvents((current) => [
        ...current,
        {
          id: `submitted-${run.run_id}`,
          session_id: run.session_id,
          run_id: run.run_id,
          step_number: null,
          status: 'submitted',
          message: 'Submitted theorem to backend.',
          created_at: new Date().toISOString(),
        },
      ]);
      setActiveTimelineStep(null);
      setIsRunning(true);

      const nextSessions = await refreshSessions();
      if (!selectedSessionId && nextSessions.length === 1) {
        setSelectedSessionId(run.session_id);
      }

      const source = new EventSource(`/api/runs/${run.run_id}/events`);
      eventSourceRef.current = source;

      let liveAssistantId = `live-${run.run_id}`;
      let sawTerminalEvent = false;
      source.addEventListener('assistant_delta', (event) => {
        const payload = JSON.parse((event as MessageEvent).data);
        setMessages((current) => {
          const existing = current.find((message) => message.id === liveAssistantId);
          if (existing) {
            const next = current.map((message) =>
              message.id === liveAssistantId
                ? { ...message, content: message.content + payload.text }
                : message,
            );
            messagesRef.current = next;
            return next;
          }
          const assistantStepCount = current.filter(
            (message) =>
              message.role === 'assistant' &&
              !message.is_live_terminal_summary,
          ).length;
          const next = [
            ...current,
            {
              id: liveAssistantId,
              session_id: run.session_id,
              run_id: run.run_id,
              role: 'assistant',
              content: payload.text,
              created_at: new Date().toISOString(),
              live_started_after_assistant_steps: assistantStepCount,
              live_started_after_code_steps: codeStepCountRef.current,
            },
          ];
          messagesRef.current = next;
          const target = { runId: run.run_id, messageId: liveAssistantId };
          setActiveTimelineStep(target);
          const nextSections = buildRunTimelineSections({
            messages: next,
            codeSteps: codeStepsRef.current,
            statusEvents: statusEventsRef.current,
            approvalEvents: approvalEventsRef.current,
            pendingApproval: pendingApprovalRef.current,
            terminalMessageId: selectedTerminalMessageIdRef.current,
          });
          const timelineIndex = timelineIndexForTarget(nextSections, target);
          if (timelineIndex !== null) {
            setCurrentStepIndex(timelineIndex);
          }
          return next;
        });
      });

      source.addEventListener('message', (event) => {
        const payload = JSON.parse((event as MessageEvent).data) as ChatMessage;
        setMessages((current) => {
          const liveMessage = current.find((message) => message.id === liveAssistantId);
          const shouldReplaceLive =
            payload.role === 'assistant' &&
            !!liveMessage &&
            (payload.content.includes(liveMessage.content) ||
              liveMessage.content.includes(payload.content));
          const withoutLive =
            shouldReplaceLive
              ? current.filter((message) => message.id !== liveAssistantId)
              : current;
          if (withoutLive.some((message) => message.id === payload.id)) {
            messagesRef.current = withoutLive;
            return withoutLive;
          }
          const nextMessage =
            shouldReplaceLive && liveMessage
              ? {
                  ...payload,
                  live_started_after_assistant_steps: liveMessage.live_started_after_assistant_steps,
                  live_started_after_code_steps: liveMessage.live_started_after_code_steps,
                }
              : payload;
          const next = [
            ...withoutLive,
            nextMessage,
          ];
          messagesRef.current = next;
          const activeTarget = activeTimelineTargetRef.current;
          if (
            shouldReplaceLive &&
            activeTarget?.messageId === liveAssistantId &&
            payload.role === 'assistant'
          ) {
            const target = { runId: payload.run_id || run.run_id, messageId: payload.id };
            setActiveTimelineStep(target);
            const nextSections = buildRunTimelineSections({
              messages: next,
              codeSteps: codeStepsRef.current,
              statusEvents: statusEventsRef.current,
              approvalEvents: approvalEventsRef.current,
              pendingApproval: pendingApprovalRef.current,
              terminalMessageId: selectedTerminalMessageIdRef.current,
            });
            const timelineIndex = timelineIndexForTarget(nextSections, target);
            if (timelineIndex !== null) {
              setCurrentStepIndex(timelineIndex);
            }
          }
          return next;
        });
      });

      source.addEventListener('code_step', (event) => {
        const payload = JSON.parse((event as MessageEvent).data) as CodeStep;
        setCodeSteps((current) => {
          if (current.some((step) => step.id === payload.id)) {
            return current;
          }
          const next = [...current, payload];
          codeStepsRef.current = next;
          codeStepCountRef.current = next.length;
          const nextSections = buildRunTimelineSections({
            messages: messagesRef.current,
            codeSteps: next,
            statusEvents: statusEventsRef.current,
            approvalEvents: approvalEventsRef.current,
            pendingApproval: pendingApprovalRef.current,
            terminalMessageId: selectedTerminalMessageIdRef.current,
          });
          const timelineIndex = timelineIndexForCodeStep(nextSections, payload.id) ?? payload.step_number - 1;
          setCurrentStepIndex(timelineIndex);
          setActiveTimelineStep({ runId: payload.run_id, codeStepId: payload.id });
          return next;
        });
      });

      source.addEventListener('status', (event) => {
        const payload = JSON.parse((event as MessageEvent).data);
        const payloadStepNumber =
          Number.isInteger(payload.step_number) && payload.step_number > 0
            ? payload.step_number
            : null;
        const nextSections = buildRunTimelineSections({
          messages: messagesRef.current,
          codeSteps: codeStepsRef.current,
          statusEvents: statusEventsRef.current,
          approvalEvents: approvalEventsRef.current,
          pendingApproval: pendingApprovalRef.current,
          terminalMessageId: selectedTerminalMessageIdRef.current,
        });
        const activeStepIndex = timelineIndexForTarget(nextSections, activeTimelineTargetRef.current);
        const activeStepNumber = activeStepIndex === null ? null : activeStepIndex + 1;
        setStatusEvents((current) => {
          const next = [
            ...current,
            {
              id: payload.id || `${run.run_id}-${current.length}-${Date.now()}`,
              session_id: payload.session_id || run.session_id,
              run_id: payload.run_id || run.run_id,
              step_number: payloadStepNumber || activeStepNumber,
              status: payload.status || null,
              message: payload.message || payload.status || 'Lea status update',
              created_at: payload.created_at || new Date().toISOString(),
            },
          ];
          statusEventsRef.current = next;
          return next;
        });
      });

      source.addEventListener('approval_requested', (event) => {
        const payload = {
          ...(JSON.parse((event as MessageEvent).data) as PendingApproval),
          session_id: run.session_id,
          run_id: run.run_id,
        };
        setPendingApproval(payload);
        setApprovalEvents((current) => {
          if (current.some((item) => item.approval_id === payload.approval_id)) {
            return current;
          }
          return [
            ...current,
            {
              ...payload,
              id: `${run.run_id}:${payload.approval_id}`,
              session_id: run.session_id,
              run_id: run.run_id,
              decision: null,
              feedback: null,
              resolved_at: null,
            },
          ];
        });
        setApprovalError(undefined);
      });

      source.addEventListener('approval_resolved', (event) => {
        const payload = JSON.parse((event as MessageEvent).data) as {
          approval_id?: string;
          decision?: string;
          feedback?: string | null;
        };
        setApprovalEvents((current) =>
          current.map((item) =>
            item.approval_id === payload.approval_id
              ? {
                  ...item,
                  decision: payload.decision || 'resolved',
                  feedback: payload.feedback || null,
                  resolved_at: new Date().toISOString(),
                }
              : item,
          ),
        );
        setPendingApproval((current) =>
          current?.approval_id === payload.approval_id ? undefined : current,
        );
        setApprovalError(undefined);
      });

      source.addEventListener('run_error', (event) => {
        const data = (event as MessageEvent).data;
        if (data) {
          const payload = JSON.parse(data);
          setError(payload.message || 'Lea reported an error.');
        }
      });

      source.addEventListener('done', async () => {
        sawTerminalEvent = true;
        eventSourceRef.current = null;
        source.close();
        setIsRunning(false);
        setCurrentRunId(undefined);
        setPendingApproval(undefined);
        setApprovalError(undefined);
        setActiveTimelineStep(null);
        await reconcileSession(run.session_id);
        await refreshSessions();
      });

      source.onerror = async () => {
        if (eventSourceRef.current !== source || source.readyState === EventSource.CLOSED) {
          return;
        }
        if (sawTerminalEvent) {
          eventSourceRef.current = null;
          source.close();
          return;
        }
        eventSourceRef.current = null;
        source.close();
        setIsRunning(false);
        setCurrentRunId(undefined);
        setPendingApproval(undefined);
        setActiveTimelineStep(null);
        try {
          const detail = await getSession(run.session_id);
          applySessionDetail(detail);
          await refreshSessions();
          if (detail.active_run || detail.status === 'running') {
            setError('Lost connection to the Lea backend.');
          }
        } catch {
          setError('Lost connection to the Lea backend.');
        }
      };
      return true;
    } catch (err) {
      setIsRunning(false);
      setCurrentRunId(undefined);
      setError(err instanceof Error ? err.message : 'Unable to start Lea.');
      return false;
    }
  };

  const handleSubmitApproval = async (
    decision: ApprovalDecision,
    feedback?: string,
  ): Promise<void> => {
    if (!currentRunId || !pendingApproval || isSubmittingApproval) {
      return;
    }
    setIsSubmittingApproval(true);
    setApprovalError(undefined);
    try {
      await submitApproval(
        currentRunId,
        pendingApproval.approval_id,
        decision,
        decision === 'reject' ? feedback : undefined,
      );
      setPendingApproval(undefined);
    } catch (err) {
      setApprovalError(err instanceof Error ? err.message : 'Unable to submit approval.');
    } finally {
      setIsSubmittingApproval(false);
    }
  };

  const handleRequestProjectAssignment = async () => {
    const projectId = selectedProjectId;
    const project = projects.find((item) => item.id === projectId);
    if (!selectedSessionId || !projectId || !project || isRunning || isAssigningProject) {
      return;
    }
    if (hasResolvedProjectAssociation(projectTheorem)) {
      setError('This formalization is already associated with a project. Reassignment is not supported yet.');
      return;
    }
    if (selectedSession?.status === 'failed') {
      setError(
        'This proof did not complete successfully. Retry the proof in this chat with a project selected; if the retry succeeds, the formalization will join that project. Failed formalizations are not moved.',
      );
      return;
    }
    if (selectedSession?.status !== 'success') {
      setError('Only completed successful formalizations can be assigned to a project.');
      return;
    }
    setError(undefined);
    setAssignmentPlan(undefined);
    setPendingAssignment({ projectId, projectTitle: project.title });
  };

  const handleConfirmProjectAssignment = async () => {
    if (!selectedSessionId || !pendingAssignment || isAssigningProject) {
      return;
    }
    setIsAssigningProject(true);
    setError(undefined);
    try {
      const plan = await checkProjectAssignment(selectedSessionId, pendingAssignment.projectId);
      setAssignmentPlan(plan);
      const result = await assignProject(selectedSessionId, pendingAssignment.projectId);
      setPendingAssignment(undefined);
      setAssignmentPlan(undefined);
      await reconcileSession(selectedSessionId);
      await refreshSessions();
      await refreshProjects();
      setCurrentStepIndex(Math.max(0, result.code_step.step_number - 1));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to assign formalization to project.');
    } finally {
      setIsAssigningProject(false);
    }
  };

  const handleRequestProjectUnassignment = async () => {
    if (!availableUnassignment || isRunning || isUnassigningProject) {
      return;
    }
    setPendingUnassignment(availableUnassignment);
  };

  const handleConfirmProjectUnassignment = async () => {
    const projectId = selectedProjectId || selectedSession?.project_id || undefined;
    if (!projectId || !pendingUnassignment || isUnassigningProject) {
      return;
    }
    setIsUnassigningProject(true);
    setError(undefined);
    try {
      const result = await unassignProjectTheorem(projectId, pendingUnassignment.theorem.name);
      setPendingUnassignment(undefined);
      setAvailableUnassignment(undefined);
      setUnassignmentBlockedReason(undefined);
      if (selectedSessionId) {
        await reconcileSession(selectedSessionId);
      }
      await refreshSessions();
      await refreshProjects();
      setStatusEvents((current) => [
        ...current,
        {
          id: `project-unassigned-${Date.now()}`,
          session_id: selectedSessionId,
          run_id: undefined,
          step_number: null,
          status: 'project_unassigned',
          message: `Unassigned ${result.theorem.name} from the project and moved it to ${result.move.to_path}.`,
          created_at: new Date().toISOString(),
        },
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to unassign project theorem.');
    } finally {
      setIsUnassigningProject(false);
    }
  };

  if (view === 'stats') {
    return <StatsPage onBack={() => setView('main')} />;
  }
  if (view === 'settings') {
    return <SettingsPage onBack={() => setView('main')} />;
  }

  return (
    <>
      <AlertDialog open={Boolean(pendingUnassignment)} onOpenChange={(open) => !open && setPendingUnassignment(undefined)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Unassign theorem from project?</AlertDialogTitle>
            <AlertDialogDescription>
              This will remove {pendingUnassignment?.theorem.name} from the project markdown and move
              {' '}{pendingUnassignment?.planned_move.from_path} to {pendingUnassignment?.planned_move.to_path}.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isUnassigningProject}>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmProjectUnassignment} disabled={isUnassigningProject}>
              Unassign
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <AlertDialog
        open={Boolean(pendingAssignment)}
        onOpenChange={(open) => {
          if (!open && !isAssigningProject) {
            setPendingAssignment(undefined);
            setAssignmentPlan(undefined);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Assign formalization to project?</AlertDialogTitle>
            <AlertDialogDescription>
              This will check for namespace conflicts, update the Lean namespace, move the proof out of
              {' '}Lea.Misc, and record it in {pendingAssignment?.projectTitle}.
              {assignmentPlan
                ? ` Planned move: ${assignmentPlan.planned_move.from_path} to ${assignmentPlan.planned_move.to_path}.`
                : ''}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isAssigningProject}>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmProjectAssignment} disabled={isAssigningProject}>
              Assign
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <div className="size-full">
      <PanelGroup direction="horizontal">
        <Panel defaultSize={20} minSize={15} maxSize={30}>
          <SessionList
            sessions={sessions}
            selectedSessionId={selectedSessionId}
            onSelectSession={(id) => loadSession(id).catch((err) => setError(err.message))}
            onNewSession={() => {
              selectedSessionIdRef.current = undefined;
              setSelectedSessionId(undefined);
              setMessages([]);
              setChatDraft('');
              setCodeSteps([]);
              codeStepCountRef.current = 0;
              setStatusEvents([]);
              setApprovalEvents([]);
              setCurrentStepIndex(0);
              setActiveTimelineStep(null);
              setCurrentRunId(undefined);
              setPendingApproval(undefined);
              setProjectTheorem(undefined);
              setPendingAssignment(undefined);
              setAssignmentPlan(undefined);
              setPendingUnassignment(undefined);
              setAvailableUnassignment(undefined);
              setUnassignmentBlockedReason(undefined);
              setApprovalError(undefined);
              setIsRunning(false);
              setSelectedProjectId(undefined);
              window.localStorage.removeItem('lea:selectedSessionId');
              setError(undefined);
            }}
          />
        </Panel>

        <PanelResizeHandle className="w-1 bg-border hover:bg-primary transition-colors" />

        <Panel defaultSize={50} minSize={30}>
          <ChatInterface
            sessionId={selectedSessionId}
            error={error}
            isPaused={isPaused}
            isRunning={isRunning}
            messages={messages}
            codeSteps={codeSteps}
            sessionStatus={selectedSession?.status}
            statusEvents={statusEvents}
            approvalEvents={approvalEvents}
            input={chatDraft}
            onSubmit={handleSubmit}
            onRetry={handleSubmit}
            onInputChange={setChatDraft}
            onSubmitApproval={handleSubmitApproval}
            onStepSelect={setCurrentStepIndex}
            onTogglePause={() => setIsPaused(!isPaused)}
            onOpenStats={() => setView('stats')}
            onOpenSettings={() => setView('settings')}
            onRequestProjectAssignment={handleRequestProjectAssignment}
            onRequestProjectUnassignment={handleRequestProjectUnassignment}
            theoremName={title}
            currentStepIndex={currentStepIndex}
            activeTimelineTarget={activeTimelineTarget}
            pendingApproval={pendingApproval}
            isSubmittingApproval={isSubmittingApproval}
            approvalError={approvalError}
            projectTheorem={projectTheorem}
            isProjectAssociated={hasResolvedProjectAssociation(projectTheorem)}
            isAssigningProject={isAssigningProject}
            canUnassignProjectTheorem={Boolean(availableUnassignment)}
            unassignmentDisabledReason={unassignmentBlockedReason}
            isUnassigningProject={isUnassigningProject}
            projects={projects}
            selectedProjectId={selectedProjectId}
            onProjectChange={setSelectedProjectId}
            onCreateProject={async (title) => {
              const slug = title
                .trim()
                .toLowerCase()
                .replace(/[^a-z0-9_-]+/g, '-')
                .replace(/^-+|-+$/g, '')
                || 'project';
              const project = await createProject({ title, slug });
              await refreshProjects();
              setSelectedProjectId(project.id);
              return project;
            }}
          />
        </Panel>

        <PanelResizeHandle className="w-1 bg-border hover:bg-primary transition-colors" />

        <Panel defaultSize={30} minSize={20} maxSize={50}>
          <CodeViewer
            codeSteps={codeSteps}
            runTimelineSections={runTimelineSections}
            timelineStepCount={Math.max(
              currentTimelineStepCount,
              activeTimelineStepIndex === null ? 0 : activeTimelineStepIndex + 1,
            )}
            isPaused={isPaused}
            isRunning={isRunning}
            currentStepIndex={currentStepIndex}
            onStepChange={setCurrentStepIndex}
          />
        </Panel>
      </PanelGroup>
      </div>
    </>
  );
}
