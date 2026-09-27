# Preservation Notes

This cleaned repository is based directly on the supplied working FeedBridge project.

## Intentionally preserved

- `template.yaml` resource names, handlers, API path/stage, table names, runtime, schedules, and IAM policy declarations
- `samconfig.toml` stack name and region
- Python runtime modules and their executable statements
- WhatsApp Cloud API endpoint and payload structures
- DynamoDB table keys and access patterns
- Existing conversation-state names and workflow branches

## Presentation-only changes

- Added explanatory source comments
- Reworked README into a professional implementation/deployment guide
- Added architecture and demo documentation
- Added `.gitignore`
- Removed generated `__pycache__` files from the deliverable

No new AWS resources or external integrations were introduced by the cleanup.
