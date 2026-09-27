# WasteRelay — WhatsApp-Native Waste Pickup MVP

WasteRelay is a WhatsApp-first workflow that lets a waste source (currently modeled as a restaurant) report what they have, choose which waste stream it belongs to, and — for the one stream that's fully wired up today (Wet Waste) — have the request routed through a working pickup pipeline: item classification, a safe-window deadline, NGO matching with escalation, and an OTP pickup handshake.

This is a **basic working MVP**, not the final product vision. It demonstrates the core WhatsApp conversation flow and the pickup/escalation logic end-to-end for one waste stream, with the other streams intentionally left as placeholders and several planned intelligence features not yet built.

## What this version actually is

- **New in this version:** after a source describes their items and they're classified, the bot now asks the user to pick a **waste stream** — Wet Waste, Dry Waste, Sanitary Waste, or Special/Hazardous — before continuing to location capture.
- **What's wired up:** only the **Wet Waste** selection continues into the working pipeline (item classification → safe-window deadline → NGO matching/escalation → ETA collection → OTP pickup handshake).
- **What's not wired up yet:** selecting Dry, Sanitary, or Special/Hazardous is currently a dead end — the bot acknowledges the choice, explains that the prototype only continues on the Wet Waste flow, and re-prompts. There is no routing, matching, or facility logic behind the other three streams yet.
- **Not implemented anywhere in this codebase yet:** AI source-photo verification, citywide batch allocation (OR-Tools), and road-based travel-time routing (OSRM). These are planned next steps, not current features — they should not be described as implemented.

## Architecture

```text
Source WhatsApp
          │
          ▼
   Meta WhatsApp Cloud API
          │
          ▼
   API Gateway /webhook
          │
          ▼
     WebhookFunction
          │
    ┌─────┴───────────────┐
    │                     │
    ▼                     ▼
Conversation State     Item classification
    │                     │
    └──────────┬──────────┘
               ▼
      Waste-stream selector
   (Wet / Dry / Sanitary / Special)
               │
        Wet Waste only
               ▼
          DynamoDB Tables
               │
               ▼
        NGO matching /
       safe-window checks
               │
               ▼
       WhatsApp responses

EscalationCheckerFunction
        │
        └── runs every minute to enforce offer/safety deadlines

Control Center Lambdas
        │
        └── create + track pickups from a browser dashboard

NGO Dashboard Lambda
        │
        └── read-only NGO-facing dashboard
```

## Repository layout

```text
template.yaml                     AWS SAM infrastructure contract
samconfig.toml                    Deployment configuration
src/requirements.txt              Lambda dependency list (used by sam build)
src/common/dynamo.py              DynamoDB access helpers
src/common/dish_classifier.py     Quantity extraction + item matching
src/common/geo.py                 Distance and distance-tier calculations
src/common/ngo_matcher.py         NGO selection, offers, escalation, cancellation
src/common/safe_window.py         Safety-window deadline calculations
src/common/structured_input.py    ETA parsing and sanity limits
src/common/whatsapp_client.py     WhatsApp Cloud API adapter
src/handlers/webhook_handler.py   Main WhatsApp webhook Lambda (item classification, waste-stream selector, Wet Waste pipeline)
src/handlers/escalation_checker.py  Scheduled timeout/safety Lambda
src/handlers/dashboard_handler.py   Control Center: create pickups from the browser
src/handlers/dashboard_status.py    Control Center: status API + serves the dashboard page
src/dashboard/index.html          Control Center front end
ngo_dashboard/                    NGO-facing dashboard Lambda, front end, and its own copy of the common modules
data/dish_dictionary_seed.json    Demo item dictionary
scripts/seed_demo_data.py         Demo-data loader
events/                           Local invocation payloads
docs/ARCHITECTURE.md              Component/data-store reference
docs/DEMO_RUNBOOK.md              Live-demo checklist
```

## Core workflow

