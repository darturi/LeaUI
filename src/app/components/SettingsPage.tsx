import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Check, Eye, EyeOff, KeyRound, RotateCcw, Save, Settings, X } from 'lucide-react';
import {
  AppSettings,
  ApiKeyStatus,
  ModelCatalogEntry,
  ModelRequirements,
  PermissionTier,
  SettingsUpdate,
  fetchModelCatalog,
  fetchModelRequirements,
  getSettings,
  saveSettings,
} from '../api';
import { ModelCombobox } from './ModelCombobox';
import { RadioGroup, RadioGroupItem } from './ui/radio-group';
import { Slider } from './ui/slider';
import { Progress } from './ui/progress';
import { Input } from './ui/input';
import { Label } from './ui/label';
import { Separator } from './ui/separator';

const PERMISSION_DETAILS: Record<PermissionTier, { title: string; description: string }> = {
  none: {
    title: 'Fully autonomous',
    description: 'The agent can run without approval prompts.',
  },
  theorem_translation: {
    title: 'Approve theorem formalization',
    description: 'Ask before committing the top-level Lean theorem statement.',
  },
  stepwise: {
    title: 'Approve each agent step',
    description: 'Ask before each agent action during formalization.',
  },
};

// Client-side format hints for the first-class providers; other providers are
// validated by the backend / provider at runtime.
const KEY_PATTERNS: Record<string, RegExp> = {
  OPENAI_API_KEY: /^sk-[A-Za-z0-9_-]{8,}$/,
  ANTHROPIC_API_KEY: /^sk-ant-.+/,
  GOOGLE_API_KEY: /^AIza[A-Za-z0-9_-]{20,}$/,
};
const KEY_PLACEHOLDERS: Record<string, string> = {
  OPENAI_API_KEY: 'sk-...',
  ANTHROPIC_API_KEY: 'sk-ant-...',
  GOOGLE_API_KEY: 'AIza...',
};

interface KeyField {
  env: string;
  label: string;
  status?: ApiKeyStatus;
}

