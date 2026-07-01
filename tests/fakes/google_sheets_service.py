"""Minimal fake Google Sheets service for unit tests."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass
class FakeResponse:
    status: int


class FakeGoogleApiError(Exception):
    """Fake Google HTTP error with safe status extraction."""

    def __init__(self, status: int, marker: str = "fake body should stay hidden") -> None:
        super().__init__(marker)
        self.resp = FakeResponse(status)
        self.content = marker


class FakeGoogleTransportError(OSError):
    """Fake retryable transport failure."""

    transport_error = True


class FakeGoogleRequest:
    """Executable fake request."""

    def __init__(
        self,
        service: "FakeGoogleSheetsService",
        operation: str,
        action: Any,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.service = service
        self.operation = operation
        self.action = action
        self.payload = copy.deepcopy(dict(payload or {}))

    def execute(self, *, num_retries: int = 0) -> Any:
        self.service.execute_count += 1
        self.service.execute_num_retries.append(num_retries)
        self.service.request_log.append((self.operation, copy.deepcopy(self.payload)))
        failure = self.service.pop_failure(self.operation)
        if failure is not None:
            raise failure
        if self.operation == "batchUpdate" and self.service.fail_batch_after_apply is not None:
            self.action()
            raise self.service.fail_batch_after_apply
        if self.operation == "create" and self.service.fail_create_after_apply is not None:
            self.action()
            raise self.service.fail_create_after_apply
        return copy.deepcopy(self.action())


class FakeValuesResource:
    def __init__(self, service: "FakeGoogleSheetsService") -> None:
        self.service = service

    def batchGet(
        self,
        *,
        spreadsheetId: str,
        ranges: Sequence[str],
        majorDimension: str | None = None,
        valueRenderOption: str | None = None,
    ) -> FakeGoogleRequest:
        self.service.batch_get_requests.append(
            {
                "spreadsheetId": spreadsheetId,
                "ranges": list(ranges),
                "majorDimension": majorDimension,
                "valueRenderOption": valueRenderOption,
            }
        )

        def action() -> dict[str, Any]:
            if self.service.malformed_values_response:
                return {"valueRanges": "not-a-list"}
            return {
                "valueRanges": [
                    {"range": item, "values": self.service.values_for_range(item)}
                    for item in ranges
                ]
            }

        return FakeGoogleRequest(
            self.service,
            "values.batchGet",
            action,
            payload={"spreadsheetId": spreadsheetId, "ranges": list(ranges)},
        )


class FakeSpreadsheetsResource:
    def __init__(self, service: "FakeGoogleSheetsService") -> None:
        self.service = service

    def get(self, *, spreadsheetId: str, fields: str | None = None) -> FakeGoogleRequest:
        self.service.get_requests.append({"spreadsheetId": spreadsheetId, "fields": fields})

        def action() -> dict[str, Any]:
            if self.service.malformed_get_response:
                return {"sheets": "not-a-list"}
            return {
                "spreadsheetId": self.service.spreadsheet_id,
                "sheets": [
                    {"properties": {"sheetId": sheet_id, "title": title}}
                    for title, sheet_id in self.service.sheet_ids.items()
                ],
            }

        return FakeGoogleRequest(
            self.service,
            "get",
            action,
            payload={"spreadsheetId": spreadsheetId, "fields": fields},
        )

    def values(self) -> FakeValuesResource:
        return FakeValuesResource(self.service)

    def batchUpdate(self, *, spreadsheetId: str, body: Mapping[str, Any]) -> FakeGoogleRequest:
        copied_body = copy.deepcopy(dict(body))
        self.service.batch_update_requests.append(
            {"spreadsheetId": spreadsheetId, "body": copied_body}
        )

        def action() -> dict[str, Any]:
            self.service.apply_batch_update(copied_body)
            return {"spreadsheetId": self.service.spreadsheet_id, "replies": []}

        return FakeGoogleRequest(
            self.service,
            "batchUpdate",
            action,
            payload={"spreadsheetId": spreadsheetId, "body": copied_body},
        )

    def create(self, *, body: Mapping[str, Any]) -> FakeGoogleRequest:
        copied_body = copy.deepcopy(dict(body))
        self.service.create_requests.append({"body": copied_body})

        def action() -> dict[str, Any]:
            if self.service.malformed_create_response:
                return {"properties": {"title": copied_body.get("properties", {}).get("title", "")}}
            title = str(copied_body.get("properties", {}).get("title", ""))
            self.service.spreadsheet_id = f"created-{self.service.create_count + 1}"
            self.service.create_count += 1
            self.service.sheet_ids = {}
            self.service.rows = {}
            self.service.title = title
            return {"spreadsheetId": self.service.spreadsheet_id}

        return FakeGoogleRequest(self.service, "create", action, payload={"body": copied_body})


class FakeGoogleSheetsService:
    """Small in-memory fake for the subset of Sheets API used by the backend."""

    def __init__(
        self,
        *,
        spreadsheet_id: str = "fake-google-spreadsheet",
        sheets: Mapping[str, Sequence[Sequence[str]]] | None = None,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.title = "Fake"
        self.sheet_ids: dict[str, int] = {}
        self.rows: dict[str, list[list[str]]] = {}
        for index, (title, rows) in enumerate((sheets or self.default_sheets()).items()):
            self.sheet_ids[str(title)] = index + 1
            self.rows[str(title)] = [list(row) for row in rows]
        self.execute_count = 0
        self.execute_num_retries: list[int] = []
        self.request_log: list[tuple[str, dict[str, Any]]] = []
        self.get_requests: list[dict[str, Any]] = []
        self.batch_get_requests: list[dict[str, Any]] = []
        self.batch_update_requests: list[dict[str, Any]] = []
        self.create_requests: list[dict[str, Any]] = []
        self.create_count = 0
        self.failures: dict[str, list[BaseException]] = {}
        self.fail_batch_after_apply: BaseException | None = None
        self.fail_create_after_apply: BaseException | None = None
        self.after_batch_apply_hook: Any | None = None
        self.malformed_get_response = False
        self.malformed_values_response = False
        self.malformed_create_response = False

    @staticmethod
    def default_sheets() -> dict[str, list[list[str]]]:
        return {
            "Metadata": [["key", "value", "updated_at", "notes"]],
            "Features": [["Feature ID", "Title"]],
        }

    def spreadsheets(self) -> FakeSpreadsheetsResource:
        return FakeSpreadsheetsResource(self)

    def add_failure(self, operation: str, error: BaseException) -> None:
        self.failures.setdefault(operation, []).append(error)

    def pop_failure(self, operation: str) -> BaseException | None:
        failures = self.failures.get(operation)
        if failures:
            return failures.pop(0)
        return None

    def values_for_range(self, a1_range: str) -> list[list[str]]:
        title, selector = self.parse_a1_range(a1_range)
        rows = self.rows.get(title, [])
        if selector == "A:D":
            return copy.deepcopy([row[:4] for row in rows])
        if selector == "1:1":
            return copy.deepcopy(rows[:1])
        raise AssertionError(f"unsupported fake range: {a1_range}")

    def apply_batch_update(self, body: Mapping[str, Any]) -> None:
        requests = body.get("requests")
        if not isinstance(requests, list):
            raise AssertionError("batchUpdate requests must be a list")
        for request in requests:
            if "addSheet" in request:
                properties = request["addSheet"]["properties"]
                self.sheet_ids[str(properties["title"])] = int(properties["sheetId"])
                self.rows[str(properties["title"])] = []
            elif "updateCells" in request:
                update = request["updateCells"]
                start = update["start"]
                title = self.title_for_sheet_id(int(start["sheetId"]))
                row_index = int(start.get("rowIndex", 0))
                column_index = int(start.get("columnIndex", 0))
                values = update["rows"][0]["values"]
                self.ensure_cell(title, row_index, column_index + len(values) - 1)
                for offset, cell in enumerate(values):
                    user_value = cell.get("userEnteredValue", {})
                    self.rows[title][row_index][column_index + offset] = str(
                        user_value.get("stringValue", "")
                    )
            else:
                raise AssertionError(f"unsupported fake request: {request}")
        if self.after_batch_apply_hook is not None:
            self.after_batch_apply_hook(self)

    def ensure_cell(self, title: str, row_index: int, column_index: int) -> None:
        while len(self.rows[title]) <= row_index:
            self.rows[title].append([])
        while len(self.rows[title][row_index]) <= column_index:
            self.rows[title][row_index].append("")

    def title_for_sheet_id(self, sheet_id: int) -> str:
        for title, current_id in self.sheet_ids.items():
            if current_id == sheet_id:
                return title
        raise AssertionError(f"unknown sheet id: {sheet_id}")

    def parse_a1_range(self, a1_range: str) -> tuple[str, str]:
        if not a1_range.startswith("'") or "'!" not in a1_range:
            raise AssertionError(f"unquoted A1 range: {a1_range}")
        title_part, selector = a1_range.rsplit("'!", 1)
        title = title_part[1:].replace("''", "'")
        return title, selector

    def snapshot(self) -> dict[str, list[list[str]]]:
        return copy.deepcopy(self.rows)
