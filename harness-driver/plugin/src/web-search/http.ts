/**
 * The one HTTP leg every Lore search provider shares: request → parsed JSON, or a
 * `WebError` carrying dsh's shared codes (`WEB_ABORTED`, `WEB_PROVIDER_ERROR`).
 */

import { WebError } from '@deepseek-ai/dsh-web'
import type { WebSearchResult, WebSearchSource } from '@deepseek-ai/dsh-web'

/** The fetch a provider calls; injected so tests drive the wire without a network. */
export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>

/** Longest provider error body carried into the error message. */
const ERROR_EXCERPT_MAX = 200

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

function aborted(label: string, error: unknown): WebError {
  return new WebError(`${label} search aborted`, 'WEB_ABORTED', { cause: error })
}

/**
 * Run one provider request and parse its JSON body.
 *
 * A non-2xx response throws `WEB_PROVIDER_ERROR` naming the status and at most
 * 200 characters of the body; an abort at any point throws `WEB_ABORTED`.
 */
export async function requestJson(
  fetchImpl: FetchLike, label: string, url: string, init: RequestInit, signal?: AbortSignal,
): Promise<unknown> {
  let response: Response
  try {
    response = await fetchImpl(url, { ...init, redirect: 'error', ...signal !== undefined ? { signal } : {} })
  } catch (error: unknown) {
    if (isAbortError(error)) throw aborted(label, error)
    throw new WebError(`${label} search request failed: ${String(error)}`, 'WEB_PROVIDER_ERROR', { cause: error })
  }
  if (!response.ok) {
    let excerpt = ''
    try {
      excerpt = (await response.text()).replace(/\s+/g, ' ').trim().slice(0, ERROR_EXCERPT_MAX)
    } catch (error: unknown) {
      if (isAbortError(error)) throw aborted(label, error)
      // WHY swallowed: the status is the real error; an unreadable body only
      // costs the excerpt.
    }
    throw new WebError(
      `${label} API error (HTTP ${response.status})${excerpt ? `: ${excerpt}` : ''}`,
      'WEB_PROVIDER_ERROR',
    )
  }
  try {
    return await response.json()
  } catch (error: unknown) {
    if (isAbortError(error)) throw aborted(label, error)
    throw new WebError(`${label} returned an unprocessable response body: ${String(error)}`, 'WEB_PROVIDER_ERROR', { cause: error })
  }
}

/** `value` as a non-blank string, else undefined — providers leave fields null or empty. */
export function text(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : undefined
}

/**
 * One provider row → a normalized source; a row without a url is dropped
 * (undefined) — it carries nothing the agent can cite or open.
 */
export function source(url: unknown, title: unknown, snippet: unknown, publishedAt: unknown): WebSearchSource | undefined {
  const href = text(url)
  if (href === undefined) return undefined
  const fields = { title: text(title), snippet: text(snippet), publishedAt: text(publishedAt) }
  return {
    url: href,
    ...Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== undefined)),
  }
}

/** Rows → a search result. The web seam owns the `maxResults` cut, so `truncated` is false. */
export function toResult<T>(rows: readonly T[], pick: (row: T) => WebSearchSource | undefined): WebSearchResult {
  return { sources: rows.map(pick).filter((s): s is WebSearchSource => s !== undefined), truncated: false }
}
