"""Google Maps Platform tools owned by the Geospatial Intelligence Agent."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote

import httpx
from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, Field, ValidationError, model_validator


SOLUTION_ID = "gmp_git_agentskills_v1"
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_GEOCODE_LOCATIONS = 50
MAX_ROUTE_WAYPOINTS = 25
MAX_MATRIX_ELEMENTS = 625
MAX_RESTRICTED_MATRIX_ELEMENTS = 100
MAX_ROADS_POINTS = 100

GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
ROUTE_MATRIX_URL = (
    "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
)
WEATHER_URLS = {
    "current": "https://weather.googleapis.com/v1/currentConditions:lookup",
    "hourly": "https://weather.googleapis.com/v1/forecast/hours:lookup",
    "daily": "https://weather.googleapis.com/v1/forecast/days:lookup",
    "history": "https://weather.googleapis.com/v1/history/hours:lookup",
    "alerts": "https://weather.googleapis.com/v1/publicAlerts:lookup",
}
ROADS_URLS = {
    "snap_to_roads": "https://roads.googleapis.com/v1/snapToRoads",
    "nearest_roads": "https://roads.googleapis.com/v1/nearestRoads",
    "speed_limits": "https://roads.googleapis.com/v1/speedLimits",
}

PLACE_BASIC_FIELDS = (
    "id,displayName,formattedAddress,location,types,primaryType,"
    "businessStatus,googleMapsUri,attributions"
)
PLACE_OPERATIONAL_FIELDS = (
    f"{PLACE_BASIC_FIELDS},currentOpeningHours,regularOpeningHours,"
    "utcOffsetMinutes,timeZone,entrances,navigationPoints,addressDescriptor"
)
ROUTE_FIELDS = (
    "routes.distanceMeters,routes.duration,routes.staticDuration,"
    "routes.polyline.encodedPolyline,routes.legs.distanceMeters,"
    "routes.legs.duration,routes.legs.staticDuration,"
    "routes.optimizedIntermediateWaypointIndex,routes.routeLabels,"
    "routes.travelAdvisory.tollInfo"
)
MATRIX_FIELDS = (
    "originIndex,destinationIndex,status,condition,distanceMeters,duration,"
    "staticDuration,travelAdvisory.tollInfo"
)


class GeographicCoordinate(BaseModel):
    """Latitude and longitude accepted by Google Maps Platform APIs."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class LocationReference(BaseModel):
    """A domain-neutral organizational or Google location reference."""

    reference_id: str = Field(min_length=1)
    name: str | None = None
    address: str | None = None
    place_id: str | None = None
    coordinates: GeographicCoordinate | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    via: bool = False
    vehicle_stopover: bool = False
    side_of_road: bool = False

    @model_validator(mode="after")
    def require_location_value(self) -> LocationReference:
        if not any((self.name, self.address, self.place_id, self.coordinates)):
            raise ValueError("name, address, place_id, or coordinates is required")
        return self


class PlanningWindow(BaseModel):
    """Time range in which physical Mission work occurs."""

    start_at: datetime
    end_at: datetime
    timezone: str

    @model_validator(mode="after")
    def validate_order(self) -> PlanningWindow:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class GeospatialJourney(BaseModel):
    """A journey the manager asks the specialist to investigate."""

    journey_id: str = Field(min_length=1)
    origin_reference_id: str = Field(min_length=1)
    destination_reference_id: str = Field(min_length=1)
    waypoint_reference_ids: list[str] = Field(default_factory=list)
    departure_time: datetime | None = None
    arrival_time: datetime | None = None
    travel_mode: Literal["DRIVE", "BICYCLE", "WALK", "TWO_WHEELER", "TRANSIT"] = (
        "DRIVE"
    )
    constraints: dict[str, Any] = Field(default_factory=dict)


class LocationContext(BaseModel):
    """Geographic context for Places API (New) searches."""

    center: GeographicCoordinate | None = None
    radius_meters: float | None = Field(default=None, gt=0, le=50_000)
    rectangle_low: GeographicCoordinate | None = None
    rectangle_high: GeographicCoordinate | None = None
    region_code: str | None = Field(default=None, min_length=2, max_length=2)
    language_code: str | None = None


