import { useState } from 'react';
import { Loader2, ShieldCheck, ShieldAlert, ShieldQuestion } from 'lucide-react';
import type { SafeVerifyResult } from '../api';

/**
 * Surfaces the kernel-level SafeVerify audit of a finished proof: whether the
 * agent actually proved the statement (vs. a proof that merely compiles but
 * smuggles in `sorry`, extra axioms, or `native_decide`). Auto-runs on success.
 */
export function SafeVerifyBadge({ result }: { result: SafeVerifyResult }) {
  const [showDetail, setShowDetail] = useState(false);
  const { status, detail } = result;

  if (status === 'unavailable') {
    return null; // SafeVerify not built on this server — stay quiet.
  }

  if (status === 'pending' || status === 'running') {
    return (
      <div className="flex items-center gap-2 rounded-md border border-border bg-muted/60 px-3 py-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        <span>Running SafeVerify — kernel-checking the proof (this can take a minute)…</span>
      </div>
    );
  }

  if (status === 'passed') {
    return (
      <div className="flex items-center gap-2 rounded-md border border-green-500/40 bg-green-500/10 px-3 py-2 text-sm text-green-700">
        <ShieldCheck className="h-4 w-4" />
        <span>
          <strong>SafeVerified</strong> — kernel-checked: the proof holds with no <code>sorry</code>,
          extra axioms, or unsafe tricks.
        </span>
      </div>
    );
  }

  const isFail = status === 'failed';
  const Icon = isFail ? ShieldAlert : ShieldQuestion;
  const tone = isFail
    ? 'border-destructive/40 bg-destructive/10 text-destructive'
    : 'border-amber-500/40 bg-amber-500/10 text-amber-700';
  const headline = isFail
    ? 'SafeVerify rejected this proof — it does not actually prove the statement.'
    : 'SafeVerify could not complete.';

  return (
    <div className={`flex flex-col gap-2 rounded-md border px-3 py-2 text-sm ${tone}`}>
      <div className="flex items-center gap-2">
        <Icon className="h-4 w-4 shrink-0" />
        <span>
          <strong>{isFail ? 'Not verified' : 'SafeVerify error'}</strong> — {headline}
        </span>
      </div>
      {detail && (
        <div>
          <button
            type="button"
            className="text-xs underline opacity-80 hover:opacity-100"
            onClick={() => setShowDetail((v) => !v)}
          >
            {showDetail ? 'Hide details' : 'Show details'}
          </button>
          {showDetail && (
            <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-background/60 p-2 text-xs">
              {detail}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
