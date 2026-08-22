import { useState, useEffect, useCallback } from 'react';
import type { ReactNode } from 'react';
import {
  Bot,
  Check,
  Copy,
  KeyRound,
  Mic,
  PlugZap,
  RefreshCw,
  Save,
  Share2,
  ShieldCheck,
  X,
} from 'lucide-react';
import {
  createInvite,
  getCodexAuthStatus,
  getAudioSupport,
  getLlmSettings,
  getMcpSettings,
  installAudioSupport,
  listInvites,
  startCodexDeviceLogin,
  updateLlmSettings,
  type AudioSupportInstallResult,
  type AudioSupportStatus,
  type CodexAuthStatus,
  type CodexDeviceLogin,
  type InviteToken,
  type LlmSettings,
  type McpSettings,
} from '../api';
import { Button } from '../components/ui/Button';
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Input } from '../components/ui/Input';
import VaultRepair from '../surfaces/VaultRepair';
import CodeRepos from '../surfaces/CodeRepos';

const PROVIDER_HELP: Record<string, string> = {
  anthropic: 'Direct Anthropic API calls. Best default when you have an API key configured.',
  openrouter: 'Use OpenRouter to switch between hosted models from one account.',
  openai_compat: 'Use any OpenAI-compatible endpoint such as OpenAI, Together, Fireworks, or Azure.',
  ollama: 'Use a local or self-hosted Ollama endpoint. Best for private, lower-cost local runs.',
  codex_cli: 'Use Codex CLI on this server for answer generation.',
  claude_cli: 'Use Claude Code on this server for answer generation.',
};

