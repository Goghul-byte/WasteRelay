# NGO Dashboard read-only aggregation layer.
#
# This module is purely additive: it only READS from the existing donations_table,
# ngos_table via helpers already defined in common/dynamo.py, and reuses the exact
# same distance/category logic already used by the live matcher (common/geo.py,
# common/safe_window.py). It does not write to any table and does not call
# ngo_matcher, whatsapp_client, or any function used by the live WhatsApp workflow.
#
# Nothing here is imported by webhook_handler.py or escalation_checker.py, so it
# cannot change behavior of the existing production flow even if it has a bug.

from collections import defaultdict
from datetime import datetime, timezone
from common.dynamo import donations_table, ngos_table, restaurants_table
from common.geo import distance_km
from common.safe_window import CATEGORY_BASE_MINUTES

# Matching boundary reused as-is from ngo_matcher / geo.distance_tier (Section 3.3),
# so "nearby" on this dashboard means the same thing it means to the live matcher.
NEARBY_RADIUS_KM = 15

# How many completed weeks of history to look at when scoring surplus likelihood.
TREND_LOOKBACK_DONATIONS = 200


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _all_donations():
    # Single full scan, same DynamoDB access pattern already used elsewhere in this
    # project (scan_donations_by_status uses the same table with a FilterExpression).
    # At hackathon/demo data volume this is fine; swap for a GSI-backed query
    # (e.g. on restaurant_id / current_ngo_id) before any real-scale launch.
    result = donations_table.scan()
    items = result.get("Items", [])
    while "LastEvaluatedKey" in result:
        result = donations_table.scan(ExclusiveStartKey=result["LastEvaluatedKey"])
        items.extend(result.get("Items", []))
    return items


def list_active_ngos_for_selector():
    """Minimal list for the dashboard's NGO picker — id + name only."""
    result = ngos_table.scan()
    ngos = result.get("Items", [])
    return sorted(
        [{"ngo_id": n["ngo_id"], "name": n.get("name", n["ngo_id"])} for n in ngos
         if n.get("active") and n.get("verified")],
        key=lambda x: x["name"],
    )


def _restaurant_profiles(donations):
    """Group donations by restaurant_id, keeping the most recent known location/name."""
    restaurant_records = restaurants_table.scan().get("Items", [])
    restaurant_phones = {
        str(r["restaurant_id"]): r.get("phone")
        for r in restaurant_records
        if r.get("restaurant_id")
    }
    profiles = {}
    for d in donations:
        rid = d.get("restaurant_id")
        if not rid or d.get("restaurant_lat") is None or d.get("restaurant_lng") is None:
            continue
        received_at = None
        for entry in d.get("history", []):
            if entry.get("event") == "donation_received":
                received_at = _parse_iso(entry.get("at"))
        received_at = received_at or _parse_iso(d.get("prep_time"))

        existing = profiles.get(rid)
        if existing is None or (received_at and existing["_last_seen"] and received_at > existing["_last_seen"]):
            profiles[rid] = {
                "restaurant_id": rid,
                "name": d.get("restaurant_name") or f"Restaurant {rid[-4:]}",
                "phone": restaurant_phones.get(str(rid)),
                "lat": float(d["restaurant_lat"]),
                "lng": float(d["restaurant_lng"]),
                "_last_seen": received_at,
}
    return profiles


