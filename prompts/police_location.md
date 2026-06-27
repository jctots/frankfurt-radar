---
model: gemini-2.5-flash
temperature: 0.1
thinking_budget: 0
response_mime_type: application/json
---
You are extracting the location from a German police report (Polizeimeldung) in the Frankfurt am Main area.

Extract and respond with a JSON object:

{{
  "location": "Place and district where the incident occurred (e.g. 'Schweizer Platz, Sachsenhausen', 'Hauptbahnhof, Bahnhofsviertel', 'Berger Strasse, Bornheim'). Use original German place names. If the location cannot be determined, use null.",
  "lat": "Latitude as a decimal number (e.g. 50.1009), or null if unknown",
  "lon": "Longitude as a decimal number (e.g. 8.6821), or null if unknown"
}}

Rules:
- Use well-known Frankfurt districts (Stadtteile): Sachsenhausen, Bornheim, Innenstadt, Nordend, Westend, Bockenheim, Gallus, Ostend, Bahnhofsviertel, Altstadt, Hoechst, Niederrad, Griesheim, Fechenheim, etc.
- For coordinates, use the approximate location of the place mentioned. If only a district is mentioned, use the district centre.
- Do not guess or hallucinate locations not mentioned in the text.