export default function SettingsPage() {
  const [invites, setInvites] = useState<InviteToken[]>([]);
  const [audioSupport, setAudioSupport] = useState<AudioSupportStatus | null>(null);
  const [llmSettings, setLlmSettings] = useState<LlmSettings | null>(null);
  const [mcpSettings, setMcpSettings] = useState<McpSettings | null>(null);
  const [llmDraft, setLlmDraft] = useState({
    llm_extraction_provider: 'ollama',
    llm_synthesis_provider: 'ollama',
    llm_model: '',
    llm_synthesis_model: '',
    ollama_base_url: '',
    ollama_api_key: '',
  });
  const [loading, setLoading] = useState(true);
  const [audioLoading, setAudioLoading] = useState(true);
  const [audioInstalling, setAudioInstalling] = useState(false);
  const [audioInstallResult, setAudioInstallResult] =
    useState<AudioSupportInstallResult | null>(null);
  const [llmLoading, setLlmLoading] = useState(true);
  const [mcpLoading, setMcpLoading] = useState(true);
  const [llmSaving, setLlmSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [audioError, setAudioError] = useState<string | null>(null);
  const [llmError, setLlmError] = useState<string | null>(null);
  const [mcpError, setMcpError] = useState<string | null>(null);
  const [llmSaved, setLlmSaved] = useState(false);
  const [mcpCopied, setMcpCopied] = useState(false);
  const [codexAuth, setCodexAuth] = useState<CodexAuthStatus | null>(null);
  const [codexLogin, setCodexLogin] = useState<CodexDeviceLogin | null>(null);
  const [codexAuthLoading, setCodexAuthLoading] = useState(false);
  const [codexAuthError, setCodexAuthError] = useState<string | null>(null);

  const [role, setRole] = useState<'viewer' | 'collaborator'>('viewer');
  const [expiryDays, setExpiryDays] = useState<number | null>(7);
  const [generating, setGenerating] = useState(false);
  const [generatedUrl, setGeneratedUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const fetchInvites = useCallback(async () => {
    try {
      const data = await listInvites();
      setInvites(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load invite links');
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchAudioSupport = useCallback(async () => {
    setAudioLoading(true);
    setAudioError(null);
    try {
      const data = await getAudioSupport();
      setAudioSupport(data);
    } catch (e) {
      setAudioError(e instanceof Error ? e.message : 'Failed to check media import support');
    } finally {
      setAudioLoading(false);
    }
  }, []);

  const fetchLlmSettings = useCallback(async () => {
    setLlmLoading(true);
    setLlmError(null);
    try {
      const [data, auth] = await Promise.all([
        getLlmSettings(),
        getCodexAuthStatus().catch(() => null),
      ]);
      setLlmSettings(data);
      setCodexAuth(auth);
      setLlmDraft({
        llm_extraction_provider: data.llm_extraction_provider,
        llm_synthesis_provider: data.llm_synthesis_provider,
        llm_model: data.llm_model,
        llm_synthesis_model: data.llm_synthesis_model,
        ollama_base_url: data.ollama_base_url,
        ollama_api_key: '',
      });
    } catch (e) {
      setLlmError(e instanceof Error ? e.message : 'Failed to load model settings');
    } finally {
      setLlmLoading(false);
    }
  }, []);

  async function handleStartCodexAuth() {
    setCodexAuthLoading(true);
    setCodexAuthError(null);
    setCodexLogin(null);
    try {
      const login = await startCodexDeviceLogin();
      setCodexLogin(login);
    } catch (e) {
      setCodexAuthError(e instanceof Error ? e.message : 'Failed to start Codex sign-in');
    } finally {
      setCodexAuthLoading(false);
    }
  }

  const fetchMcpSettings = useCallback(async () => {
    setMcpLoading(true);
    setMcpError(null);
    try {
      setMcpSettings(await getMcpSettings());
    } catch (e) {
      setMcpError(e instanceof Error ? e.message : 'Failed to load agent access settings');
    } finally {
      setMcpLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchInvites();
    fetchAudioSupport();
    fetchLlmSettings();
    fetchMcpSettings();
  }, [fetchAudioSupport, fetchInvites, fetchLlmSettings, fetchMcpSettings]);

  async function handleGenerate() {
    setGenerating(true);
    setGeneratedUrl(null);
    setError(null);
    try {
      const result = await createInvite(role, expiryDays);
      const url = `${window.location.origin}${result.url}`;
      setGeneratedUrl(url);
      await fetchInvites();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create invite link');
    } finally {
      setGenerating(false);
    }
  }

  async function handleCopy() {
    if (!generatedUrl) return;
    await navigator.clipboard.writeText(generatedUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  async function handleCopyMcpConfig() {
    if (!mcpSettings) return;
    await navigator.clipboard.writeText(JSON.stringify(mcpSettings.client_config, null, 2));
    setMcpCopied(true);
    setTimeout(() => setMcpCopied(false), 2000);
  }

  async function handleSaveLlmSettings() {
    setLlmSaving(true);
    setLlmSaved(false);
    setLlmError(null);
    try {
      const next = await updateLlmSettings({
        llm_extraction_provider: llmDraft.llm_extraction_provider,
        llm_synthesis_provider: llmDraft.llm_synthesis_provider,
        llm_model: llmDraft.llm_model,
        llm_synthesis_model: llmDraft.llm_synthesis_model,
        ollama_base_url: llmDraft.ollama_base_url,
        ollama_api_key: llmDraft.ollama_api_key ? llmDraft.ollama_api_key : null,
      });
      setLlmSettings(next);
      setLlmDraft((draft) => ({ ...draft, ollama_api_key: '' }));
      setLlmSaved(true);
      setTimeout(() => setLlmSaved(false), 2400);
    } catch (e) {
      setLlmError(e instanceof Error ? e.message : 'Failed to save model settings');
    } finally {
      setLlmSaving(false);
    }
  }

  async function handleInstallAudioSupport() {
    setAudioInstalling(true);
    setAudioError(null);
    setAudioInstallResult(null);
    try {
      const result = await installAudioSupport();
      setAudioInstallResult(result);
      setAudioSupport(result.status);
    } catch (e) {
      setAudioError(e instanceof Error ? e.message : 'Failed to enable media transcription');
    } finally {
      setAudioInstalling(false);
    }
  }

  function formatDate(iso: string | null) {
    if (!iso) return 'Never';
    try {
      return new Date(iso).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return iso;
    }
  }

  function isExpired(iso: string | null) {
    if (!iso) return false;
    return new Date(iso) < new Date();
  }

  const mediaStatus = audioSupport?.video_available
    ? 'Audio and video imports are ready.'
    : audioSupport?.audio_available
      ? 'Audio imports are ready. Video still needs ffmpeg.'
      : 'Text, docs, code, data, email, and archives are ready. Media transcription is optional.';

  return (
    <div className="page-frame bg-transparent">
      <div className="page-header">
        <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.2em] text-muted-foreground">
          Settings
        </p>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div className="min-w-0">
            <h1 className="text-3xl font-semibold tracking-tight text-foreground">Workspace Settings</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">
              Configure the parts that affect daily use: models, media import, agent access, and sharing.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <StatusPill label="Models" ready={Boolean(llmSettings?.llm_synthesis_model)} />
            <StatusPill label="Agents" ready={Boolean(mcpSettings?.api_key_configured)} />
            <StatusPill label="Media" ready={Boolean(audioSupport?.audio_available)} />
          </div>
        </div>
      </div>

      <div className="grid min-h-0 gap-4 overflow-y-auto xl:grid-cols-2">
        <section className="space-y-4">
          <SettingsCard
            icon={<Bot className="h-4 w-4" />}
            title="AI Models"
            description="Choose the providers used to extract structure from sources and answer questions with citations."
            badge={llmSettings?.ollama_api_key_configured ? `Ollama key ${llmSettings.ollama_api_key_masked}` : undefined}
          >
            {llmLoading ? (
              <LoadingText>Loading model settings...</LoadingText>
            ) : (
              <div className="space-y-4">
                <div className="grid gap-4 md:grid-cols-2">
                  <ProviderField
                    label="Extraction"
                    value={llmDraft.llm_extraction_provider}
                    onChange={(value) => setLlmDraft((draft) => ({ ...draft, llm_extraction_provider: value }))}
                  />
                  <ProviderField
                    label="Answers"
                    value={llmDraft.llm_synthesis_provider}
                    onChange={(value) => setLlmDraft((draft) => ({ ...draft, llm_synthesis_provider: value }))}
                  />
                </div>

                <div className="grid gap-4 md:grid-cols-2">
                  <TextField
                    label="Extraction model"
                    help="Used while importing sources and proposing memory."
                    value={llmDraft.llm_model}
                    onChange={(value) => setLlmDraft((draft) => ({ ...draft, llm_model: value }))}
                  />
                  <TextField
                    label="Answer model"
                    help="Used for cited Q&A in Search and Query."
                    value={llmDraft.llm_synthesis_model}
                    onChange={(value) => setLlmDraft((draft) => ({ ...draft, llm_synthesis_model: value }))}
                  />
                </div>

                <div className="soft-border rounded-[8px] border bg-white/[0.03] p-3">
                  <p className="text-sm font-medium text-foreground">Local and subscription-backed providers</p>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">
                    Ollama uses a local endpoint. Codex CLI and Claude Code run non-interactively on this server when installed and authenticated.
                  </p>
                  <div className="mt-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(180px,0.65fr)]">
                    <TextField
                      label="Ollama base URL"
                      value={llmDraft.ollama_base_url}
                      onChange={(value) => setLlmDraft((draft) => ({ ...draft, ollama_base_url: value }))}
                    />
                    <TextField
                      label="Ollama API key"
                      value={llmDraft.ollama_api_key}
                      type="password"
                      placeholder={llmSettings?.ollama_api_key_configured ? 'Leave blank to keep existing key' : ''}
                      onChange={(value) => setLlmDraft((draft) => ({ ...draft, ollama_api_key: value }))}
                    />
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <DependencyBadge label="Codex CLI" ready={Boolean(llmSettings?.cli_providers?.codex_cli?.available)} />
                    <DependencyBadge label="Codex signed in" ready={Boolean(codexAuth?.authenticated)} />
                    <DependencyBadge label="Claude Code" ready={Boolean(llmSettings?.cli_providers?.claude_cli?.available)} />
                  </div>
                  <div className="mt-3 space-y-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={handleStartCodexAuth}
                        disabled={codexAuthLoading || !llmSettings?.cli_providers?.codex_cli?.available}
                      >
                        <KeyRound className="h-3.5 w-3.5" />
                        {codexAuthLoading ? 'Starting sign-in...' : 'Sign in to Codex'}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={fetchLlmSettings}
                        disabled={llmLoading}
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                        Check sign-in
                      </Button>
                    </div>
                    {codexAuth?.detail && (
                      <p className="text-xs leading-5 text-muted-foreground">{codexAuth.detail}</p>
                    )}
                    {codexLogin && (
                      <div className="soft-border rounded-[8px] border bg-white/[0.03] p-3 text-xs leading-5 text-muted-foreground">
                        <p>
                          Open{' '}
                          <a
                            className="text-primary underline-offset-2 hover:underline"
                            href={codexLogin.url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {codexLogin.url}
                          </a>
                        </p>
                        <p className="mt-1 font-mono text-sm text-foreground">{codexLogin.code}</p>
                        <p className="mt-1">{codexLogin.detail}</p>
                      </div>
                    )}
                    {codexAuthError && <InlineError>{codexAuthError}</InlineError>}
                  </div>
                </div>

                {llmError && <InlineError>{llmError}</InlineError>}
                {llmSaved && <InlineSuccess>Model settings saved.</InlineSuccess>}

                <Button onClick={handleSaveLlmSettings} disabled={llmSaving} size="sm">
                  <Save className="h-3.5 w-3.5" />
                  {llmSaving ? 'Saving...' : 'Save model settings'}
                </Button>
              </div>
            )}
          </SettingsCard>

          <SettingsCard
            icon={<Mic className="h-4 w-4" />}
            title="Media Import"
            description="Enable local transcription for audio and video. Other supported file types work without this."
            action={
              <Button
                onClick={fetchAudioSupport}
                disabled={audioLoading}
                variant="outline"
                size="sm"
                title="Refresh media import status"
              >
                <RefreshCw className={['h-3.5 w-3.5', audioLoading ? 'animate-spin' : ''].join(' ')} />
                Refresh
              </Button>
            }
          >
            {audioLoading ? (
              <LoadingText>Checking media support...</LoadingText>
            ) : audioError ? (
              <InlineError>{audioError}</InlineError>
            ) : audioSupport ? (
              <div className="space-y-4">
                <div className="flex flex-wrap items-center gap-2">
                  {audioSupport.available ? (
                    <Badge variant="success" className="text-xs">Ready</Badge>
                  ) : (
                    <Badge variant="secondary" className="text-xs">Optional setup</Badge>
                  )}
                  <DependencyBadge label="Whisper" ready={audioSupport.dependencies.openai_whisper} />
                  <DependencyBadge label="ffmpeg" ready={audioSupport.dependencies.ffmpeg} />
                  {audioSupport.audio_available && !audioSupport.video_available && (
                    <Badge variant="secondary" className="text-xs">Audio only</Badge>
                  )}
                </div>

                <p className="text-sm leading-6 text-muted-foreground">{mediaStatus}</p>

                {!audioSupport.available && (
                  <div className="soft-border space-y-3 rounded-[8px] border bg-white/[0.03] p-3">
                    <p className="text-sm font-medium text-foreground">
                      Missing: {audioSupport.missing.join(', ') || 'none'}
                    </p>
                    <p className="text-xs leading-5 text-muted-foreground">
                      Use this when you want uploaded audio or video to become searchable notes.
                    </p>
                    <Button onClick={handleInstallAudioSupport} disabled={audioInstalling} size="sm">
                      <Mic className="h-3.5 w-3.5" />
                      {audioInstalling ? 'Enabling...' : 'Enable media transcription'}
                    </Button>
                  </div>
                )}

                {audioInstallResult && (
                  <div className="soft-border rounded-[8px] border bg-white/[0.03] p-3">
                    <p className="text-sm font-medium text-foreground">
                      {audioInstallResult.ok ? 'Media transcription is ready.' : 'Media transcription still needs attention.'}
                    </p>
                    <div className="mt-2 space-y-1">
                      {audioInstallResult.actions.map((action) => (
                        <div key={action.name} className="flex items-start gap-2 text-xs">
                          <DependencyBadge label={action.name} ready={action.status !== 'failed'} />
                          <span className="min-w-0 flex-1 break-words text-muted-foreground">
                            {action.status === 'failed'
                              ? action.detail
                              : action.status === 'already_available'
                                ? 'Already available.'
                                : 'Installed successfully.'}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : null}
          </SettingsCard>
        </section>

        <section className="space-y-4">
          <SettingsCard
            icon={<Bot className="h-4 w-4" />}
            title="Code memory"
            description="Index a repository so your code, and the decisions behind it, live in the same graph as everything else."
          >
            <CodeRepos />
          </SettingsCard>

          <SettingsCard
            icon={<RefreshCw className="h-4 w-4" />}
            title="Vault repair"
            description="Bring the indexes and your agents' memory back in line with what is on disk."
          >
            <VaultRepair />
          </SettingsCard>

          <SettingsCard
            icon={<PlugZap className="h-4 w-4" />}
            title="Agent Access"
            description="Copy a ready-to-use MCP configuration for local coding agents and assistants."
            badge={mcpSettings?.api_key_configured ? 'Bearer auth on' : 'Bearer auth off'}
          >
            {mcpLoading ? (
              <LoadingText>Checking agent access...</LoadingText>
            ) : mcpError ? (
              <InlineError>{mcpError}</InlineError>
            ) : mcpSettings ? (
              <div className="space-y-3">
                <div className="soft-border rounded-[8px] border bg-white/[0.03] p-3">
                  <p className="text-xs font-semibold uppercase tracking-[0.14em] text-zinc-500">Endpoint</p>
                  <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{mcpSettings.endpoint}</p>
                  <p className="mt-2 text-xs leading-5 text-muted-foreground">
                    {mcpSettings.auth_required
                      ? 'Clients must send the configured MCP API key as a bearer token.'
                      : 'No bearer token is required. Enable MCP_API_KEY for exposed deployments.'}
                  </p>
                </div>
                <Button type="button" variant="outline" size="sm" onClick={handleCopyMcpConfig}>
                  <Copy className="h-3.5 w-3.5" />
                  {mcpCopied ? 'Copied config' : 'Copy agent config'}
                </Button>
              </div>
            ) : null}
          </SettingsCard>

          <SettingsCard
            icon={<Share2 className="h-4 w-4" />}
            title="Sharing"
            description="Create scoped links for viewers or collaborators. Existing links remain listed below."
          >
            <div className="space-y-4">
              <div className="grid gap-4">
                <SegmentedControl
                  label="Role"
                  value={role}
                  options={[
                    { label: 'Viewer', value: 'viewer' },
                    { label: 'Collaborator', value: 'collaborator' },
                  ]}
                  onChange={(value) => setRole(value as 'viewer' | 'collaborator')}
                />
                <SegmentedControl
                  label="Expires"
                  value={String(expiryDays)}
                  options={[
                    { label: '7 days', value: '7' },
                    { label: '30 days', value: '30' },
                    { label: 'Never', value: 'null' },
                  ]}
                  onChange={(value) => setExpiryDays(value === 'null' ? null : Number(value))}
                />
              </div>

              <Button onClick={handleGenerate} disabled={generating} size="sm">
                <KeyRound className="h-3.5 w-3.5" />
                {generating ? 'Creating...' : 'Create invite link'}
              </Button>

              {error && <InlineError>{error}</InlineError>}

              {generatedUrl && (
                <div className="flex items-center gap-2">
                  <input
                    readOnly
                    value={generatedUrl}
                    aria-label="Generated invite link"
                    className="soft-border h-8 min-w-0 flex-1 rounded-[5px] border bg-white/[0.05] px-3 font-mono text-xs text-muted-foreground focus:outline-none"
                  />
                  <Button onClick={handleCopy} variant="outline" size="sm">
                    {copied ? 'Copied' : 'Copy'}
                  </Button>
                </div>
              )}

              <InviteList
                loading={loading}
                invites={invites}
                isExpired={isExpired}
                formatDate={formatDate}
              />
            </div>
          </SettingsCard>
        </section>
      </div>
    </div>
  );
}

function SettingsCard({
  icon,
  title,
  description,
  badge,
  action,
  children,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  badge?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card>
      <CardHeader className="p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-base">
              <span className="text-primary">{icon}</span>
              {title}
            </CardTitle>
            <p className="mt-1 max-w-xl text-xs leading-5 text-muted-foreground">{description}</p>
          </div>
          {action ?? (badge ? <Badge variant="secondary" className="shrink-0 text-xs">{badge}</Badge> : null)}
        </div>
      </CardHeader>
      <CardContent className="p-5 pt-0">{children}</CardContent>
    </Card>
  );
}

function StatusPill({ label, ready }: { label: string; ready: boolean }) {
  return (
    <span className="soft-border inline-flex items-center gap-2 rounded-[5px] border bg-white/[0.04] px-3 py-1.5 text-xs text-muted-foreground">
      {ready ? <ShieldCheck className="h-3.5 w-3.5 text-emerald-300" /> : <CircleDot />}
      {label}
    </span>
  );
}

function CircleDot() {
  return <span className="h-2 w-2 rounded-full bg-zinc-500" aria-hidden="true" />;
}

function LoadingText({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>;
}

function InlineError({ children }: { children: ReactNode }) {
  return <p className="text-xs leading-5 text-destructive">{children}</p>;
}

function InlineSuccess({ children }: { children: ReactNode }) {
  return <p className="text-xs leading-5 text-emerald-300">{children}</p>;
}

function DependencyBadge({ label, ready }: { label: string; ready: boolean }) {
  return (
    <span className="soft-border inline-flex items-center gap-1 rounded-[5px] border bg-white/[0.03] px-2 py-1 text-xs text-muted-foreground">
      {ready ? <Check className="h-3 w-3 text-emerald-300" /> : <X className="h-3 w-3 text-destructive" />}
      {label}
    </span>
  );
}

function ProviderField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="space-y-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="soft-border h-9 w-full rounded-[5px] border bg-white/[0.05] px-3 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
      >
        <option value="anthropic">Anthropic API</option>
        <option value="openrouter">OpenRouter</option>
        <option value="openai_compat">OpenAI-compatible</option>
        <option value="ollama">Ollama / local</option>
        {label === 'Answers' && <option value="codex_cli">Codex CLI on server</option>}
        {label === 'Answers' && <option value="claude_cli">Claude Code on server</option>}
      </select>
      <p className="text-xs leading-5 text-muted-foreground">{PROVIDER_HELP[value] ?? 'Use the configured provider for this stage.'}</p>
    </label>
  );
}

function TextField({
  label,
  value,
  onChange,
  type = 'text',
  placeholder,
  help,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  placeholder?: string;
  help?: string;
}) {
  return (
    <label className="space-y-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <Input
        value={value}
        type={type}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="h-9 text-sm"
      />
      {help && <p className="text-xs leading-5 text-muted-foreground">{help}</p>}
    </label>
  );
}

function SegmentedControl({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { label: string; value: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <div className="section-tabs">
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            onClick={() => onChange(option.value)}
            className={['section-tab', value === option.value ? 'section-tab-active' : ''].join(' ')}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function InviteList({
  loading,
  invites,
  isExpired,
  formatDate,
}: {
  loading: boolean;
  invites: InviteToken[];
  isExpired: (iso: string | null) => boolean;
  formatDate: (iso: string | null) => string;
}) {
  if (loading) return <LoadingText>Loading invite links...</LoadingText>;
  if (invites.length === 0) {
    return <p className="text-sm text-muted-foreground">No invite links yet.</p>;
  }
  return (
    <div className="space-y-2">
      {invites.map((invite) => {
        const expired = isExpired(invite.expires_at);
        return (
          <div
            key={invite.id}
            className="subtle-divider flex items-center justify-between gap-3 border-b py-2 last:border-0"
          >
            <code className="truncate font-mono text-xs text-muted-foreground">
              {invite.token.slice(0, 16)}...
            </code>
            <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
              <Badge variant="outline" className="text-xs">{invite.role}</Badge>
              {invite.used ? (
                <Badge variant="secondary" className="text-xs">Used</Badge>
              ) : expired ? (
                <Badge variant="destructive" className="text-xs">Expired</Badge>
              ) : (
                <Badge variant="success" className="text-xs">Active</Badge>
              )}
              <span className="whitespace-nowrap text-xs text-muted-foreground">
                Expires {formatDate(invite.expires_at)}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