def _surplus_score(restaurant_donations):
    """Heuristic surplus-likelihood score (v1 — no trained model, see project notes).

    Scores how consistently a restaurant has posted donations in the current
    day-of-week / 3-hour time slot over its own history. This is deliberately
    a transparent frequency heuristic, not a trained classifier — with the data
    volume a hackathon/pilot project has, a real model would just overfit noise.
    """
    if not restaurant_donations:
        return {"label": "Not enough data", "score": 0.0}

    now = datetime.now(timezone.utc)
    current_slot = now.hour // 3
    current_dow = now.weekday()

    same_slot_hits = 0
    for d in restaurant_donations:
        received_at = None
        for entry in d.get("history", []):
            if entry.get("event") == "donation_received":
                received_at = _parse_iso(entry.get("at"))
        received_at = received_at or _parse_iso(d.get("prep_time"))
        if not received_at:
            continue
        if received_at.weekday() == current_dow and (received_at.hour // 3) == current_slot:
            same_slot_hits += 1

    ratio = same_slot_hits / max(1, len(restaurant_donations))
    if len(restaurant_donations) < 3:
        return {"label": "Not enough data", "score": round(ratio, 2)}
    if ratio >= 0.4:
        return {"label": "High", "score": round(ratio, 2)}
    if ratio >= 0.15:
        return {"label": "Medium", "score": round(ratio, 2)}
    return {"label": "Low", "score": round(ratio, 2)}


def nearby_restaurants_for_ngo(ngo):
    """Restaurants within the live matching radius, each with donation history and
    a surplus-likelihood heuristic — everything the NGO dashboard card needs."""
    donations = _all_donations()[-TREND_LOOKBACK_DONATIONS:]
    profiles = _restaurant_profiles(donations)

    by_restaurant = defaultdict(list)
    for d in donations:
        rid = d.get("restaurant_id")
        if rid in profiles:
            by_restaurant[rid].append(d)

    ngo_lat, ngo_lng = float(ngo["lat"]), float(ngo["lng"])
    results = []
    for rid, profile in profiles.items():
        km = distance_km(ngo_lat, ngo_lng, profile["lat"], profile["lng"])
        if km > NEARBY_RADIUS_KM:
            continue
        r_donations = by_restaurant[rid]
        total_qty_by_unit = defaultdict(float)
        category_counts = defaultdict(int)
        unclaimed_count = 0
        picked_up_count = 0
        for d in r_donations:
            for item in d.get("items", []):
                try:
                    total_qty_by_unit[item.get("unit", "kg")] += float(item.get("quantity", 0))
                except (TypeError, ValueError):
                    pass
                if item.get("category"):
                    category_counts[item["category"]] += 1
            if d.get("status") in ("unclaimed", "cancelled"):
                unclaimed_count += 1
            if d.get("status") == "picked_up":
                picked_up_count += 1

        results.append({
            "restaurant_id": rid,
            "name": profile["name"],
            "phone": profile.get("phone"),
            "distance_km": round(km, 1),
            "donation_count": len(r_donations),
            "picked_up_count": picked_up_count,
            "unclaimed_count": unclaimed_count,
            "total_quantity_by_unit": dict(total_qty_by_unit),
            "top_category": max(category_counts, key=category_counts.get) if category_counts else None,
            "surplus_likelihood": _surplus_score(r_donations),
        })

    results.sort(key=lambda r: r["distance_km"])
    return results


def donation_records_for_ngo(ngo_id, limit=25):
    """This NGO's own donation history — offers it received, accepted, or picked up."""
    donations = _all_donations()
    relevant = [
        d for d in donations
        if d.get("current_ngo_id") == ngo_id or ngo_id in [str(x) for x in d.get("excluded_ngo_ids", [])]
    ]

    def sort_key(d):
        return _parse_iso(d.get("prep_time")) or datetime.min.replace(tzinfo=timezone.utc)

    relevant.sort(key=sort_key, reverse=True)

    records = []
    for d in relevant[:limit]:
        items_summary = ", ".join(
            f"{i.get('quantity')}{i.get('unit')} {i.get('item_name')}" for i in d.get("items", [])
        )
        records.append({
            "donation_id": d["donation_id"],
            "restaurant_name": d.get("restaurant_name"),
            "items_summary": items_summary,
            "status": d.get("status"),
            "distance_km": d.get("offer_distance_km"),
            "prep_time": d.get("prep_time"),
        })
    return records


def wastage_trend_summary(ngo):
    """Area-wide wastage trend within this NGO's matching radius — donations that
    were never picked up (unclaimed/cancelled) vs. successfully rescued."""
    restaurants = nearby_restaurants_for_ngo(ngo)
    total_donations = sum(r["donation_count"] for r in restaurants)
    total_unclaimed = sum(r["unclaimed_count"] for r in restaurants)
    total_picked_up = sum(r["picked_up_count"] for r in restaurants)
    rescue_rate = round(100 * total_picked_up / total_donations, 1) if total_donations else None
    return {
        "restaurants_in_range": len(restaurants),
        "total_donations": total_donations,
        "total_picked_up": total_picked_up,
        "total_unclaimed": total_unclaimed,
        "rescue_rate_pct": rescue_rate,
        "categories_tracked": list(CATEGORY_BASE_MINUTES.keys()),
    }
