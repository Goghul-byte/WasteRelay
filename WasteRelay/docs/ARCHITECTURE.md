# FeedBridge Architecture

## Runtime components

### WebhookFunction
Receives GET verification requests and POST WhatsApp events through API Gateway. It determines whether the sender is a restaurant or known NGO and advances the appropriate conversation state.

### Common modules
- `dynamo.py`: persistence and table access
- `dish_classifier.py`: free-text item extraction and local fuzzy classification
- `geo.py`: Haversine distance and matching tiers
- `ngo_matcher.py`: offer creation, escalation and cancellation
- `safe_window.py`: category/estimate safety deadline calculations
- `structured_input.py`: bounded ETA parsing
- `whatsapp_client.py`: WhatsApp Cloud API payload construction

### EscalationCheckerFunction
Invoked every minute by the existing scheduled event. It checks NGO offer expiry and food-safety deadlines.

## Data stores

- `feedbridge-donations`: donation lifecycle and routing state
- `feedbridge-ngos`: active/verified NGO records
- `feedbridge-restaurants`: restaurant records
- `feedbridge-dish-dictionary`: dish-to-category mappings
- `feedbridge-conversation-state`: WhatsApp conversation state

## Integration boundary

The application talks to Meta only through `common/whatsapp_client.py`. AWS resource configuration remains in `template.yaml` and `samconfig.toml`; the runtime code does not create or rename infrastructure.
