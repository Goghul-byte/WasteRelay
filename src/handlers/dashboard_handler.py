"""FeedBridge Control Center create-donation entry point.

This handler intentionally reuses the existing FeedBridge domain modules used by
WebhookFunction. It does not duplicate classification, safety-window, matching,
or WhatsApp-offer logic.
"""

import json
import os
import uuid
from datetime import datetime, timezone

from common import ngo_matcher
from common.dish_classifier import extract_items, match_dish_category
from common.dynamo import put_donation, update_donation
from common.safe_window import final_safe_window_minutes, compute_deadline


DASHBOARD_RESTAURANT_ID = os.environ.get("DASHBOARD_RESTAURANT_ID", "dashboard-control-center")
DASHBOARD_RESTAURANT_NAME = os.environ.get("DASHBOARD_RESTAURANT_NAME", "FeedBridge Control Center")
DASHBOARD_RESTAURANT_PHONE = os.environ.get("DASHBOARD_RESTAURANT_PHONE", "")
DASHBOARD_RESTAURANT_LAT = float(os.environ.get("DASHBOARD_RESTAURANT_LAT", "11.94"))
DASHBOARD_RESTAURANT_LNG = float(os.environ.get("DASHBOARD_RESTAURANT_LNG", "79.81"))


def _response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=_json_default),
    }


def _json_default(value):
    # DynamoDB may return Decimal values from shared helpers.
    try:
        from decimal import Decimal
        if isinstance(value, Decimal):
            return float(value)
    except Exception:
        pass
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _parse_body(event):
    raw = event.get("body") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Request body must be valid JSON")


def _normalise_prep_time(value):
    if not value:
        raise ValueError("prep_time is required")

    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("prep_time must be an ISO-8601 datetime") from exc

    # The existing safe-window module subtracts timezone-aware datetimes. If a
    # caller supplies a naive datetime, treat it as UTC rather than silently
    # using the Lambda machine's local timezone.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _build_event(event_name, at, **extra):
    item = {"event": event_name, "at": at}
    item.update(extra)
    return item


def lambda_handler(event, context):
    try:
        body = _parse_body(event)

        food_name = str(body.get("food_name", "")).strip()
        if not food_name:
            raise ValueError("food_name is required")

        try:
            quantity = float(body.get("quantity"))
        except (TypeError, ValueError) as exc:
            raise ValueError("quantity must be numeric") from exc
        if quantity <= 0:
            raise ValueError("quantity must be greater than zero")

        unit = str(body.get("unit", "kg")).strip().lower()
        if not unit:
            raise ValueError("unit is required")

        prep_time_iso = _normalise_prep_time(body.get("prep_time"))
        temperature_raw = body.get("temperature")
        temperature = None if temperature_raw in (None, "") else float(temperature_raw)

        received_at = datetime.now(timezone.utc).isoformat()
        # Reuse the actual Tier-1 parser rather than constructing a parallel item schema.
        parsed_items = extract_items(f"1 {unit} {food_name}")
        if not parsed_items:
            raise ValueError("Could not extract a food item from the supplied dish name")

        # The dashboard form has one food item. Preserve the exact user-supplied
        # quantity/unit while keeping the existing parser responsible for item extraction.
        item = parsed_items[0]
        item["item_name"] = food_name
        item["quantity"] = quantity
        item["unit"] = unit

        category, confident = match_dish_category(item["item_name"])
        if not confident:
            return _response(422, {
                "error": "Food could not be confidently classified from the existing dish dictionary.",
                "food_name": food_name,
                "hint": "Use a dish name present in feedbridge-dish-dictionary or add it through the existing learning flow.",
            })
        item["category"] = category

        classified_at = datetime.now(timezone.utc).isoformat()
        safe_window_minutes = final_safe_window_minutes(category)
        deadline = compute_deadline(prep_time_iso, safe_window_minutes)
        rssl_at = datetime.now(timezone.utc).isoformat()

        donation_id = str(uuid.uuid4())
        donation = {
            "donation_id": donation_id,
            "restaurant_id": DASHBOARD_RESTAURANT_ID,
            "restaurant_phone": DASHBOARD_RESTAURANT_PHONE,
            "restaurant_name": DASHBOARD_RESTAURANT_NAME,
            "restaurant_lat": DASHBOARD_RESTAURANT_LAT,
            "restaurant_lng": DASHBOARD_RESTAURANT_LNG,
            "items": [item],
            "prep_time": prep_time_iso,
            "temperature_c": temperature,
            "safe_window_deadline": deadline.isoformat(),
            "safe_window_minutes": safe_window_minutes,
            "current_ngo_id": None,
            "excluded_ngo_ids": [],
            "status": "created",
            "history": [
                _build_event("donation_received", received_at),
                _build_event("food_classified", classified_at,
                              item_name=food_name, category=category),
                _build_event("rssl_calculated", rssl_at,
                              safe_window_minutes=safe_window_minutes,
                              deadline=deadline.isoformat()),
            ],
        }

        # Same donations table and exact field names consumed by EscalationCheckerFunction.
        put_donation(donation)

        # The matcher expects this exact status before it creates an offer.
        update_donation(donation_id, {"status": "awaiting_ngo_response"})
        donation["status"] = "awaiting_ngo_response"

        ngo, distance = ngo_matcher.find_next_ngo(donation)
        if ngo is None:
            # Reuse the existing cancellation behavior; no parallel fallback path.
            ngo_matcher.cancel_unclaimed(donation)
            update_donation(donation_id, {
                "history": donation["history"] + [
                    _build_event("no_ngo_available", datetime.now(timezone.utc).isoformat())
                ],
            })
            return _response(201, {
                "donation_id": donation_id,
                "status": "unclaimed",
                "message": "Donation created, but no eligible NGO was available within the existing matcher rules.",
            })

        ngo_selected_at = datetime.now(timezone.utc).isoformat()
        ngo_matcher.send_offer(donation, ngo, distance)

        # send_offer() is the authoritative existing WhatsApp path. Its adapter
        # logs HTTP failures rather than raising; therefore this event means the
        # real send_offer call completed, not that Meta acknowledged delivery.
        history = donation["history"] + [
            _build_event("ngo_selected", ngo_selected_at,
                         ngo_id=ngo["ngo_id"], ngo_name=ngo.get("name"),
                         distance_km=round(float(distance), 1)),
            _build_event("whatsapp_sent", datetime.now(timezone.utc).isoformat(),
                         ngo_id=ngo["ngo_id"], channel="whatsapp_offer"),
        ]
        update_donation(donation_id, {"history": history})

        return _response(201, {
            "donation_id": donation_id,
            "status": "awaiting_ngo_response",
            "ngo": {
                "ngo_id": ngo["ngo_id"],
                "name": ngo.get("name"),
                "distance_km": round(float(distance), 1),
            },
        })

    except ValueError as exc:
        return _response(400, {"error": str(exc)})
    except Exception as exc:
        print(f"DashboardCreateFunction error: {exc}")
        return _response(500, {"error": "Internal server error while creating the donation."})
