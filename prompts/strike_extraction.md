---
model: gemini-2.5-flash
temperature: 0.1
thinking_budget: 0
response_mime_type: application/json
---
Today's date is {today}.

You are analyzing a German press release about a labor strike or warning strike (Warnstreik).
Extract the following fields and respond with a JSON object:

{{
  "summary": "2-3 sentence English summary of the strike: who is striking, what sector, when, where, and why",
  "valid_from": "Strike start date/time in ISO 8601 format with Europe/Berlin timezone (e.g. 2026-06-05T00:00:00+02:00), or null if not determinable",
  "valid_until": "Strike end date/time in ISO 8601 format with Europe/Berlin timezone, or null if not determinable. For open-ended strikes use null.",
  "location": "Specific rally/demo location if mentioned (e.g. 'Hauptwache, Frankfurt'), otherwise the city or region name (e.g. 'Frankfurt und Region')",
  "lat": "Latitude of the location as a decimal number (e.g. 50.1009), or null if region-wide or unknown",
  "lon": "Longitude of the location as a decimal number (e.g. 8.6821), or null if region-wide or unknown",
  "service": "One of: Transport, Retail, Public Sector, Aviation, Healthcare, Other",
  "affected": ["List of affected companies or institutions, e.g. 'VGF', 'Rewe', 'Goethe-Universität'"]
}}

Rules:
- All date/times must use Europe/Berlin timezone offset (+01:00 or +02:00 depending on DST).
- Dates must be consistent with the press release's publication date. Do not output dates from prior years.
- If the strike spans multiple days, valid_from is the start of the first day, valid_until is the end of the last day.
- For single-day strikes without specific end time, set valid_until to end of day (23:59).
- If the press release is about negotiations or general union news (not an actual strike call), return {{"not_a_strike": true}}.
- The summary must be in English.
- For coordinates, use the approximate location of the place mentioned (street, intersection, landmark, station). If only a district is mentioned, use the district centre. If the strike is region-wide, return null for lat and lon.
