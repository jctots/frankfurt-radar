---
model: gemini-2.5-flash
temperature: 0.2
max_output_tokens: 2048
thinking_budget: 0
response_mime_type: application/json
---
You are summarizing a full day of hourly City Pulse reports for Frankfurt. Compress 24 hours of situational data into a single concise daily summary that will be used as historical context for future hourly pulses.

Date: {date}

Hourly pulses from this day ({pulse_count} total):
{pulses_json}

Previous daily summaries for trend context:
{previous_summaries}

Produce a JSON object:

{{
  "summary": "3-5 sentence narrative of the day. What were the major disruptions? When did they start/resolve? What was the overall travel impact? Mention specific lines, roads, or weather events by name. End with whether the day ended better or worse than it started.",
  "peak_issues": ["List of 1-3 most significant issues that affected the city today"],
  "travel_ok_pct": 0-100 integer — percentage of hours where travel_ok was true
}}

Rules:
- Focus on what CHANGED during the day, not steady-state background noise.
- If all 24 pulses were "all clear", say so in one sentence.
- Reference specific times when major events started or resolved (e.g. "S1 suspended from 08:00, restored by 14:00").
- Keep summary under 500 characters.
- Compare to previous days if provided — note multi-day patterns ("third day of A5 closures").
