from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIRECTORY))

from google.adk.tools.function_tool import FunctionTool  # noqa: E402

from geoagent.agent import GeospatialFindings  # noqa: E402
from geoagent.agent import GeospatialRequest  # noqa: E402
from geoagent.geospatial_tools import MapsToolError  # noqa: E402
from geoagent.geospatial_tools import _request_json  # noqa: E402
from geoagent.geospatial_tools import compute_route_matrix  # noqa: E402
from geoagent.geospatial_tools import compute_routes  # noqa: E402
from geoagent.geospatial_tools import geocode_locations  # noqa: E402
from geoagent.geospatial_tools import get_weather_context  # noqa: E402
from geoagent.geospatial_tools import inspect_roads  # noqa: E402
from geoagent.geospatial_tools import search_places  # noqa: E402


class GeospatialToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = SimpleNamespace(state={})
        self.key_patch = patch.dict(
            os.environ, {"GOOGLE_MAPS_API_KEY": "test-maps-key"}, clear=False
        )
        self.key_patch.start()

    def tearDown(self) -> None:
        self.key_patch.stop()

    @patch("geoagent.geospatial_tools._request_json")
    def test_geocoding_success_ambiguity_and_partial_batch(self, request_json) -> None:
        request_json.side_effect = [
            {
                "status": "OK",
                "results": [
                    {
                        "formatted_address": "Kalamassery, Kerala, India",
                        "place_id": "place-depot",
                        "types": ["locality"],
                        "partial_match": True,
                        "geometry": {
                            "location": {"lat": 10.05, "lng": 76.31},
                            "location_type": "APPROXIMATE",
                        },
                    },
                    {"place_id": "alternative-place"},
                ],
            },
            {"status": "ZERO_RESULTS", "results": []},
        ]

        result = geocode_locations(
            [
                {
                    "reference_id": "LOC-1",
                    "address": "Kalamassery, Kerala",
                    "source": {"source_id": "src-1", "record_id": "LOC-1"},
                },
                {"reference_id": "LOC-2", "address": "not a real location"},
                {"reference_id": "LOC-3"},
            ],
            self.context,
            region_code="IN",
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["resolved_locations"][0]["reference_id"], "LOC-1")
        self.assertEqual(
            result["resolved_locations"][0]["source"]["source_id"], "src-1"
        )
        self.assertEqual(result["warnings"][0]["code"], "AMBIGUOUS_LOCATION")
        self.assertEqual(
            {error["code"] for error in result["errors"]},
            {"LOCATION_NOT_FOUND", "INVALID_LOCATION"},
        )
        self.assertNotIn("test-maps-key", str(result))

    @patch("geoagent.geospatial_tools._request_json")
    def test_places_text_nearby_and_details_use_new_api_and_masks(
        self, request_json
    ) -> None:
        request_json.return_value = {
            "places": [
                {
                    "id": "place-1",
                    "displayName": {"text": "Operations Depot"},
                    "formattedAddress": "Kochi, Kerala",
                    "location": {"latitude": 9.98, "longitude": 76.28},
                    "businessStatus": "OPERATIONAL",
                }
            ]
        }
        text_result = search_places(
            "distribution depot",
            "text",
            self.context,
            location_context={
                "center": {"latitude": 9.98, "longitude": 76.28},
                "radius_meters": 5000,
                "region_code": "IN",
            },
            detail_level="operational",
            reference_id="LOC-DEPOT",
        )
        self.assertEqual(text_result["status"], "success")
        self.assertEqual(
            text_result["places"][0]["input_reference_id"], "LOC-DEPOT"
        )
        text_call = request_json.call_args
        self.assertEqual(text_call.args[1], "https://places.googleapis.com/v1/places:searchText")
        self.assertNotIn("*", text_call.kwargs["field_mask"])
        self.assertIn("places.entrances", text_call.kwargs["field_mask"])

        request_json.return_value = {"places": []}
        nearby_result = search_places(
            None,
            "nearby",
            self.context,
            location_context={
                "center": {"latitude": 9.98, "longitude": 76.28},
                "radius_meters": 1000,
            },
            included_types=["hospital"],
        )
        self.assertEqual(nearby_result["status"], "success")
        self.assertEqual(nearby_result["warnings"][0]["code"], "NO_PLACES_FOUND")
        self.assertEqual(
            request_json.call_args.kwargs["json_body"]["includedTypes"],
            ["hospital"],
        )

        request_json.return_value = {
            "id": "place-2",
            "displayName": {"text": "Customer"},
            "location": {"latitude": 10.0, "longitude": 76.3},
        }
        details_result = search_places(
            None, "details", self.context, place_id="place-2"
        )
        self.assertEqual(details_result["places"][0]["place_id"], "place-2")
        self.assertTrue(request_json.call_args.args[1].endswith("/place-2"))

    @patch("geoagent.geospatial_tools._request_json")
    def test_route_normalization_and_constraints(self, request_json) -> None:
        request_json.return_value = {
            "routes": [
                {
                    "distanceMeters": 12345,
                    "duration": "900s",
                    "staticDuration": "840s",
                    "polyline": {"encodedPolyline": "encoded"},
                    "legs": [
                        {
                            "distanceMeters": 12345,
                            "duration": "900s",
                            "staticDuration": "840s",
                        }
                    ],
                    "optimizedIntermediateWaypointIndex": [0],
                    "travelAdvisory": {
                        "tollInfo": {"estimatedPrice": [{"currencyCode": "INR"}]}
                    },
                }
            ]
        }
        result = compute_routes(
            {
                "reference_id": "origin",
                "coordinates": {"latitude": 10.05, "longitude": 76.31},
            },
            {
                "reference_id": "destination",
                "place_id": "destination-place",
            },
            [{"reference_id": "stop", "address": "Kakkanad, Kerala"}],
            {
                "travel_mode": "DRIVE",
                "routing_preference": "TRAFFIC_AWARE",
                "include_tolls": True,
                "optimize_waypoint_order": True,
            },
            self.context,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["routes"][0]["duration_seconds"], 900)
        self.assertEqual(result["routes"][0]["encoded_polyline"], "encoded")
        body = request_json.call_args.kwargs["json_body"]
        self.assertEqual(body["routingPreference"], "TRAFFIC_AWARE")
        self.assertEqual(body["extraComputations"], ["TOLLS"])
        self.assertNotIn("*", request_json.call_args.kwargs["field_mask"])

        oversized = compute_routes(
            {"reference_id": "o", "name": "Origin"},
            {"reference_id": "d", "name": "Destination"},
            [
                {"reference_id": f"w-{index}", "name": f"Waypoint {index}"}
                for index in range(26)
            ],
            {},
            self.context,
        )
        self.assertEqual(oversized["errors"][0]["code"], "TOO_MANY_WAYPOINTS")

    @patch("geoagent.geospatial_tools._request_json")
    def test_route_matrix_indexes_failures_and_enforces_limits(self, request_json) -> None:
        request_json.return_value = [
            {
                "originIndex": 0,
                "destinationIndex": 0,
                "condition": "ROUTE_EXISTS",
                "distanceMeters": 1000,
                "duration": "120s",
                "status": {},
            },
            {
                "originIndex": 0,
                "destinationIndex": 1,
                "condition": "ROUTE_NOT_FOUND",
                "status": {"code": 5, "message": "No route"},
            },
        ]
        result = compute_route_matrix(
            [{"reference_id": "o", "coordinates": {"latitude": 10, "longitude": 76}}],
            [
                {"reference_id": "d1", "coordinates": {"latitude": 10.1, "longitude": 76.1}},
                {"reference_id": "d2", "coordinates": {"latitude": 10.2, "longitude": 76.2}},
            ],
            {"travel_mode": "DRIVE"},
            self.context,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["matrix"]["elements"][0]["duration_seconds"], 120)
        self.assertEqual(result["errors"][0]["code"], "MATRIX_ELEMENT_FAILED")
        self.assertIn("status", request_json.call_args.kwargs["field_mask"])

        origins = [
            {"reference_id": f"o-{index}", "coordinates": {"latitude": 10, "longitude": 76}}
            for index in range(11)
        ]
        destinations = [
            {"reference_id": f"d-{index}", "coordinates": {"latitude": 11, "longitude": 77}}
            for index in range(10)
        ]
        limited = compute_route_matrix(
            origins,
            destinations,
            {
                "travel_mode": "DRIVE",
                "routing_preference": "TRAFFIC_AWARE_OPTIMAL",
            },
            self.context,
        )
        self.assertEqual(
            limited["errors"][0]["code"], "ROUTE_MATRIX_LIMIT_EXCEEDED"
        )

    @patch("geoagent.geospatial_tools._request_json")
    def test_weather_selects_operations_and_normalizes_results(self, request_json) -> None:
        request_json.side_effect = [
            {
                "currentTime": "2026-08-27T10:00:00Z",
                "temperature": {"degrees": 29, "unit": "CELSIUS"},
                "relativeHumidity": 80,
                "unknownRawField": "excluded",
            },
            {
                "forecastHours": [{"interval": {"startTime": "2026-08-28T00:00:00Z"}}],
                "timeZone": {"id": "Asia/Kolkata"},
            },
            {
                "forecastDays": [
                    {"displayDate": {"year": 2026, "month": 8, "day": 28}}
                ],
                "timeZone": {"id": "Asia/Kolkata"},
            },
            {
                "historyHours": [
                    {"interval": {"startTime": "2026-08-27T00:00:00Z"}}
                ],
                "timeZone": {"id": "Asia/Kolkata"},
            },
            {
                "weatherAlerts": [
                    {"alertId": "alert-1", "severity": "SEVERE"}
                ],
                "regionCode": "IN",
            },
        ]
        result = get_weather_context(
            [
                {
                    "reference_id": "LOC-1",
                    "coordinates": {"latitude": 10.05, "longitude": 76.31},
                }
            ],
            ["current", "hourly", "daily", "history", "alerts"],
            self.context,
            hours=12,
        )
        self.assertEqual(result["status"], "success")
        data = result["weather_context"][0]["data"]
        self.assertEqual(
            set(data), {"current", "hourly", "daily", "history", "alerts"}
        )
        self.assertNotIn("unknownRawField", data["current"])
        self.assertEqual(request_json.call_count, 5)

        unresolved = get_weather_context(
            [{"reference_id": "LOC-2", "address": "Kochi"}],
            ["current"],
            self.context,
        )
        self.assertEqual(unresolved["errors"][0]["code"], "COORDINATES_REQUIRED")

    @patch("geoagent.geospatial_tools._request_json")
    def test_roads_operations_and_speed_limit_license_warning(self, request_json) -> None:
        request_json.return_value = {
            "snappedPoints": [
                {
                    "location": {"latitude": 10.0, "longitude": 76.3},
                    "placeId": "road-place",
                }
            ]
        }
        result = inspect_roads(
            "snap_to_roads",
            self.context,
            points=[{"latitude": 10.0, "longitude": 76.3}],
            interpolate=True,
            reference_id="JOURNEY-1",
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["road_context"]["reference_id"], "JOURNEY-1")
        self.assertEqual(result["road_context"]["results"][0]["placeId"], "road-place")
        self.assertEqual(request_json.call_args.kwargs["params"]["interpolate"], "true")

        request_json.side_effect = MapsToolError(
            "MAPS_PERMISSION_DENIED",
            "Permission denied",
            http_status=403,
        )
        restricted = inspect_roads(
            "speed_limits", self.context, place_ids=["road-place"]
        )
        self.assertEqual(restricted["status"], "partial")
        self.assertEqual(
            restricted["warnings"][0]["code"], "SPEED_LIMITS_NOT_LICENSED"
        )

    def test_missing_key_fails_without_network_or_secret_leak(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = geocode_locations(
                [{"reference_id": "LOC-1", "address": "Kochi"}], self.context
            )
        self.assertEqual(result["errors"][0]["code"], "MAPS_KEY_MISSING")
        self.assertNotIn("test-maps-key", str(result))

    def test_http_retry_timeout_malformed_json_and_api_failures(self) -> None:
        request = httpx.Request("GET", "https://example.test")
        response_500 = httpx.Response(500, json={"error": {"message": "temporary"}}, request=request)
        response_ok = httpx.Response(200, json={"ok": True}, request=request)
        with patch.object(httpx.Client, "request", side_effect=[response_500, response_ok]) as mocked:
            result = _request_json("GET", "https://example.test", product="Test")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mocked.call_count, 2)

        with patch.object(
            httpx.Client,
            "request",
            side_effect=[httpx.ReadTimeout("timeout"), httpx.ReadTimeout("timeout")],
        ):
            with self.assertRaises(MapsToolError) as timeout_error:
                _request_json("GET", "https://example.test", product="Test")
        self.assertEqual(timeout_error.exception.code, "MAPS_TIMEOUT")

        malformed = httpx.Response(200, content=b"not-json", request=request)
        with patch.object(httpx.Client, "request", return_value=malformed):
            with self.assertRaises(MapsToolError) as malformed_error:
                _request_json("GET", "https://example.test", product="Test")
        self.assertEqual(malformed_error.exception.code, "INVALID_MAPS_RESPONSE")

        for status, code in (
            (400, "MAPS_REQUEST_INVALID"),
            (403, "MAPS_PERMISSION_DENIED"),
            (429, "MAPS_RATE_LIMITED"),
        ):
            with self.subTest(status=status):
                failed = httpx.Response(
                    status,
                    json={"error": {"message": "failed test-maps-key"}},
                    request=request,
                )
                with patch.object(
                    httpx.Client, "request", side_effect=[failed, failed]
                ):
                    with self.assertRaises(MapsToolError) as api_error:
                        _request_json("GET", "https://example.test", product="Test")
                self.assertEqual(api_error.exception.code, code)
                self.assertNotIn("test-maps-key", api_error.exception.message)

    def test_adk_declarations_hide_context_and_schemas_validate(self) -> None:
        for function in (
            geocode_locations,
            search_places,
            compute_routes,
            compute_route_matrix,
            get_weather_context,
            inspect_roads,
        ):
            declaration = FunctionTool(function)._get_declaration()
            self.assertNotIn(
                "tool_context", declaration.parameters_json_schema["properties"]
            )

        request = GeospatialRequest.model_validate(
            {
                "objective": "Inspect tomorrow's physical operations.",
                "locations": [
                    {"reference_id": "LOC-1", "address": "Kochi, Kerala"}
                ],
                "journeys": [
                    {
                        "journey_id": "J-1",
                        "origin_reference_id": "LOC-1",
                        "destination_reference_id": "LOC-2",
                    }
                ],
                "planning_window": {
                    "start_at": "2026-08-28T06:30:00+05:30",
                    "end_at": "2026-08-28T18:00:00+05:30",
                    "timezone": "Asia/Kolkata",
                },
            }
        )
        self.assertEqual(request.locations[0].reference_id, "LOC-1")
        findings = GeospatialFindings.model_validate(
            {
                "resolved_locations": [],
                "places": [],
                "routes": [],
                "travel_matrices": [],
                "weather_context": [],
                "road_context": [],
                "warnings": [
                    {"code": "INFO", "message": "Informational context only."}
                ],
                "unresolved": [],
                "provenance": [],
            }
        )
        self.assertEqual(findings.warnings[0].code, "INFO")


