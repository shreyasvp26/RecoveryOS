import { useEffect, useState } from 'react'

/** Base URL for backend reads. Dev uses the Vite /api proxy to FastAPI. */
export const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

/**
 * Thin read wrapper. Throws an Error carrying the server's detail message on
 * non-2xx so callers can render real error states (never mask failures).
 */
export async function get(path, params) {
  const qs = params
    ? '?' + new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== '')).toString()
    : ''
  let res
  try {
    res = await fetch(`${API_BASE}${path}${qs}`)
  } catch {
    throw new Error(`Network error reaching the RecoveryOS API at ${API_BASE}${path}`)
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const body = await res.json()
      if (body && body.detail) detail = body.detail
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail)
  }
  return res.json()
}

/**
 * Thin write wrapper for the Policy Lab. Mirrors `get`'s error contract so a
 * rejected scenario surfaces the server's validation detail verbatim rather
 * than a generic failure — the server is the only authority on what policy is
 * valid, and the operator needs to read its actual reason.
 */
export async function post(path, body) {
  let res
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    throw new Error(`Network error reaching the RecoveryOS API at ${API_BASE}${path}`)
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const payload = await res.json()
      if (payload && payload.detail) detail = payload.detail
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail)
  }
  return res.json()
}

/**
 * Generic data-fetching hook exposing the three states every view needs:
 * loading, error, and data (which may itself represent an "empty" result —
 * the view distinguishes empty from failed, and a failed fetch is a real
 * error, never silently replaced with zeros or fabricated numbers).
 *
 * @param {(signal: AbortSignal) => Promise<any>} loader
 * @param {Array<unknown>} deps
 */
export function useAsync(loader, deps) {
  const [state, setState] = useState({ status: 'loading', data: null, error: null })
  const key = deps ? JSON.stringify(deps) : ''

  useEffect(() => {
    const controller = new AbortController()
    setState({ status: 'loading', data: null, error: null })
    loader(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ status: 'ok', data, error: null })
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setState({ status: 'error', data: null, error: err })
        }
      })
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  return state
}

export const dashboardSummary = () => get('/dashboard/summary')
export const listEvents = (params) => get('/events', params)
export const eventTrace = (eventId) => get(`/events/${encodeURIComponent(eventId)}/trace`)
export const blockedDecisions = (params) => get('/decisions/blocked', params)
export const replayScenarios = () => get('/replay/scenarios')
export const compareReplays = (body) => post('/replay/compare', body)
export const listIncidents = () => get('/incidents')
export const incidentEvents = (incidentId) =>
  get(`/incidents/${encodeURIComponent(incidentId)}/events`)
export const replayIncident = (incidentId, body) =>
  post(`/incidents/${encodeURIComponent(incidentId)}/replay`, body ?? {})
export const recoveryQueue = (params) => get('/recovery/queue', params)
/**
 * Ask the server to execute for one event. The body is deliberately empty:
 * the intervention, the authorization and the evaluation time are the
 * server's to derive, and it rejects a request that tries to supply them.
 */
export const executeRecovery = (eventId) =>
  post(`/recovery/${encodeURIComponent(eventId)}/execute`, {})
