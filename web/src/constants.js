export const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

// A turn is up to 8 sequential model calls server-side, so this is generous.
// It exists so a wedged backend surfaces as an error rather than as an
// animation that never stops.
export const REQUEST_TIMEOUT_MS = 120_000

// Matches MAX_MESSAGE_CHARS in api/main.py -- caught here so an over-long
// message never becomes a 422 round trip.
export const MAX_MESSAGE_CHARS = 2000

export const SUGGESTED_QUESTIONS = [
  'Which airports in New England are strong candidates for terminal expansion?',
  'Compare LA and Santa Ana airport congestion levels.',
  'What is the percentage of long haul flights out of Anchorage airport?',
  'What is the unmet flight demand in SFO airport and why?',
]
