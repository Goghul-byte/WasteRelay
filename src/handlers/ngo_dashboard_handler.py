"""NGO Dashboard — read-only status page + same-origin frontend.

Mirrors the existing dashboard_status.py pattern (same-origin HTML serving,
JSON default handling) but is a separate function bound to separate API paths.
It only reads data (common.ngo_dashboard, common.dynamo.get_ngo) and never
writes to any table or calls whatsapp_client / ngo_matcher, so it cannot
affect the live WhatsApp workflow, the escalation checker, or the existing
restaurant Control Center.
"""

import json
from decimal import Decimal
from pathlib import Path

from common.dynamo import get_ngo
from common.ngo_dashboard import (
    list_active_ngos_for_selector,
    nearby_restaurants_for_ngo,
    donation_records_for_ngo,
    wastage_trend_summary,
)

PAGE_PATH = Path(__file__).resolve().parent.parent / "dashboard" / "ngo_dashboard.html"


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=_json_default),
    }


def _html_response():
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": PAGE_PATH.read_text(encoding="utf-8"),
    }


def lambda_handler(event, context):
    path = event.get("path", "") or event.get("rawPath", "")
    params = event.get("queryStringParameters") or {}

    # GET /ngo-dashboard serves the HTML page itself, same-origin (no CORS change).
    if path.endswith("/ngo-dashboard"):
        return _html_response()

    # GET /ngo-dashboard-data with no ngo_id -> NGO picker list only.
    ngo_id = params.get("ngo_id")
    if not ngo_id:
        try:
            return _json_response(200, {"ngos": list_active_ngos_for_selector()})
        except Exception as exc:
            print(f"NgoDashboardFunction error (selector): {exc}")
            return _json_response(500, {"error": "Internal server error while listing NGOs."})

    try:
        ngo = get_ngo(ngo_id)
        if not ngo:
            return _json_response(404, {"error": "NGO not found"})

        payload = {
            "ngo": {"ngo_id": ngo["ngo_id"], "name": ngo.get("name"), "lat": ngo.get("lat"), "lng": ngo.get("lng")},
            "nearby_restaurants": nearby_restaurants_for_ngo(ngo),
            "donation_records": donation_records_for_ngo(ngo_id),
            "wastage_trend": wastage_trend_summary(ngo),
        }
        return _json_response(200, payload)

    except Exception as exc:
        print(f"NgoDashboardFunction error: {exc}")
        return _json_response(500, {"error": "Internal server error while building the NGO dashboard."})