class RouteOptions(BaseModel):
    """Supported Routes API constraints shared by routes and matrices."""

    travel_mode: Literal["DRIVE", "BICYCLE", "WALK", "TWO_WHEELER", "TRANSIT"] = (
        "DRIVE"
    )
    routing_preference: Literal[
        "TRAFFIC_UNAWARE", "TRAFFIC_AWARE", "TRAFFIC_AWARE_OPTIMAL"
    ] | None = None
    departure_time: datetime | None = None
    arrival_time: datetime | None = None
    avoid_tolls: bool = False
    avoid_highways: bool = False
    avoid_ferries: bool = False
    avoid_indoor: bool = False
    compute_alternative_routes: bool = False
    optimize_waypoint_order: bool = False
    include_tolls: bool = False
    language_code: str | None = None
    units: Literal["METRIC", "IMPERIAL"] = "METRIC"

    @model_validator(mode="after")
    def validate_route_options(self) -> RouteOptions:
        if self.departure_time and self.arrival_time:
            raise ValueError("departure_time and arrival_time cannot both be set")
        if self.routing_preference and self.travel_mode not in {
            "DRIVE",
            "TWO_WHEELER",
        }:
            raise ValueError(
                "routing_preference is supported only for DRIVE or TWO_WHEELER"
            )
        return self


