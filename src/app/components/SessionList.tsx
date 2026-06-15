import { CheckCircle, XCircle, Circle } from 'lucide-react';
import { SessionSummary } from '../api';
import { projectTagClass } from '../projectTags.js';

export function SessionList({
  sessions,
  selectedSessionId,
  onSelectSession,
  onNewSession,
}: {
  sessions: SessionSummary[];
  selectedSessionId?: string;
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
}) {
  const getStatusIcon = (status: SessionSummary['status']) => {
    switch (status) {
      case 'success':
        return <CheckCircle className="w-4 h-4 text-green-600" />;
      case 'failed':
      case 'max_turns':
      case 'max_spend':
        return <XCircle className="w-4 h-4 text-destructive" />;
      case 'running':
        return <Circle className="w-4 h-4 text-muted-foreground animate-pulse" />;
    }
  };

  return (
    <div className="flex flex-col h-full bg-sidebar border-r border-sidebar-border">
      <div className="p-4 border-b border-sidebar-border">
        <h2 className="text-sidebar-foreground">Sessions</h2>
      </div>

      <div className="flex-1 overflow-y-auto">
        {sessions.length === 0 ? (
          <div className="p-4 text-sm text-muted-foreground">
            No saved sessions yet.
          </div>
        ) : (
          sessions.map((session) => (
            <button
              key={session.id}
              onClick={() => onSelectSession(session.id)}
              className={`w-full p-4 text-left border-b border-sidebar-border hover:bg-sidebar-accent transition-colors ${
                selectedSessionId === session.id ? 'bg-sidebar-accent' : ''
              }`}
            >
              <div className="flex items-start gap-3">
                <div className="flex-shrink-0 mt-1">
                  {getStatusIcon(session.status)}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm text-sidebar-foreground truncate">
                    {session.title}
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {new Date(session.updated_at).toLocaleTimeString()}
                  </p>
                  {session.project_title && (
                    <span
                      className={`mt-1 inline-flex max-w-full items-center rounded-full border px-2 py-0.5 text-xs font-medium ${projectTagClass(session.project_title)}`}
                      title={session.project_title}
                    >
                      <span className="truncate">
                        {session.project_title}
                      </span>
                    </span>
                  )}
                </div>
              </div>
            </button>
          ))
        )}
      </div>

      <div className="p-4 border-t border-sidebar-border">
        <button
          onClick={onNewSession}
          className="w-full px-4 py-2 bg-primary text-primary-foreground rounded-md hover:opacity-90 transition-opacity"
        >
          New Session
        </button>
      </div>
    </div>
  );
}