export function SettingsPage({ onBack }: { onBack: () => void }) {
  const [settings, setSettings] = useState<AppSettings>();
  const [model, setModel] = useState('');
  const [permissionTier, setPermissionTier] = useState<PermissionTier>('theorem_translation');
  const [theoremTranslationRetries, setTheoremTranslationRetries] = useState(3);
  const [maxTurns, setMaxTurns] = useState(20);
  const [maxSpend, setMaxSpend] = useState('');
  const [apiKeys, setApiKeys] = useState<Record<string, string>>({});
  const [clearedKeys, setClearedKeys] = useState<Record<string, boolean>>({});
  const [visibleKeys, setVisibleKeys] = useState<Record<string, boolean>>({});
  const [catalog, setCatalog] = useState<ModelCatalogEntry[]>([]);
  const [requirements, setRequirements] = useState<ModelRequirements>();
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string>();
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const load = async () => {
    setIsLoading(true);
    setError(undefined);
    try {
      const loaded = await getSettings();
      setSettings(loaded);
      setModel(loaded.model);
      setPermissionTier(loaded.permission_tier);
      setTheoremTranslationRetries(loaded.theorem_translation_max_retries);
      setMaxTurns(loaded.max_turns ?? 20);
      setMaxSpend(loaded.max_spend_usd == null ? '' : String(loaded.max_spend_usd));
      setApiKeys({});
      setClearedKeys({});
      setFieldErrors({});
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to load settings.');
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // The full model catalog is best-effort — a blank list just falls back to
    // the featured shortlist in the combobox.
    fetchModelCatalog()
      .then(setCatalog)
      .catch(() => setCatalog([]));
  }, []);

  // Look up which API key(s) the selected model needs, so the right field shows.
  useEffect(() => {
    if (!model.trim()) {
      setRequirements(undefined);
      return;
    }
    let cancelled = false;
    fetchModelRequirements(model)
      .then((req) => {
        if (!cancelled) setRequirements(req);
      })
      .catch(() => {
        if (!cancelled) setRequirements(undefined);
      });
    return () => {
      cancelled = true;
    };
  }, [model]);

  const currentSpend = settings?.current_spend_usd ?? 0;
  const maxSpendValue = Number(maxSpend);
  const spendPercent = maxSpendValue > 0 ? Math.min(100, (currentSpend / maxSpendValue) * 100) : 0;
  const spendTone =
    spendPercent >= 90
      ? '[&_[data-slot=progress-indicator]]:bg-destructive'
      : spendPercent >= 70
      ? '[&_[data-slot=progress-indicator]]:bg-yellow-500'
      : '[&_[data-slot=progress-indicator]]:bg-primary';

  // The API-key fields to render: every configured provider plus any key the
  // selected model requires (so a newly-needed provider's field appears).
  const keyFields = useMemo<KeyField[]>(() => {
    const fields: KeyField[] = [];
    const seen = new Set<string>();
    for (const [env, status] of Object.entries(settings?.api_keys || {})) {
      fields.push({ env, label: status.label, status });
      seen.add(env);
    }
    for (const req of requirements?.required_keys || []) {
      if (!seen.has(req.env)) {
        fields.push({ env: req.env, label: req.label });
        seen.add(req.env);
      }
    }
    return fields;
  }, [settings, requirements]);

  const requiredKeys = requirements?.required_keys || [];
  // A model's key requirement is a group: any one of the acceptable env vars
  // satisfies it (e.g. Gemini via GOOGLE_API_KEY or GEMINI_API_KEY).
  const keyMissing =
    requiredKeys.length > 0 &&
    !requiredKeys.some((k) => (k.configured && !clearedKeys[k.env]) || apiKeys[k.env]?.trim());
  // Only badge fields "Required" while the requirement is actually unmet.
  const requiredEnvs = useMemo(
    () => new Set(keyMissing ? requiredKeys.map((k) => k.env) : []),
    [keyMissing, requiredKeys],
  );

  const submit = async () => {
    setError(undefined);
    setFieldErrors({});
    setSaved(false);
    const localErrors = validateBeforeSave(keyFields, apiKeys, keyMissing, requiredKeys[0], model);
    if (Object.keys(localErrors).length > 0) {
      setFieldErrors(localErrors);
      setError(Object.values(localErrors)[0]);
      return;
    }
    setIsSaving(true);
    try {
      const apiKeyUpdates: NonNullable<SettingsUpdate['api_keys']> = {};
      for (const field of keyFields) {
        const value = (apiKeys[field.env] || '').trim();
        if (value) {
          apiKeyUpdates[field.env] = { value };
        } else if (clearedKeys[field.env]) {
          apiKeyUpdates[field.env] = { clear: true };
        }
      }
      const update: SettingsUpdate = {
        model,
        permission_tier: permissionTier,
        theorem_translation_max_retries: theoremTranslationRetries,
        max_turns: maxTurns,
        max_spend_usd: maxSpend.trim() ? Number(maxSpend) : null,
        api_keys: Object.keys(apiKeyUpdates).length ? apiKeyUpdates : undefined,
      };
      const savedSettings = await saveSettings(update);
      setSettings(savedSettings);
      setModel(savedSettings.model);
      setPermissionTier(savedSettings.permission_tier);
      setTheoremTranslationRetries(savedSettings.theorem_translation_max_retries);
      setMaxTurns(savedSettings.max_turns ?? 20);
      setMaxSpend(savedSettings.max_spend_usd == null ? '' : String(savedSettings.max_spend_usd));
      setApiKeys({});
      setClearedKeys({});
      setFieldErrors({});
      setSaved(true);
      window.setTimeout(() => setSaved(false), 1800);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Unable to save settings.';
      const field = err instanceof Error ? (err as Error & { field?: string }).field : undefined;
      if (field) {
        setFieldErrors({ [field]: message });
      }
      setError(message);
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="min-h-full bg-background">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-5 py-8">
        <div className="flex items-start justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <Settings className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <h1 className="text-xl font-semibold text-foreground">Settings</h1>
              <p className="text-sm text-muted-foreground">Configure models, approval behavior, and run limits.</p>
            </div>
          </div>
          <button
            type="button"
            onClick={onBack}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            aria-label="Close settings"
            title="Close settings"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {error && (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-foreground">
            {error}
          </div>
        )}

        {isLoading ? (
          <div className="rounded-md border border-border bg-card px-4 py-8 text-center text-sm text-muted-foreground">
            Loading settings...
          </div>
        ) : (
          <div className="rounded-md border border-border bg-card">
            <Section
              title="Model"
              description="Backend model used for proof formalization. Search the full provider catalog or type any model ID — you'll be prompted for that provider's API key below."
            >
              <ModelCombobox
                value={model}
                onChange={setModel}
                catalog={catalog}
                featured={settings?.model_options || []}
              />
            </Section>

            <Separator />

            <Section title="API Keys" description="Configured keys stay in the local backend config. The key for the selected model is required.">
              <div className="space-y-4">
                {keyFields.map((field) => {
                  const status = field.status;
                  const configured = Boolean(status?.configured) && !clearedKeys[field.env];
                  const placeholder = KEY_PLACEHOLDERS[field.env] || 'Enter API key';
                  return (
                    <div key={field.env} className="space-y-2">
                      <div className="flex items-center justify-between gap-3">
                        <Label htmlFor={`${field.env}-api-key`} className="flex items-center gap-2">
                          {field.label}
                          {requiredEnvs.has(field.env) && (
                            <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium uppercase text-primary">
                              Required
                            </span>
                          )}
                        </Label>
                        {configured && (
                          <span className="flex items-center gap-1 text-xs text-muted-foreground">
                            <KeyRound className="h-3.5 w-3.5" />
                            Saved{status?.last4 ? ` ...${status.last4}` : ''}
                          </span>
                        )}
                      </div>
                      <div className="flex gap-2">
                        <div className="relative flex-1">
                          <Input
                            id={`${field.env}-api-key`}
                            type={visibleKeys[field.env] ? 'text' : 'password'}
                            value={apiKeys[field.env] || ''}
                            placeholder={configured ? 'Enter a new key to replace the saved key' : placeholder}
                            onChange={(event) =>
                              setApiKeys((current) => ({ ...current, [field.env]: event.target.value }))
                            }
                            className="pr-10 font-mono"
                          />
                          <button
                            type="button"
                            onClick={() =>
                              setVisibleKeys((current) => ({ ...current, [field.env]: !current[field.env] }))
                            }
                            className="absolute right-2 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
                            aria-label={`Toggle ${field.label} key visibility`}
                            title={`Toggle ${field.label} key visibility`}
                          >
                            {visibleKeys[field.env] ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                          </button>
                        </div>
                        {status?.configured && (
                          <button
                            type="button"
                            onClick={() =>
                              setClearedKeys((current) => ({ ...current, [field.env]: !current[field.env] }))
                            }
                            className={[
                              'flex h-9 w-9 items-center justify-center rounded-md border transition-colors',
                              clearedKeys[field.env]
                                ? 'border-destructive/40 bg-destructive/10 text-destructive'
                                : 'border-border text-muted-foreground hover:bg-accent hover:text-foreground',
                            ].join(' ')}
                            aria-label={clearedKeys[field.env] ? `Keep ${field.label} key` : `Clear ${field.label} key`}
                            title={clearedKeys[field.env] ? `Keep ${field.label} key` : `Clear ${field.label} key`}
                          >
                            {clearedKeys[field.env] ? <RotateCcw className="h-4 w-4" /> : <X className="h-4 w-4" />}
                          </button>
                        )}
                      </div>
                      {fieldErrors[`api_keys.${field.env}`] && (
                        <p className="text-xs text-destructive">{fieldErrors[`api_keys.${field.env}`]}</p>
                      )}
                      {clearedKeys[field.env] && (
                        <p className="text-xs text-destructive">This saved key will be removed on save.</p>
                      )}
                    </div>
                  );
                })}
              </div>
            </Section>

            <Separator />

            <Section title="Permission Tier" description="Controls where the agent pauses for approval.">
              <RadioGroup
                value={permissionTier}
                onValueChange={(value) => setPermissionTier(value as PermissionTier)}
                className="gap-2"
              >
                {(settings?.permission_tiers || []).map((tier) => {
                  const detail = PERMISSION_DETAILS[tier.value];
                  return (
                    <Label
                      key={tier.value}
                      className={[
                        'flex cursor-pointer items-start gap-3 rounded-md border p-3 transition-colors',
                        permissionTier === tier.value ? 'border-primary bg-accent' : 'border-border hover:bg-accent/70',
                      ].join(' ')}
                    >
                      <RadioGroupItem value={tier.value} className="mt-0.5" />
                      <span className="min-w-0">
                        <span className="block text-sm font-medium text-foreground">{detail.title}</span>
                        <span className="mt-1 block text-sm font-normal text-muted-foreground">{detail.description}</span>
                      </span>
                    </Label>
                  );
                })}
              </RadioGroup>
            </Section>

            <Separator />

            <Section
              title="Theorem Translation Retries"
              description="Internal attempts to produce a checked Lean theorem statement."
            >
              <Input
                type="number"
                min={1}
                step={1}
                value={theoremTranslationRetries}
                disabled={permissionTier !== 'theorem_translation'}
                onChange={(event) => setTheoremTranslationRetries(toPositiveInteger(event.target.value))}
                aria-label="Theorem translation retry attempts"
                className="w-32 disabled:cursor-not-allowed disabled:opacity-50"
              />
            </Section>

            <Separator />

            <Section title="Max Turns" description="Maximum turns the agent may take on a proof attempt.">
              <div className="flex items-center gap-4">
                <Slider
                  min={1}
                  max={100}
                  step={1}
                  value={[maxTurns]}
                  onValueChange={([value]) => setMaxTurns(value)}
                  className="flex-1"
                />
                <Input
                  type="number"
                  min={1}
                  max={100}
                  value={maxTurns}
                  onChange={(event) =>
                    setMaxTurns(Math.min(100, Math.max(1, Number(event.target.value) || 1)))
                  }
                  className="w-24"
                />
              </div>
            </Section>

            <Separator />

            <Section title="Max Spend" description="Global spend cap across recorded runs.">
              <div className="space-y-4">
                <div className="flex items-center gap-3">
                  <div className="relative flex-1">
                    <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground">
                      $
                    </span>
                    <Input
                      type="number"
                      min={0}
                      step="0.01"
                      value={maxSpend}
                      onChange={(event) => setMaxSpend(event.target.value)}
                      placeholder="No cap"
                      className="pl-7"
                    />
                  </div>
                  <div className="w-36 text-right text-sm text-muted-foreground">
                    ${currentSpend.toFixed(2)} used
                  </div>
                </div>
                <div className="space-y-2">
                  <Progress value={spendPercent} className={spendTone} />
                  <div className="flex justify-between text-xs text-muted-foreground">
                    <span>{spendPercent.toFixed(0)}% of cap</span>
                    <span>{maxSpendValue > 0 ? `$${maxSpendValue.toFixed(2)}` : 'No cap set'}</span>
                  </div>
                </div>
              </div>
            </Section>

            <div className="flex items-center justify-end gap-3 border-t border-border p-5">
              {keyMissing && requiredKeys[0] && (
                <span className="text-sm text-muted-foreground">{requiredKeys[0].label} key required for this model.</span>
              )}
              {saved && (
                <span className="flex items-center gap-1 text-sm text-green-600">
                  <Check className="h-4 w-4" />
                  Saved
                </span>
              )}
              <button
                type="button"
                onClick={submit}
                disabled={isSaving || !model}
                className="flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
              >
                <Save className="h-4 w-4" />
                {isSaving ? 'Saving...' : 'Save Settings'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="grid gap-4 p-5 md:grid-cols-[minmax(160px,220px)_1fr]">
      <div>
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
        <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{description}</p>
      </div>
      <div className="min-w-0">{children}</div>
    </section>
  );
}

function toPositiveInteger(value: string): number {
  const parsed = Math.trunc(Number(value));
  return Number.isFinite(parsed) && parsed >= 1 ? parsed : 1;
}

function validateBeforeSave(
  keyFields: KeyField[],
  apiKeys: Record<string, string>,
  keyMissing: boolean,
  firstRequired: { env: string; label: string } | undefined,
  model: string,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of keyFields) {
    const value = (apiKeys[field.env] || '').trim();
    const pattern = KEY_PATTERNS[field.env];
    if (value && pattern && !pattern.test(value)) {
      errors[`api_keys.${field.env}`] = `The ${field.label} API key does not look valid. Check the key and try again.`;
    }
  }
  if (keyMissing && firstRequired) {
    errors[`api_keys.${firstRequired.env}`] = `Add a ${firstRequired.label} API key before saving ${model}.`;
  }
  return errors;
}