class MapsToolError(Exception):
    """Safe error surfaced to an agent without credentials or raw responses."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provenance(product: str) -> dict[str, Any]:
    return {
        "provider": "google_maps_platform",
        "product": product,
        "retrieved_at": _now(),
        "attribution": SOLUTION_ID,
    }


def _error_payload(error: MapsToolError, *, input_ref: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    if error.http_status is not None:
        payload["http_status"] = error.http_status
    if input_ref is not None:
        payload["input_ref"] = input_ref
    return payload


def _configuration_error(product: str) -> dict[str, Any]:
    error = MapsToolError(
        "MAPS_KEY_MISSING",
        "GOOGLE_MAPS_API_KEY is not configured.",
    )
    return {
        "status": "error",
        "errors": [_error_payload(error)],
        "provenance": _provenance(product),
    }


def _api_key() -> str:
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        raise MapsToolError(
            "MAPS_KEY_MISSING", "GOOGLE_MAPS_API_KEY is not configured."
        )
    return api_key


def _safe_api_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:500]
        if isinstance(payload.get("error_message"), str):
            return payload["error_message"][:500]
    return fallback


def _request_json(
    method: str,
    url: str,
    *,
    product: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    field_mask: str | None = None,
    key_in_query: bool = False,
) -> Any:
    """Make one bounded Maps request and return decoded JSON."""
    api_key = _api_key()
    request_params = dict(params or {})
    headers = {"X-Goog-Maps-Solution-ID": SOLUTION_ID}
    if key_in_query:
        request_params["key"] = api_key
    else:
        headers["X-Goog-Api-Key"] = api_key
    if field_mask:
        headers["X-Goog-FieldMask"] = field_mask

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        for attempt in range(2):
            try:
                response = client.request(
                    method,
                    url,
                    params=request_params or None,
                    json=json_body,
                    headers=headers,
                )
            except httpx.TimeoutException as error:
                if attempt == 0:
                    continue
                raise MapsToolError(
                    "MAPS_TIMEOUT",
                    f"{product} did not respond before the timeout.",
                    retryable=True,
                ) from error
            except httpx.HTTPError as error:
                raise MapsToolError(
                    "MAPS_UNAVAILABLE",
                    f"{product} could not be reached.",
                    retryable=True,
                ) from error

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 0:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = min(max(float(retry_after or 0), 0), 2)
                    except ValueError:
                        delay = 0
                    if delay:
                        time.sleep(delay)
                    continue

            try:
                payload = response.json()
            except ValueError as error:
                raise MapsToolError(
                    "INVALID_MAPS_RESPONSE",
                    f"{product} returned an invalid response.",
                    http_status=response.status_code,
                    retryable=response.status_code >= 500,
                ) from error

            if not 200 <= response.status_code < 300:
                code = (
                    "MAPS_RATE_LIMITED"
                    if response.status_code == 429
                    else "MAPS_PERMISSION_DENIED"
                    if response.status_code in {401, 403}
                    else "MAPS_REQUEST_INVALID"
                    if response.status_code == 400
                    else "MAPS_UNAVAILABLE"
                )
                message = _safe_api_message(
                    payload, f"{product} request failed."
                ).replace(api_key, "[redacted]")
                raise MapsToolError(
                    code,
                    message,
                    http_status=response.status_code,
                    retryable=response.status_code == 429
                    or response.status_code >= 500,
                )
            return payload

    raise MapsToolError(
        "MAPS_UNAVAILABLE",
        f"{product} request failed after retry.",
        retryable=True,
    )


def _coerce_model(value: Any, model_type: type[BaseModel]) -> BaseModel:
    if isinstance(value, model_type):
        return value
    return model_type.model_validate(value)


def _location_error(index: int, error: ValidationError) -> dict[str, Any]:
    return {
        "code": "INVALID_LOCATION",
        "message": "The location reference is invalid.",
        "input_ref": f"locations[{index}]",
        "retryable": False,
        "details": json.loads(error.json(include_url=False)),
    }


def _waypoint(location: LocationReference) -> dict[str, Any]:
    if location.place_id:
        waypoint: dict[str, Any] = {"placeId": location.place_id}
    elif location.coordinates:
        waypoint = {
            "location": {
                "latLng": {
                    "latitude": location.coordinates.latitude,
                    "longitude": location.coordinates.longitude,
                }
            }
        }
    elif location.address or location.name:
        waypoint = {"address": location.address or location.name}
    else:  # protected by LocationReference validation
        raise MapsToolError("INVALID_LOCATION", "A route location is incomplete.")
    if location.via:
        waypoint["via"] = True
    if location.vehicle_stopover:
        waypoint["vehicleStopover"] = True
    if location.side_of_road:
        waypoint["sideOfRoad"] = True
    return waypoint


def _duration_seconds(value: Any) -> float | None:
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    try:
        return float(value[:-1])
    except ValueError:
        return None


def geocode_locations(
    locations: list[LocationReference],
    tool_context: ToolContext,
    region_code: str | None = None,
    language_code: str | None = None,
) -> dict[str, Any]:
    """Resolve organizational locations through the Google Geocoding API.

    Args:
        locations: Address, Place ID, or coordinate references to resolve.
        region_code: Optional two-letter region bias.
        language_code: Optional response language code.
    """
    _ = tool_context
    product = "Geocoding API"
    if len(locations) > MAX_GEOCODE_LOCATIONS:
        error = MapsToolError(
            "TOO_MANY_LOCATIONS",
            f"At most {MAX_GEOCODE_LOCATIONS} locations may be geocoded per call.",
        )
        return {
            "status": "error",
            "resolved_locations": [],
            "warnings": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }

    resolved: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, value in enumerate(locations):
        try:
            location = _coerce_model(value, LocationReference)
        except ValidationError as error:
            errors.append(_location_error(index, error))
            continue

        params: dict[str, Any] = {}
        if location.place_id:
            params["place_id"] = location.place_id
        elif location.address or location.name:
            params["address"] = location.address or location.name
        elif location.coordinates:
            params["latlng"] = (
                f"{location.coordinates.latitude},{location.coordinates.longitude}"
            )
        if region_code:
            params["region"] = region_code.lower()
        if language_code:
            params["language"] = language_code

        try:
            payload = _request_json(
                "GET",
                GEOCODING_URL,
                product=product,
                params=params,
                key_in_query=True,
            )
        except MapsToolError as error:
            if error.code == "MAPS_KEY_MISSING":
                return {
                    **_configuration_error(product),
                    "resolved_locations": resolved,
                    "warnings": warnings,
                }
            errors.append(_error_payload(error, input_ref=location.reference_id))
            continue

        api_status = payload.get("status") if isinstance(payload, dict) else None
        results = payload.get("results", []) if isinstance(payload, dict) else []
        if api_status != "OK" or not results:
            message = _safe_api_message(
                payload,
                "Google could not resolve this location."
                if api_status == "ZERO_RESULTS"
                else "The geocoding request failed.",
            )
            errors.append(
                {
                    "code": "LOCATION_NOT_FOUND"
                    if api_status == "ZERO_RESULTS"
                    else "GEOCODING_FAILED",
                    "message": message,
                    "input_ref": location.reference_id,
                    "retryable": api_status in {"UNKNOWN_ERROR", "OVER_QUERY_LIMIT"},
                }
            )
            continue

        result = results[0]
        geometry = result.get("geometry", {})
        google_location = geometry.get("location", {})
        resolved.append(
            {
                "reference_id": location.reference_id,
                "name": location.name,
                "input_address": location.address,
                "formatted_address": result.get("formatted_address"),
                "place_id": result.get("place_id"),
                "coordinates": {
                    "latitude": google_location.get("lat"),
                    "longitude": google_location.get("lng"),
                },
                "location_type": geometry.get("location_type"),
                "types": result.get("types", []),
                "partial_match": bool(result.get("partial_match", False)),
                "address_components": result.get("address_components", []),
                "source": location.source,
            }
        )
        if len(results) > 1 or result.get("partial_match"):
            warnings.append(
                {
                    "code": "AMBIGUOUS_LOCATION",
                    "message": "The location has multiple or partial Google matches.",
                    "input_ref": location.reference_id,
                    "alternative_place_ids": [
                        item.get("place_id") for item in results[1:5]
                    ],
                }
            )

    status = "success" if not errors and not warnings else "partial" if resolved else "error"
    return {
        "status": status,
        "resolved_locations": resolved,
        "warnings": warnings,
        "errors": errors,
        "provenance": _provenance(product),
    }


def _places_field_mask(search_mode: str, detail_level: str) -> str:
    fields = (
        PLACE_OPERATIONAL_FIELDS
        if detail_level == "operational"
        else PLACE_BASIC_FIELDS
    )
    if search_mode == "details":
        return fields
    return ",".join(f"places.{field}" for field in fields.split(","))


def _normalize_place(
    place: dict[str, Any], input_reference_id: str | None
) -> dict[str, Any]:
    display_name = place.get("displayName")
    if isinstance(display_name, dict):
        display_name = display_name.get("text")
    return {
        "input_reference_id": input_reference_id,
        "place_id": place.get("id"),
        "display_name": display_name,
        "formatted_address": place.get("formattedAddress"),
        "coordinates": place.get("location"),
        "types": place.get("types", []),
        "primary_type": place.get("primaryType"),
        "business_status": place.get("businessStatus"),
        "google_maps_uri": place.get("googleMapsUri"),
        "current_opening_hours": place.get("currentOpeningHours"),
        "regular_opening_hours": place.get("regularOpeningHours"),
        "utc_offset_minutes": place.get("utcOffsetMinutes"),
        "time_zone": place.get("timeZone"),
        "entrances": place.get("entrances", []),
        "navigation_points": place.get("navigationPoints", []),
        "address_descriptor": place.get("addressDescriptor"),
        "attributions": place.get("attributions", []),
    }


def search_places(
    query: str | None,
    search_mode: Literal["text", "nearby", "details"],
    tool_context: ToolContext,
    location_context: LocationContext | None = None,
    included_types: list[str] | None = None,
    max_results: int = 5,
    detail_level: Literal["basic", "operational"] = "basic",
    place_id: str | None = None,
    reference_id: str | None = None,
) -> dict[str, Any]:
    """Search or verify places with Places API (New).

    Args:
        query: Natural-language text for Text Search.
        search_mode: Text Search, Nearby Search, or Place Details.
        location_context: Optional geographic bias or restriction.
        included_types: Optional standard Google place types for Nearby Search.
        max_results: Maximum results from 1 to 20.
        detail_level: Basic fields or additional operational fields.
        place_id: Required Google Place ID for Place Details.
        reference_id: Optional organizational record ID associated with the search.
    """
    _ = tool_context
    product = "Places API (New)"
    try:
        context = (
            _coerce_model(location_context, LocationContext)
            if location_context is not None
            else None
        )
    except ValidationError:
        error = MapsToolError(
            "INVALID_LOCATION_CONTEXT", "The Places location context is invalid."
        )
        return {
            "status": "error",
            "places": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }
    if not 1 <= max_results <= 20:
        error = MapsToolError(
            "INVALID_RESULT_LIMIT", "max_results must be between 1 and 20."
        )
        return {
            "status": "error",
            "places": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }

    url: str
    body: dict[str, Any] | None = None
    method = "POST"
    if search_mode == "text":
        if not isinstance(query, str) or not query.strip():
            error = MapsToolError(
                "INVALID_PLACE_QUERY", "Text Search requires a non-empty query."
            )
            return {
                "status": "error",
                "places": [],
                "errors": [_error_payload(error)],
                "provenance": _provenance(product),
            }
        url = PLACES_TEXT_URL
        body = {"textQuery": query.strip(), "maxResultCount": max_results}
        if context and context.rectangle_low and context.rectangle_high:
            body["locationRestriction"] = {
                "rectangle": {
                    "low": context.rectangle_low.model_dump(),
                    "high": context.rectangle_high.model_dump(),
                }
            }
        elif context and context.center:
            circle = {
                "center": context.center.model_dump(),
                "radius": context.radius_meters or 5_000,
            }
            body["locationBias"] = {"circle": circle}
        if context and context.region_code:
            body["regionCode"] = context.region_code.upper()
        if context and context.language_code:
            body["languageCode"] = context.language_code
    elif search_mode == "nearby":
        if not context or not context.center:
            error = MapsToolError(
                "INVALID_LOCATION_CONTEXT",
                "Nearby Search requires center coordinates.",
            )
            return {
                "status": "error",
                "places": [],
                "errors": [_error_payload(error)],
                "provenance": _provenance(product),
            }
        url = PLACES_NEARBY_URL
        body = {
            "maxResultCount": max_results,
            "locationRestriction": {
                "circle": {
                    "center": context.center.model_dump(),
                    "radius": context.radius_meters or 5_000,
                }
            },
        }
        if included_types:
            body["includedTypes"] = included_types
        if context.language_code:
            body["languageCode"] = context.language_code
        if context.region_code:
            body["regionCode"] = context.region_code.upper()
    else:
        if not isinstance(place_id, str) or not place_id.strip():
            error = MapsToolError(
                "INVALID_PLACE_ID", "Place Details requires a Google Place ID."
            )
            return {
                "status": "error",
                "places": [],
                "errors": [_error_payload(error)],
                "provenance": _provenance(product),
            }
        method = "GET"
        url = PLACES_DETAILS_URL.format(place_id=quote(place_id.strip(), safe=""))

    try:
        payload = _request_json(
            method,
            url,
            product=product,
            json_body=body,
            field_mask=_places_field_mask(search_mode, detail_level),
        )
    except MapsToolError as error:
        return {
            "status": "error",
            "places": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }

    raw_places = (
        [payload]
        if search_mode == "details" and isinstance(payload, dict)
        else payload.get("places", [])
        if isinstance(payload, dict)
        else []
    )
    places = [_normalize_place(place, reference_id) for place in raw_places]
    return {
        "status": "success",
        "places": places,
        "warnings": []
        if places
        else [
            {
                "code": "NO_PLACES_FOUND",
                "message": "The Places search returned no results.",
            }
        ],
        "errors": [],
        "provenance": _provenance(product),
    }


def _coerce_route_options(value: Any) -> RouteOptions:
    return _coerce_model(value, RouteOptions)  # type: ignore[return-value]


def _route_body_options(options: RouteOptions) -> dict[str, Any]:
    body: dict[str, Any] = {
        "travelMode": options.travel_mode,
        "routeModifiers": {
            "avoidTolls": options.avoid_tolls,
            "avoidHighways": options.avoid_highways,
            "avoidFerries": options.avoid_ferries,
            "avoidIndoor": options.avoid_indoor,
        },
        "languageCode": options.language_code or "en",
        "units": options.units,
    }
    if options.routing_preference:
        body["routingPreference"] = options.routing_preference
    if options.departure_time:
        body["departureTime"] = options.departure_time.isoformat()
    if options.arrival_time:
        body["arrivalTime"] = options.arrival_time.isoformat()
    if options.include_tolls:
        body["extraComputations"] = ["TOLLS"]
    return body


def compute_routes(
    origin: LocationReference,
    destination: LocationReference,
    waypoints: list[LocationReference],
    constraints: RouteOptions,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Compute route geometry, distance, duration, and journey facts.

    Args:
        origin: Resolved route origin.
        destination: Resolved route destination.
        waypoints: Ordered intermediate locations, if any.
        constraints: Travel mode, timing, avoidance, traffic, and toll options.
    """
    _ = tool_context
    product = "Routes API"
    if len(waypoints) > MAX_ROUTE_WAYPOINTS:
        error = MapsToolError(
            "TOO_MANY_WAYPOINTS",
            f"Routes API accepts at most {MAX_ROUTE_WAYPOINTS} intermediate waypoints.",
        )
        return {
            "status": "error",
            "routes": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }
    try:
        origin_model = _coerce_model(origin, LocationReference)
        destination_model = _coerce_model(destination, LocationReference)
        waypoint_models = [
            _coerce_model(item, LocationReference) for item in waypoints
        ]
        options = _coerce_route_options(constraints)
    except ValidationError:
        error = MapsToolError(
            "INVALID_ROUTE_REQUEST", "The route request or constraints are invalid."
        )
        return {
            "status": "error",
            "routes": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }

    body = {
        "origin": _waypoint(origin_model),
        "destination": _waypoint(destination_model),
        "intermediates": [_waypoint(item) for item in waypoint_models],
        "computeAlternativeRoutes": options.compute_alternative_routes,
        "optimizeWaypointOrder": options.optimize_waypoint_order,
        **_route_body_options(options),
    }
    try:
        payload = _request_json(
            "POST",
            ROUTES_URL,
            product=product,
            json_body=body,
            field_mask=ROUTE_FIELDS,
        )
    except MapsToolError as error:
        return {
            "status": "error",
            "routes": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }

    normalized_routes = []
    for index, route in enumerate(payload.get("routes", [])):
        legs = [
            {
                "distance_meters": leg.get("distanceMeters"),
                "duration_seconds": _duration_seconds(leg.get("duration")),
                "static_duration_seconds": _duration_seconds(
                    leg.get("staticDuration")
                ),
            }
            for leg in route.get("legs", [])
        ]
        normalized_routes.append(
            {
                "route_index": index,
                "origin_reference_id": origin_model.reference_id,
                "destination_reference_id": destination_model.reference_id,
                "waypoint_reference_ids": [
                    item.reference_id for item in waypoint_models
                ],
                "distance_meters": route.get("distanceMeters"),
                "duration_seconds": _duration_seconds(route.get("duration")),
                "static_duration_seconds": _duration_seconds(
                    route.get("staticDuration")
                ),
                "encoded_polyline": route.get("polyline", {}).get(
                    "encodedPolyline"
                ),
                "legs": legs,
                "optimized_waypoint_order": route.get(
                    "optimizedIntermediateWaypointIndex", []
                ),
                "route_labels": route.get("routeLabels", []),
                "toll_info": route.get("travelAdvisory", {}).get("tollInfo"),
            }
        )
    return {
        "status": "success" if normalized_routes else "partial",
        "routes": normalized_routes,
        "warnings": []
        if normalized_routes
        else [{"code": "ROUTE_NOT_FOUND", "message": "No route was found."}],
        "errors": [],
        "provenance": _provenance(product),
    }


