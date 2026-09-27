# FeedBridge Live Demo Runbook

## Pre-demo checks

1. Confirm AWS identity and region.
2. Confirm the `feedbridge` stack is healthy.
3. Confirm the existing WhatsApp webhook is still configured for `/prod/webhook`.
4. Confirm the NGO demo records have valid phone numbers for the presentation.
5. Confirm the dish dictionary contains the demo dish.

## Suggested restaurant-side flow

Send a message such as:

```text
50kg Biryani
```

Then follow the location, safety-estimate and confirmation prompts.

## Suggested NGO-side flow

1. Receive the pickup offer.
2. Press **Accept**.
3. Provide the requested ETA values.
4. Complete the pickup OTP step when appropriate.

## Escalation demonstration

For a controlled demonstration, allow the offer to expire or use **Decline**. The system should move to the next eligible NGO while preserving the original safety deadline.

## If Meta outbound messaging is unavailable

Use the included SAM local event payloads to demonstrate backend execution and inspect the existing DynamoDB/CloudWatch state. Do not create a second Meta integration merely for the demo.
