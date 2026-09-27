# FeedBridge — WhatsApp-Native Food Rescue Platform

FeedBridge is a WhatsApp-first food-rescue workflow that connects restaurants with verified NGOs without requiring either side to install a separate application. The current implementation runs on AWS serverless services and uses the WhatsApp Cloud API for the operational conversation.

> **Implementation note:** This repository is the application source for the existing `feedbridge` AWS CloudFormation/SAM stack. The infrastructure contract is intentionally preserved. Do not create a second stack or change the existing resource names, table names, API paths, stages, environment variables, or WhatsApp integration when deploying updates.

## Architecture

```text
Restaurant / NGO WhatsApp
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
Conversation State     Domain Logic
    │                     │
    └──────────┬──────────┘
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
```

## Repository layout

```text
template.yaml                         Existing AWS SAM infrastructure contract
samconfig.toml                        Existing deployment configuration
requirements.txt                      Root dependency list
src/common/dynamo.py                  DynamoDB access helpers
src/common/dish_classifier.py         Quantity extraction + dish matching
src/common/geo.py                     Distance and distance-tier calculations
src/common/ngo_matcher.py              NGO selection, offers, escalation, cancellation
src/common/safe_window.py              Food-safety deadline calculations
src/common/structured_input.py         ETA parsing and sanity limits
src/common/whatsapp_client.py          WhatsApp Cloud API adapter
src/handlers/webhook_handler.py       Main WhatsApp webhook Lambda
src/handlers/escalation_checker.py    Scheduled timeout/safety Lambda
data/dish_dictionary_seed.json        Demo dish dictionary
scripts/seed_demo_data.py             Demo-data loader
events/                               Local invocation payloads
```

## Core workflow

1. A restaurant sends a food description such as `50kg Biryani`.
2. The webhook extracts quantity and item information.
3. The dish dictionary is queried and RapidFuzz is used for local matching.
4. If automatic classification cannot confidently identify the category, the WhatsApp flow asks the user to choose a category.
5. The restaurant provides the food location and a preparation/safety estimate.
6. FeedBridge calculates the applicable safe-window deadline.
7. The matcher selects the nearest active, verified NGO within the configured distance tiers.
8. The NGO receives an Accept/Decline offer.
9. A timeout or decline triggers escalation to the next eligible NGO.
10. ETA information is collected in structured steps.
11. The combined ETA is checked against the remaining food-safety window.
12. A successful confirmation creates a pickup OTP/custody handshake.
13. During transit, an NGO can report a delay; the safety window is checked again immediately.
14. The scheduled checker also cancels offers that have expired or passed their safety deadline.

## AWS resources preserved by this project

The existing stack is named `feedbridge` in `ap-south-1`. The application contract uses these resources and names:

| Resource | Existing name / contract |
|---|---|
| CloudFormation/SAM stack | `feedbridge` |
| API stage | `prod` |
| Webhook path | `/webhook` |
| Donations table | `feedbridge-donations` |
| NGO table | `feedbridge-ngos` |
| Restaurant table | `feedbridge-restaurants` |
| Dish dictionary table | `feedbridge-dish-dictionary` |
| Conversation state table | `feedbridge-conversation-state` |
| Webhook Lambda | `WebhookFunction` |
| Escalation Lambda | `EscalationCheckerFunction` |

The WhatsApp configuration remains parameter/environment-variable based; secrets should not be committed to the repository.

## Deployment

### Prerequisites

- AWS CLI authenticated to the intended AWS account
- AWS SAM CLI
- Python 3.12
- An existing `feedbridge` stack in `ap-south-1`

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

This repository is intended to update the **existing** `feedbridge` stack. Do not use a new stack name. Before accepting a deployment change set, review CloudFormation's proposed resource changes and stop if the existing DynamoDB tables, API Gateway, Lambda functions, or WhatsApp-related configuration are unexpectedly being replaced or deleted.

The repository's `samconfig.toml` already targets:

```text
stack_name = feedbridge
region = ap-south-1
```

The existing Meta webhook should continue pointing to the existing API Gateway `/prod/webhook` endpoint. A local source-folder change does not require a new Meta application or webhook.

## Demo data

The seed script loads the dish dictionary and two demonstration NGO records into the existing DynamoDB tables. Review the phone numbers in `scripts/seed_demo_data.py` before running it in a real demo environment.

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

- The food-safety window is a wall-clock deadline.
- A restaurant estimate can shorten the category-based window; it cannot extend it.
- Only active and verified NGOs are considered.
- NGOs outside the configured 15 km matching boundary are not eligible.
- An NGO that declines or times out is excluded from the next matching attempt.
- When little safe-window time remains, the acceptance window is shortened.
- ETA input is bounded to 1–180 minutes for custom values.
- An in-transit ETA modification is rechecked immediately against the remaining safety window.
- Once the safety deadline has passed, escalation stops and the donation becomes unclaimed.

## Current scope

The repository intentionally focuses on the implemented Phase-1 workflow. The seed dish dictionary is a demonstration dataset rather than a production-scale food taxonomy. A production deployment would also need additional operational hardening, larger datasets, and appropriate database indexing before high-volume use.

## Important deployment rule

**Do not edit infrastructure names or create a second AWS stack just to make a new local copy of the repository.** The local source can be cleaned and repackaged while the running `feedbridge` infrastructure remains the same.