def compute_route_matrix(
    origins: list[LocationReference],
    destinations: list[LocationReference],
    constraints: RouteOptions,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Compute travel times and distances between sets of locations.

    Args:
        origins: Resolved origin locations.
        destinations: Resolved destination locations.
        constraints: Travel mode, timing, avoidance, traffic, and toll options.
    """
    _ = tool_context
    product = "Routes API - Compute Route Matrix"
    if not origins or not destinations:
        error = MapsToolError(
            "EMPTY_ROUTE_MATRIX", "At least one origin and destination are required."
        )
        return {
            "status": "error",
            "matrix": None,
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }
    try:
        origin_models = [_coerce_model(item, LocationReference) for item in origins]
        destination_models = [
            _coerce_model(item, LocationReference) for item in destinations
        ]
        options = _coerce_route_options(constraints)
    except ValidationError:
        error = MapsToolError(
            "INVALID_MATRIX_REQUEST", "The route matrix request is invalid."
        )
        return {
            "status": "error",
            "matrix": None,
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }

    element_count = len(origin_models) * len(destination_models)
    element_limit = (
        MAX_RESTRICTED_MATRIX_ELEMENTS
        if options.travel_mode == "TRANSIT"
        or options.routing_preference == "TRAFFIC_AWARE_OPTIMAL"
        else MAX_MATRIX_ELEMENTS
    )
    address_or_place_count = sum(
        bool(item.address or item.place_id)
        for item in [*origin_models, *destination_models]
    )
    if element_count > element_limit or address_or_place_count > 50:
        error = MapsToolError(
            "ROUTE_MATRIX_LIMIT_EXCEEDED",
            f"This matrix exceeds the {element_limit}-element or 50 address/Place-ID limit.",
        )
        return {
            "status": "error",
            "matrix": None,
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }

    common_options = _route_body_options(options)
    modifiers = common_options.pop("routeModifiers")
    body = {
        "origins": [
            {"waypoint": _waypoint(item), "routeModifiers": modifiers}
            for item in origin_models
        ],
        "destinations": [
            {"waypoint": _waypoint(item)} for item in destination_models
        ],
        **common_options,
    }
    try:
        payload = _request_json(
            "POST",
            ROUTE_MATRIX_URL,
            product=product,
            json_body=body,
            field_mask=MATRIX_FIELDS,
        )
    except MapsToolError as error:
        return {
            "status": "error",
            "matrix": None,
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }

    elements = payload if isinstance(payload, list) else payload.get("elements", [])
    normalized: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for element in elements:
        origin_index = element.get("originIndex")
        destination_index = element.get("destinationIndex")
        status = element.get("status", {})
        condition = element.get("condition")
        item = {
            "origin_index": origin_index,
            "destination_index": destination_index,
            "origin_reference_id": origin_models[origin_index].reference_id
            if isinstance(origin_index, int) and origin_index < len(origin_models)
            else None,
            "destination_reference_id": destination_models[
                destination_index
            ].reference_id
            if isinstance(destination_index, int)
            and destination_index < len(destination_models)
            else None,
            "condition": condition,
            "distance_meters": element.get("distanceMeters"),
            "duration_seconds": _duration_seconds(element.get("duration")),
            "static_duration_seconds": _duration_seconds(
                element.get("staticDuration")
            ),
            "toll_info": element.get("travelAdvisory", {}).get("tollInfo"),
        }
        status_code = status.get("code", 0) if isinstance(status, dict) else 0
        if status_code or condition == "ROUTE_NOT_FOUND":
            errors.append(
                {
                    "code": "MATRIX_ELEMENT_FAILED",
                    "message": status.get("message", "No route exists for this pair")
                    if isinstance(status, dict)
                    else "No route exists for this pair",
                    "origin_index": origin_index,
                    "destination_index": destination_index,
                    "retryable": False,
                }
            )
        normalized.append(item)

    return {
        "status": "partial" if errors else "success",
        "matrix": {
            "origin_reference_ids": [item.reference_id for item in origin_models],
            "destination_reference_ids": [
                item.reference_id for item in destination_models
            ],
            "elements": normalized,
        },
        "errors": errors,
        "provenance": _provenance(product),
    }


def _weather_data(payload: dict[str, Any], data_type: str) -> Any:
    common_condition_fields = {
        "interval",
        "displayDateTime",
        "displayDate",
        "weatherCondition",
        "temperature",
        "feelsLikeTemperature",
        "minTemperature",
        "maxTemperature",
        "feelsLikeMinTemperature",
        "feelsLikeMaxTemperature",
        "relativeHumidity",
        "uvIndex",
        "precipitation",
        "thunderstormProbability",
        "wind",
        "visibility",
        "cloudCover",
        "daytimeForecast",
        "nighttimeForecast",
        "sunEvents",
    }
    if data_type == "current":
        allowed = {
            "currentTime",
            "timeZone",
            "weatherCondition",
            "temperature",
            "feelsLikeTemperature",
            "dewPoint",
            "heatIndex",
            "windChill",
            "relativeHumidity",
            "uvIndex",
            "precipitation",
            "thunderstormProbability",
            "airPressure",
            "wind",
            "visibility",
            "cloudCover",
        }
        return {key: payload[key] for key in allowed if key in payload}
    list_key = {
        "hourly": "forecastHours",
        "daily": "forecastDays",
        "history": "historyHours",
        "alerts": "weatherAlerts",
    }[data_type]
    items = payload.get(list_key, [])
    if data_type == "alerts":
        allowed = {
            "alertId",
            "alertTitle",
            "eventType",
            "areaName",
            "instruction",
            "safetyRecommendations",
            "timezoneOffset",
            "startTime",
            "expirationTime",
            "dataSource",
            "description",
            "severity",
            "certainty",
            "urgency",
        }
    else:
        allowed = common_condition_fields
    normalized_items = [
        {key: item[key] for key in allowed if key in item}
        for item in items
        if isinstance(item, dict)
    ]
    result: dict[str, Any] = {list_key: normalized_items}
    for key in ("timeZone", "regionCode"):
        if key in payload:
            result[key] = payload[key]
    return result


def get_weather_context(
    locations: list[LocationReference],
    data_types: list[Literal["current", "hourly", "daily", "history", "alerts"]],
    tool_context: ToolContext,
    hours: int = 24,
    days: int = 5,
    history_hours: int = 24,
    language_code: str = "en",
    units: Literal["METRIC", "IMPERIAL"] = "METRIC",
) -> dict[str, Any]:
    """Fetch selected weather context for resolved Mission locations.

    Args:
        locations: Resolved locations with coordinates.
        data_types: Current, hourly, daily, history, and/or alerts.
        hours: Hourly forecast horizon from 1 to 240.
        days: Daily forecast horizon from 1 to 10.
        history_hours: Historical horizon from 1 to 24.
        language_code: Response language code.
        units: Metric or imperial units.
    """
    _ = tool_context
    product = "Weather API"
    allowed_types = {"current", "hourly", "daily", "history", "alerts"}
    requested_types = list(dict.fromkeys(data_types))
    if (
        not requested_types
        or any(item not in allowed_types for item in requested_types)
        or not 1 <= hours <= 240
        or not 1 <= days <= 10
        or not 1 <= history_hours <= 24
    ):
        error = MapsToolError(
            "INVALID_WEATHER_REQUEST", "Weather types or forecast horizons are invalid."
        )
        return {
            "status": "error",
            "weather_context": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }

    context: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, value in enumerate(locations):
        try:
            location = _coerce_model(value, LocationReference)
        except ValidationError as error:
            errors.append(_location_error(index, error))
            continue
        if not location.coordinates:
            errors.append(
                {
                    "code": "COORDINATES_REQUIRED",
                    "message": "Weather requests require resolved coordinates.",
                    "input_ref": location.reference_id,
                    "retryable": False,
                }
            )
            continue
        location_result = {
            "reference_id": location.reference_id,
            "coordinates": location.coordinates.model_dump(),
            "data": {},
        }
        for data_type in requested_types:
            params: dict[str, Any] = {
                "location.latitude": location.coordinates.latitude,
                "location.longitude": location.coordinates.longitude,
                "languageCode": language_code,
                "unitsSystem": units,
            }
            if data_type == "hourly":
                params.update({"hours": hours, "pageSize": min(hours, 24)})
            elif data_type == "daily":
                params.update({"days": days, "pageSize": days})
            elif data_type == "history":
                params.update(
                    {"hours": history_hours, "pageSize": min(history_hours, 24)}
                )
            try:
                payload = _request_json(
                    "GET",
                    WEATHER_URLS[data_type],
                    product=product,
                    params=params,
                    key_in_query=True,
                )
                if data_type != "current" and isinstance(payload, dict):
                    list_key = {
                        "hourly": "forecastHours",
                        "daily": "forecastDays",
                        "history": "historyHours",
                        "alerts": "weatherAlerts",
                    }[data_type]
                    combined = list(payload.get(list_key, []))
                    next_page_token = payload.get("nextPageToken")
                    page_count = 1
                    while next_page_token and page_count < 10:
                        page_params = {**params, "pageToken": next_page_token}
                        page = _request_json(
                            "GET",
                            WEATHER_URLS[data_type],
                            product=product,
                            params=page_params,
                            key_in_query=True,
                        )
                        if not isinstance(page, dict):
                            break
                        combined.extend(page.get(list_key, []))
                        next_page_token = page.get("nextPageToken")
                        page_count += 1
                    payload = {**payload, list_key: combined}
                    payload.pop("nextPageToken", None)
            except MapsToolError as error:
                if error.code == "MAPS_KEY_MISSING":
                    return {
                        **_configuration_error(product),
                        "weather_context": context,
                    }
                errors.append(
                    _error_payload(
                        error, input_ref=f"{location.reference_id}:{data_type}"
                    )
                )
                continue
            location_result["data"][data_type] = _weather_data(payload, data_type)
        if location_result["data"]:
            context.append(location_result)

    status = "success" if not errors else "partial" if context else "error"
    return {
        "status": status,
        "weather_context": context,
        "errors": errors,
        "provenance": _provenance(product),
    }


def inspect_roads(
    operation: Literal["snap_to_roads", "nearest_roads", "speed_limits"],
    tool_context: ToolContext,
    points: list[GeographicCoordinate] | None = None,
    place_ids: list[str] | None = None,
    interpolate: bool = False,
    units: Literal["KPH", "MPH"] = "KPH",
    reference_id: str | None = None,
) -> dict[str, Any]:
    """Inspect GPS samples and road segments with the Roads API.

    Args:
        operation: Snap GPS points, find nearest roads, or fetch speed limits.
        points: Coordinates used by snap, nearest, or speed-limit operations.
        place_ids: Road-segment Place IDs used for speed limits.
        interpolate: Whether Snap to Roads should interpolate the path.
        units: KPH or MPH for speed-limit responses.
        reference_id: Optional organizational or journey record ID.
    """
    _ = tool_context
    product = "Roads API"
    try:
        point_models = [
            _coerce_model(item, GeographicCoordinate) for item in (points or [])
        ]
    except ValidationError:
        error = MapsToolError(
            "INVALID_ROAD_POINTS", "One or more road coordinates are invalid."
        )
        return {
            "status": "error",
            "road_context": None,
            "warnings": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }
    if len(point_models) > MAX_ROADS_POINTS:
        error = MapsToolError(
            "TOO_MANY_ROAD_POINTS",
            f"Roads API accepts at most {MAX_ROADS_POINTS} points per call.",
        )
        return {
            "status": "error",
            "road_context": None,
            "warnings": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }
    if operation in {"snap_to_roads", "nearest_roads"} and not point_models:
        error = MapsToolError(
            "ROAD_POINTS_REQUIRED", f"{operation} requires at least one coordinate."
        )
        return {
            "status": "error",
            "road_context": None,
            "warnings": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }
    if operation == "speed_limits" and not point_models and not place_ids:
        error = MapsToolError(
            "ROAD_REFERENCE_REQUIRED",
            "speed_limits requires coordinates or road-segment Place IDs.",
        )
        return {
            "status": "error",
            "road_context": None,
            "warnings": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }

    params: dict[str, Any] = {}
    if point_models:
        params["path" if operation != "nearest_roads" else "points"] = "|".join(
            f"{point.latitude},{point.longitude}" for point in point_models
        )
    if operation == "snap_to_roads":
        params["interpolate"] = str(interpolate).lower()
    if operation == "speed_limits":
        if place_ids:
            params["placeId"] = place_ids
        params["units"] = units

    try:
        payload = _request_json(
            "GET",
            ROADS_URLS[operation],
            product=product,
            params=params,
            key_in_query=True,
        )
    except MapsToolError as error:
        if operation == "speed_limits" and error.code == "MAPS_PERMISSION_DENIED":
            return {
                "status": "partial",
                "road_context": {
                    "operation": operation,
                    "reference_id": reference_id,
                    "results": [],
                },
                "warnings": [
                    {
                        "code": "SPEED_LIMITS_NOT_LICENSED",
                        "message": (
                            "Roads speed-limit data is unavailable for this project or license."
                        ),
                    }
                ],
                "errors": [],
                "provenance": _provenance(product),
            }
        return {
            "status": "error",
            "road_context": None,
            "warnings": [],
            "errors": [_error_payload(error)],
            "provenance": _provenance(product),
        }

    result_key = "speedLimits" if operation == "speed_limits" else "snappedPoints"
    results = payload.get(result_key, []) if isinstance(payload, dict) else []
    return {
        "status": "success" if results else "partial",
        "road_context": {
            "operation": operation,
            "reference_id": reference_id,
            "results": results,
            "units": units if operation == "speed_limits" else None,
        },
        "warnings": []
        if results
        else [
            {
                "code": "NO_ROAD_MATCH",
                "message": "The Roads API returned no matching road segments.",
            }
        ],
        "errors": [],
        "provenance": _provenance(product),
    }


__all__ = [
    "GeographicCoordinate",
    "GeospatialJourney",
    "LocationContext",
    "LocationReference",
    "PlanningWindow",
    "RouteOptions",
    "compute_route_matrix",
    "compute_routes",
    "geocode_locations",
    "get_weather_context",
    "inspect_roads",
    "search_places",
]
