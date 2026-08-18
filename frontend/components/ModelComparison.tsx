// Directory: deep-research/frontend/components/ModelComparison.tsx
/**
 * Run one question across several providers and compare them.
 *
 * Rewritten against the job API. The previous version POSTed to
 * `/research/compare` -- a bespoke server endpoint -- and required the user to
 * paste a provider key per model into the browser. Both are gone:
 *
 *   - Comparison is a *client-side* concern. N parallel `POST /v1/research`
 *     calls do the same job, and every provider runs the identical pipeline.
 *   - Keys live in the backend, so this component just asks which providers are
 *     configured and offers those.
 */

'use client'

import { useCallback, useEffect, useState } from 'react'
import { BarChart3, Loader2, AlertCircle, CheckCircle2, XCircle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { getJob, listProviders, startResearch, type Job } from '@/lib/deepResearch'

interface ProviderOption {
  id: string
  label: string
  configured: boolean
  default_model: string
}

interface Run {
  provider: string
  jobId?: string
  job?: Job
  error?: string
  state: 'pending' | 'running' | 'done' | 'failed'
}

const POLL_MS = 2500
const MAX_POLLS = 240 // ~10 minutes

const ModelComparison = () => {
  const [providers, setProviders] = useState<ProviderOption[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [query, setQuery] = useState('')
  const [runs, setRuns] = useState<Record<string, Run>>({})
  const [isRunning, setIsRunning] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    listProviders()
      .then((list) => {
        const options = list as ProviderOption[]
        setProviders(options)
        // Preselect what can actually run, so the tab is usable immediately.
        setSelected(options.filter((p) => p.configured).map((p) => p.id).slice(0, 3))
      })
      .catch((e: Error) => setLoadError(e.message))
  }, [])

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id]))

  const run = useCallback(async () => {
    if (!query.trim() || selected.length === 0 || isRunning) return
    setIsRunning(true)
    setRuns(Object.fromEntries(selected.map((p) => [p, { provider: p, state: 'pending' as const }])))

    // Fire every provider at once; the backend caps real concurrency.
    await Promise.all(
      selected.map(async (provider) => {
        try {
          const jobId = await startResearch({ query: query.trim(), provider })
          setRuns((prev) => ({ ...prev, [provider]: { provider, jobId, state: 'running' } }))

          for (let i = 0; i < MAX_POLLS; i++) {
            await new Promise((r) => setTimeout(r, POLL_MS))
            const job = await getJob(jobId)
            if (job.status === 'succeeded' || job.status === 'failed' || job.status === 'cancelled') {
              setRuns((prev) => ({
                ...prev,
                [provider]: {
                  provider,
                  jobId,
                  job,
                  state: job.status === 'succeeded' ? 'done' : 'failed',
                  error: job.error?.message
                }
              }))
              return
            }
          }

          setRuns((prev) => ({
            ...prev,
            [provider]: { provider, jobId, state: 'failed', error: 'Timed out waiting for the run.' }
          }))
        } catch (e) {
          setRuns((prev) => ({
            ...prev,
            [provider]: { provider, state: 'failed', error: (e as Error).message }
          }))
        }
      })
    )

    setIsRunning(false)
  }, [query, selected, isRunning])

  const results = Object.values(runs)
  const fastest = results
    .filter((r) => r.state === 'done' && r.job?.duration_seconds)
    .sort((a, b) => a.job!.duration_seconds! - b.job!.duration_seconds!)[0]

  return (
    <div className="h-full overflow-y-auto p-6 space-y-6">
      <header className="flex items-center gap-2">
        <BarChart3 className="w-5 h-5 text-blue-600" />
        <h2 className="text-lg font-semibold">Model comparison</h2>
      </header>

      {loadError && (
        <div className="flex items-start gap-2 p-3 rounded border border-red-300 bg-red-50 dark:bg-red-950/30 text-sm">
          <AlertCircle className="w-4 h-4 mt-0.5 text-red-600 shrink-0" />
          <span>Could not load providers: {loadError}</span>
        </div>
      )}

      <section className="space-y-2">
        <label className="text-sm font-medium">Providers</label>
        <div className="flex flex-wrap gap-2">
          {providers.map((p) => (
            <button
              key={p.id}
              type="button"
              disabled={!p.configured || isRunning}
              onClick={() => toggle(p.id)}
              title={p.configured ? p.default_model : 'No credential configured on the server'}
              className={cn(
                'px-3 py-1.5 rounded border text-sm transition-colors',
                selected.includes(p.id)
                  ? 'bg-blue-600 text-white border-blue-600'
                  : 'bg-white dark:bg-slate-800 border-slate-300 dark:border-slate-600',
                !p.configured && 'opacity-40 cursor-not-allowed'
              )}
            >
              {p.label}
            </button>
          ))}
        </div>
        {providers.length > 0 && providers.every((p) => !p.configured) && (
          <p className="text-sm text-amber-600">
            No provider has a credential configured on the server.
          </p>
        )}
      </section>

      <section className="space-y-2">
        <label className="text-sm font-medium">Question</label>
        <textarea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          disabled={isRunning}
          rows={3}
          placeholder="Ask the same question of every selected provider…"
          className="w-full p-3 rounded border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 text-sm"
        />
        <button
          type="button"
          onClick={run}
          disabled={isRunning || !query.trim() || selected.length === 0}
          className="px-4 py-2 rounded bg-blue-600 text-white text-sm font-medium disabled:opacity-40 inline-flex items-center gap-2"
        >
          {isRunning && <Loader2 className="w-4 h-4 animate-spin" />}
          {isRunning ? 'Running…' : 'Compare ' + selected.length + ' provider(s)'}
        </button>
      </section>

      {results.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-sm font-medium">Results</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="text-left border-b border-slate-300 dark:border-slate-600">
                  <th className="py-2 pr-4">Provider</th>
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2 pr-4">Duration</th>
                  <th className="py-2 pr-4">Sources</th>
                  <th className="py-2">Searches</th>
                </tr>
              </thead>
              <tbody>
                {results.map((r) => (
                  <tr key={r.provider} className="border-b border-slate-200 dark:border-slate-700">
                    <td className="py-2 pr-4 font-medium">
                      {r.provider}
                      {fastest?.provider === r.provider && (
                        <span className="ml-2 text-xs text-green-600">fastest</span>
                      )}
                    </td>
                    <td className="py-2 pr-4">
                      {r.state === 'done' && <CheckCircle2 className="w-4 h-4 text-green-600" />}
                      {r.state === 'failed' && <XCircle className="w-4 h-4 text-red-600" />}
                      {(r.state === 'running' || r.state === 'pending') && (
                        <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
                      )}
                    </td>
                    <td className="py-2 pr-4">
                      {r.job?.duration_seconds ? r.job.duration_seconds.toFixed(1) + 's' : '—'}
                    </td>
                    <td className="py-2 pr-4">{r.job?.result?.sources.length ?? '—'}</td>
                    <td className="py-2">{r.job?.result?.usage?.searches ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {results.map((r) =>
            r.error ? (
              <p key={r.provider} className="text-sm text-red-600">
                <strong>{r.provider}:</strong> {r.error}
              </p>
            ) : null
          )}

          {results
            .filter((r) => r.state === 'done' && r.job?.result?.stage_timings?.length)
            .map((r) => (
              <details key={r.provider} className="text-sm">
                <summary className="cursor-pointer font-medium">
                  {r.provider} — stage breakdown
                </summary>
                <ul className="mt-2 ml-4 space-y-1">
                  {r.job!.result!.stage_timings.map((t) => (
                    <li key={t.stage} className="text-slate-600 dark:text-slate-400">
                      {t.stage}: {t.seconds.toFixed(2)}s
                    </li>
                  ))}
                </ul>
              </details>
            ))}
        </section>
      )}
    </div>
  )
}

export default ModelComparison
