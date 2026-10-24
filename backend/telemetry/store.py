"""Bounded DynamoDB Queries against the dedicated machine/time index."""

from boto3.dynamodb.types import TypeDeserializer

from .model import InvalidSample, normalize_sample


MAX_WINDOW_MS = 2 * 60 * 60 * 1000
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

    def latest(self, *, start=None, end=None):
        items, _, invalid = self.query(limit=1, start=start, end=end, ascending=False)
        return items[0] if items else None, invalid