1. A source sends a description of what they have, e.g. `50kg Biryani`.
2. The webhook extracts quantity and item information.
3. Items are matched against a dictionary with fuzzy matching; unmatched items fall back to a WhatsApp button prompt.
4. The bot asks which **waste stream** the pickup belongs to: Wet Waste, Dry Waste, Sanitary Waste, or Special/Hazardous.
5. If the source picks anything other than Wet Waste, the bot explains that the prototype currently only continues on the Wet Waste flow and re-asks — nothing further happens for those streams yet.
6. If Wet Waste is picked, the source provides pickup location and a preparation/safety estimate per item.
7. WasteRelay calculates the applicable safe-window deadline.
8. The matcher selects the nearest active, verified NGO within the configured distance tiers.
9. The NGO receives an Accept/Decline offer.
10. A timeout or decline triggers escalation to the next eligible NGO.
11. ETA information is collected in structured steps and checked against the remaining safe window.
12. A successful confirmation creates a pickup OTP/custody handshake.
13. During transit, an NGO can report a delay; the safety window is checked again immediately.
14. A scheduled checker also cancels offers that have expired or passed their safety deadline.
15. Independently of the WhatsApp flow, a Control Center dashboard can create and track pickups, and an NGO dashboard gives NGOs a read-only view.

## Deployment

### Prerequisites

- AWS CLI authenticated to the intended AWS account
- AWS SAM CLI
- Python 3.12
- An existing deployed backend stack for this project

Confirm the AWS identity and region before deploying:

```powershell
aws sts get-caller-identity
aws configure get region
sam --version
python --version
```

### Build

```powershell
sam build
```

### Safe deployment procedure

This repository is intended to update the **existing** backend stack, not create a new one. Before accepting a deployment change set, review CloudFormation's proposed resource changes and stop if any existing DynamoDB tables, API Gateway routes, Lambda functions, or WhatsApp-related configuration are unexpectedly being replaced or deleted.

The existing Meta webhook should continue pointing at the same API endpoint. A local source-folder change does not require a new Meta application or webhook.

## Demo data

The seed script loads the item dictionary and demonstration NGO records into the existing DynamoDB tables. Review the phone numbers in `scripts/seed_demo_data.py` before running it in a real demo environment.

```powershell
python scripts/seed_demo_data.py
```

## Local testing

The repository contains example events under `events/` for invoking the webhook Lambda without sending a real WhatsApp message. For example:

```powershell
sam local invoke WebhookFunction --event events/restaurant_first_message.json
sam local invoke WebhookFunction --event events/restaurant_location.json
```

Local invocation exercises Lambda logic but does not reproduce Meta's external delivery path. For the final demonstration, the live WhatsApp integration remains the source of truth.

## Safety and operational rules implemented

- The safe window is a wall-clock deadline.
- A source's own estimate can shorten the category-based window; it cannot extend it.
- Only active and verified NGOs are considered.
- NGOs outside the configured 15 km matching boundary are not eligible.
- An NGO that declines or times out is excluded from the next matching attempt.
- When little safe-window time remains, the acceptance window is shortened.
- ETA input is bounded to 1–180 minutes for custom values.
- An in-transit ETA modification is rechecked immediately against the remaining safety window.
- Once the safety deadline has passed, escalation stops and the pickup becomes unclaimed.
- All of the above currently applies only to requests routed through the Wet Waste stream.

## Current scope

This is a basic working MVP, not the target end-state. What actually runs today:

- A working pipeline (classification → safe window → NGO matching/escalation → OTP handshake) for anything selected as Wet Waste.
- A front-of-flow waste-stream selector with four options, of which three (Dry, Sanitary, Special/Hazardous) are currently intake-only placeholders with no backend logic behind them.
- A Control Center dashboard and a read-only NGO-facing dashboard.

Planned next, **not yet built**:

- AI source-photo verification (fine-tuned MobileNetV3-Large) to check a submitted photo against the declared waste stream.
- Citywide batch allocation across pending requests using Google OR-Tools, instead of one-at-a-time NGO matching.
- Road-based travel-time evaluation via OSRM in place of straight-line distance.
- Actual routing/matching logic for the Dry, Sanitary, and Special/Hazardous streams.

The seed item dictionary is a demonstration dataset rather than a production-scale taxonomy. A production deployment would also need additional operational hardening, larger datasets, and appropriate database indexing before high-volume use.

## Important deployment rule

**Do not edit infrastructure names or create a second AWS stack just to make a new local copy of the repository.** The local source can be cleaned and repackaged while the running infrastructure remains the same.