@unittest.skipUnless(
    os.getenv("RUN_LIVE_MAPS_TESTS") == "1" and os.getenv("GOOGLE_MAPS_API_KEY"),
    "set RUN_LIVE_MAPS_TESTS=1 with GOOGLE_MAPS_API_KEY to run",
)
class LiveMapsSmokeTest(unittest.TestCase):
    def test_one_small_request_per_product(self) -> None:
        context = SimpleNamespace(state={})
        geocoded = geocode_locations(
            [
                {
                    "reference_id": "depot",
                    "address": "Kalamassery, Kerala 683104, India",
                }
            ],
            context,
            region_code="IN",
        )
        self.assertIn(geocoded["status"], {"success", "partial"})
        coordinate = geocoded["resolved_locations"][0]["coordinates"]

        places = search_places(
            "hospital",
            "text",
            context,
            location_context={"center": coordinate, "radius_meters": 5000},
            max_results=1,
        )
        self.assertIn(places["status"], {"success", "partial"})

        destination = {
            "reference_id": "destination",
            "coordinates": {"latitude": 10.0159, "longitude": 76.3419},
        }
        route = compute_routes(
            {"reference_id": "depot", "coordinates": coordinate},
            destination,
            [],
            {"travel_mode": "DRIVE", "routing_preference": "TRAFFIC_AWARE"},
            context,
        )
        self.assertEqual(route["status"], "success")

        matrix = compute_route_matrix(
            [{"reference_id": "depot", "coordinates": coordinate}],
            [destination],
            {"travel_mode": "DRIVE"},
            context,
        )
        self.assertIn(matrix["status"], {"success", "partial"})

        weather = get_weather_context(
            [{"reference_id": "depot", "coordinates": coordinate}],
            ["current"],
            context,
        )
        self.assertEqual(weather["status"], "success")

        roads = inspect_roads(
            "nearest_roads", context, points=[coordinate]
        )
        self.assertEqual(roads["status"], "success")


if __name__ == "__main__":
    unittest.main()
