/**
 * RecoveryOS integer-safe display formatters.
 *
 * Financial amounts are persisted as integer paise and are NEVER mutated or
 * recomputed to floating point. These functions only produce display strings
 * from the integer value, so the stored precision is always preserved.
 */

/** Pad a positive integer to two digits for paise/cent display. */
function pad2(n) {
  return n.toString().padStart(2, '0')
}

/** Group an integer string with Indian digit grouping (##,##,##,###). */
function groupIndian(numStr) {
  const s = numStr
  if (s.length <= 3) return s
  const last3 = s.slice(-3)
  let rest = s.slice(0, -3)
  const parts = []
  while (rest.length > 2) {
    parts.unshift(rest.slice(-2))
    rest = rest.slice(0, -2)
  }
  if (rest.length > 0) parts.unshift(rest)
  return parts.join(',') + ',' + last3
}

/**
 * Format integer paise as an Indian Rupee string (e.g. 1164900 -> ₹11,649.00).
 * Pure integer math; never touches a float.
 */
export function formatINR(paise) {
  if (paise === null || paise === undefined || Number.isNaN(Number(paise))) {
    return '₹—'
  }
  const total = Number(paise)
  const abs = Math.abs(total)
  const rupees = Math.floor(abs / 100)
  const remainder = abs % 100
  const signed = total < 0 ? '-' : ''
  return `${signed}₹${groupIndian(rupees.toString())}.${pad2(remainder)}`
}

/** Format a rate (0..1) as a percentage string, e.g. 0.482 -> "48.2%". */
export function formatRate(rate) {
  if (rate === null || rate === undefined || Number.isNaN(Number(rate))) {
    return '—'
  }
  return `${(Number(rate) * 100).toFixed(1)}%`
}

/** Format an ISO timestamp for display. */
export function formatTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

/** Humanize a machine id for compact display (e.g. fraud_protection). */
export function humanize(key) {
  if (!key) return '—'
  return String(key).replace(/[_.-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}
