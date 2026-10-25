"""Fixed-window rate limiter backed by DynamoDB.

Each (bucket, identity, window) gets one counter item. A single atomic
UpdateItem ADD increments it, so concurrent Lambdas never double count.
Items carry an `expiresAt` TTL attribute so DynamoDB cleans them up for free.
"""

import time
from dataclasses import dataclass

# bucket name -> (max requests, window length in seconds)
LIMITS = {
    "read": (120, 60),        # signed-in reads per account
    "write": (30, 60),        # signed-in creates/updates/deletes per account
    "comment": (10, 60),      # new comments per account
    "anonymous": (60, 60),    # public reads per IP address
}


@dataclass
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


def check(table, bucket, identity, now=None):
    limit, window = LIMITS[bucket]
    now = int(now if now is not None else time.time())
    window_start = now - (now % window)
    result = table.update_item(
        Key={"pk": f"RATE#{bucket}#{identity}", "sk": f"W#{window_start}"},
        UpdateExpression="ADD #count :one SET expiresAt = if_not_exists(expiresAt, :exp)",
        ExpressionAttributeNames={"#count": "count"},
        ExpressionAttributeValues={":one": 1, ":exp": window_start + window * 2},
        ReturnValues="UPDATED_NEW",
    )
    count = int(result["Attributes"]["count"])
    return Decision(
        allowed=count <= limit,
        limit=limit,
        remaining=max(limit - count, 0),
        retry_after=window_start + window - now,
    )
