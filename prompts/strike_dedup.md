---
model: gemini-2.5-flash
temperature: 0.1
thinking_budget: 0
response_mime_type: application/json
---
Are these two alerts about the same labor strike or warning strike event?

EXISTING ALERT:
Title: {existing_title}
Summary: {existing_body}
From: {existing_from}
Until: {existing_until}
Service: {existing_service}

NEW ALERT:
Title: {new_title}
Summary: {new_body}
From: {new_from}
Until: {new_until}
Service: {new_service}

Respond with a JSON object: {{"same_event": true}} or {{"same_event": false}}.
Only return true if both alerts clearly describe the same strike action by the same union affecting the same workers/companies.
