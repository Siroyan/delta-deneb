"""Bounded DynamoDB Queries against the dedicated machine/time index."""

from boto3.dynamodb.types import TypeDeserializer

from .model import InvalidSample, normalize_sample


MAX_WINDOW_MS = 2 * 60 * 60 * 1000
MAX_LAP_ITEMS = 10000
MAX_LAP_PAGES = 20
DESERIALIZER = TypeDeserializer()


class TelemetryStore:
    def __init__(self, client, *, table_name, index_name, machine_id):
        self.client = client
        self.table_name = table_name
        self.index_name = index_name
        self.machine_id = machine_id

    def query(self, *, limit, start=None, end=None, key=None, ascending=True):
        names = {"#machine": "machine_id"}
        values = {":machine": {"S": self.machine_id}}
        condition = "#machine = :machine"
        if start is not None:
            names["#receipt"] = "received_at_ms"
            values.update({":start": {"N": str(start)}, ":end": {"N": str(end)}})
            condition += " AND #receipt BETWEEN :start AND :end"
        args = {
            "TableName": self.table_name, "IndexName": self.index_name,
            "KeyConditionExpression": condition,
            "ExpressionAttributeNames": names, "ExpressionAttributeValues": values,
            "Limit": limit, "ScanIndexForward": ascending, "ConsistentRead": False,
        }
        if key:
            args["ExclusiveStartKey"] = key
        response = self.client.query(**args)
        items, invalid = [], 0
        for raw in response.get("Items", []):
            native = {name: DESERIALIZER.deserialize(value) for name, value in raw.items()}
            try:
                item = normalize_sample(native, received_at_ms=native.get("received_at_ms"))
                if item["machine_id"] != self.machine_id:
                    raise InvalidSample("Unexpected machine")
                identifier = native.get("sample_id")
                if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
                    raise InvalidSample("Invalid sample identifier")
                item["sample_id"] = identifier
                items.append(item)
            except InvalidSample:
                invalid += 1
        return items, response.get("LastEvaluatedKey"), invalid

    def latest(self):
        items, _, invalid = self.query(limit=1, ascending=False)
        return items[0] if items else None, invalid

    def laps_window(self, *, start, end):
        # Read newest first so a cap always retains the most recent run.
        items, key, invalid, evaluated, pages = [], None, 0, 0, 0
        while evaluated < MAX_LAP_ITEMS and pages < MAX_LAP_PAGES:
            page, key, skipped = self.query(
                limit=min(1000, MAX_LAP_ITEMS - evaluated), start=start, end=end,
                key=key, ascending=False,
            )
            items.extend(page)
            pages += 1
            invalid += skipped
            evaluated += len(page) + skipped
            if not key:
                break
            if not page and not skipped:
                # No filters are used: an empty indexed page with a key is an
                # unexpected response, so never loop indefinitely.
                break
        items.sort(key=lambda item: (item["received_at_ms"], item["timestamp_ms"], item["sample_id"]))
        return items, bool(key), invalid
