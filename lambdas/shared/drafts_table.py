"""Draft status records in DynamoDB. Every status change is a conditional write."""

from datetime import datetime, timezone

UPLOADED = "UPLOADED"
TRANSCRIBING = "TRANSCRIBING"
ANALYZING = "ANALYZING"
PENDING_REVIEW = "PENDING_REVIEW"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
WRITTEN = "WRITTEN"
FAILED = "FAILED"

TERMINAL = (REJECTED, WRITTEN, FAILED)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class DraftsTable:
    def __init__(self, table):
        """`table` is a boto3 DynamoDB Table resource."""
        self._table = table

    def get(self, draft_id: str) -> dict | None:
        return self._table.get_item(Key={"draftId": draft_id}, ConsistentRead=True).get("Item")

    def create(self, draft_id: str, status: str, attrs: dict) -> dict | None:
        """Insert a new draft; None if the id already exists."""
        now = utc_now()
        item = {**attrs, "draftId": draft_id, "status": status, "createdAt": now, "updatedAt": now}
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(draftId)")
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return None
        return item

    def scan(self, max_items: int = 500) -> list[dict]:
        """All drafts (demo scale), up to `max_items`."""
        items, kwargs = [], {}
        while len(items) < max_items:
            page = self._table.scan(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return items[:max_items]

    def transition(
        self,
        draft_id: str,
        to_status: str,
        *,
        allowed_from: tuple[str, ...],
        allow_missing: bool = False,
        attrs: dict | None = None,
    ) -> dict | None:
        """Move a draft to `to_status` if its current status is in `allowed_from`.

        Returns the updated item, or None when the condition fails (duplicate event,
        or the draft has already moved on).
        """
        now = utc_now()
        names = {"#status": "status", "#updatedAt": "updatedAt", "#createdAt": "createdAt"}
        values = {":to": to_status, ":now": now}
        sets = ["#status = :to", "#updatedAt = :now", "#createdAt = if_not_exists(#createdAt, :now)"]
        for i, (name, value) in enumerate((attrs or {}).items()):
            names[f"#a{i}"] = name
            values[f":a{i}"] = value
            sets.append(f"#a{i} = :a{i}")

        allowed = []
        for i, status in enumerate(allowed_from):
            values[f":f{i}"] = status
            allowed.append(f":f{i}")
        condition = f"#status IN ({', '.join(allowed)})"
        if allow_missing:
            condition = f"attribute_not_exists(draftId) OR {condition}"

        try:
            return self._table.update_item(
                Key={"draftId": draft_id},
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return None

    def fail(self, draft_id: str, error_code: str) -> dict | None:
        """Mark a draft FAILED with a short error code (never PHI)."""
        return self.transition(
            draft_id,
            FAILED,
            allowed_from=(UPLOADED, TRANSCRIBING, ANALYZING, PENDING_REVIEW, APPROVED),
            allow_missing=True,
            attrs={"errorCode": error_code[:64]},
        )

    # --- approval: PENDING_REVIEW -> APPROVED (writing, resumable) -> WRITTEN -----------------
    # A short write lock stops two approve calls from writing the same draft at once;
    # healthLakeIds records each write as it happens, so a retry only writes what is missing.

    def _update(self, draft_id: str, **kwargs) -> dict | None:
        try:
            return self._table.update_item(Key={"draftId": draft_id}, ReturnValues="ALL_NEW", **kwargs)["Attributes"]
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return None

    def start_approval(self, draft_id: str, approved_ids: list[str], user_sub: str,
                       now_epoch: int, lock_seconds: int) -> dict | None:
        return self._update(
            draft_id,
            UpdateExpression=("SET #s = :approved, approvedIds = :ids, approvedBy = :by, approvedAt = :now, "
                              "healthLakeIds = :empty, lockUntil = :lock, updatedAt = :now"),
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":approved": APPROVED, ":pending": PENDING_REVIEW, ":ids": approved_ids,
                                       ":by": user_sub, ":now": utc_now(), ":empty": {},
                                       ":lock": now_epoch + lock_seconds},
        )

    def acquire_write_lock(self, draft_id: str, now_epoch: int, lock_seconds: int) -> dict | None:
        return self._update(
            draft_id,
            UpdateExpression="SET lockUntil = :lock, updatedAt = :now",
            ConditionExpression="#s = :approved AND (attribute_not_exists(lockUntil) OR lockUntil < :epoch)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":approved": APPROVED, ":lock": now_epoch + lock_seconds,
                                       ":epoch": now_epoch, ":now": utc_now()},
        )

    def record_written(self, draft_id: str, key: str, reference: str) -> None:
        self._update(
            draft_id,
            UpdateExpression="SET healthLakeIds.#k = :ref, updatedAt = :now",
            ConditionExpression="#s = :approved",
            ExpressionAttributeNames={"#s": "status", "#k": key},
            ExpressionAttributeValues={":ref": reference, ":approved": APPROVED, ":now": utc_now()},
        )

    def release_write_lock(self, draft_id: str, error_code: str) -> None:
        self._update(
            draft_id,
            UpdateExpression="REMOVE lockUntil SET lastErrorCode = :code, updatedAt = :now",
            ConditionExpression="#s = :approved",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":approved": APPROVED, ":code": error_code[:64], ":now": utc_now()},
        )

    def finish_written(self, draft_id: str) -> dict | None:
        return self._update(
            draft_id,
            UpdateExpression="REMOVE lockUntil, lastErrorCode SET #s = :written, writtenAt = :now, updatedAt = :now",
            ConditionExpression="#s = :approved",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":approved": APPROVED, ":written": WRITTEN, ":now": utc_now()},
        )

    # --- engines and notes (string sets) -------------------------------------------------------

    def complete_engine(self, draft_id: str, engine: str) -> dict | None:
        """Remove `engine` from pendingEngines. None if it was not pending (duplicate event).
        The returned item has no pendingEngines once every engine has finished."""
        return self._update(
            draft_id,
            UpdateExpression="DELETE pendingEngines :set SET updatedAt = :now",
            ConditionExpression="contains(pendingEngines, :engine)",
            ExpressionAttributeValues={":set": {engine}, ":engine": engine, ":now": utc_now()},
        )

    def add_pending_note(self, draft_id: str, template: str) -> dict | None:
        """Mark a note template as generating; None unless the draft is waiting for review and
        that template is not already generating."""
        return self._update(
            draft_id,
            UpdateExpression="ADD pendingNotes :set SET updatedAt = :now",
            ConditionExpression="#s = :pending AND NOT contains(pendingNotes, :template)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":set": {template}, ":template": template, ":pending": PENDING_REVIEW,
                                       ":now": utc_now()},
        )

    def finish_pending_note(self, draft_id: str, template: str, error_code: str | None = None) -> None:
        expression = "DELETE pendingNotes :set SET updatedAt = :now"
        values = {":set": {template}, ":now": utc_now()}
        if error_code:
            expression += ", lastNoteError = :error"
            values[":error"] = f"{template}:{error_code}"[:64]
        self._update(draft_id, UpdateExpression=expression, ExpressionAttributeValues=values)
