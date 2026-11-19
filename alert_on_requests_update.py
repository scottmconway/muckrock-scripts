#!/usr/bin/env python3
"""Standalone MuckRock request update monitor"""

import sys

import argparse
import json
import logging
import logging.config
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urljoin, urlsplit

import requests

API_URI = "https://www.muckrock.com/api_v2/"
AUTH_URI = "https://accounts.muckrock.com/api/"
TIMEOUT = 30
STATUS_LABELS = {
    "ack": "Awaiting Acknowledgement",
    "processed": "Awaiting Response",
    "appealing": "Awaiting Appeal",
    "fix": "Fix Required",
    "payment": "Payment Required",
    "lawsuit": "In Litigation",
    "rejected": "Rejected",
    "no_docs": "No Responsive Documents",
    "done": "Completed",
    "partial": "Partially Completed",
    "consolidated": "Consolidated",
    "unknown": "Unknown",
}


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        config = json.load(file)
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    auth = config.get("auth_info")
    if not isinstance(auth, dict):
        raise ValueError("auth_info must be an object")
    for key in ("username", "password"):
        if not isinstance(auth.get(key), str) or not auth[key]:
            raise ValueError(f"auth_info.{key} must be a non-empty string")
    return config


def describe_api_error(error: Exception) -> str:
    details = [type(error).__name__]
    response = getattr(error, "response", None)
    request = getattr(error, "request", None)
    if response is not None:
        details.append(f"HTTP {response.status_code}")
        request = response.request
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            details.append("HTML response")
        if response.headers.get("cf-mitigated", "").lower() == "challenge":
            details.append("Cloudflare browser challenge blocks API access")
        retry_after = response.headers.get("Retry-After", "")
        if 0 < len(retry_after) <= 9 and retry_after.isascii() and retry_after.isdecimal():
            details.append(f"Retry-After {retry_after} seconds")
    if request is not None and request.url:
        url = urlsplit(request.url)
        details.append(f"{request.method} {url.scheme}://{url.hostname}{url.path}")
    return "; ".join(details)


def parse_timestamp(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Expected a timestamp with a timezone")
    return value.astimezone(timezone.utc)


def load_status(path: Path, user_id: int) -> tuple[datetime | None, dict, dict]:
    try:
        with path.open(encoding="utf-8") as file:
            status = json.load(file)
    except FileNotFoundError:
        return None, {}, {}
    if not isinstance(status, dict) or status.get("user_id") != user_id:
        raise ValueError("Status file is invalid or belongs to a different user")
    if not isinstance(status.get("requests"), dict):
        raise ValueError("Status file must contain request timestamps")
    checked_at = parse_timestamp(status.get("checked_at"))
    timestamps = {}
    for request_id, updated_at in status["requests"].items():
        if not request_id.isdecimal():
            raise ValueError("Status file contains an invalid request ID")
        timestamps[request_id] = parse_timestamp(updated_at)
    statuses = status.get("statuses", {})
    if not isinstance(statuses, dict):
        raise ValueError("Status file must contain valid previous statuses")
    for request_id, previous_status in statuses.items():
        if not request_id.isdecimal() or not isinstance(previous_status, str):
            raise ValueError("Status file contains an invalid previous status")
    return checked_at, timestamps, statuses


def save_status(path: Path, status: dict) -> None:
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(status, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def check_request_updates(
    user_id: int,
    fetch_requests,
    status_path: Path,
    now: datetime | None = None,
) -> int:
    logger = logging.getLogger("muckrock_updates")
    started_at = parse_timestamp(now if now is not None else datetime.now(timezone.utc))
    checked_at, timestamps, statuses = load_status(status_path, user_id)
    cutoff = checked_at if checked_at is not None else started_at - timedelta(days=7)
    updates = []

    # Finish fetching every page before logging or saving.
    for request in fetch_requests():
        request_id = str(request.id)
        updated_at = parse_timestamp(request.datetime_updated)
        previous = timestamps.get(request_id)
        if (previous is not None and updated_at > previous) or (
            previous is None and updated_at >= cutoff
        ):
            updates.append((
                request.title, request.url, statuses.get(request_id, "unknown"),
                request.status,
            ))
        if previous is None or updated_at >= previous:
            statuses[request_id] = request.status
        timestamps[request_id] = max(previous, updated_at) if previous else updated_at

    for title, url, old_status, new_status in updates:
        title = " ".join(title.splitlines()).translate(
            str.maketrans({character: f"\\{character}" for character in r"\[]*_`<>"})
        )
        url = quote(url, safe=":/?#[]@!$&'*+,;=%~")
        old_label = STATUS_LABELS.get(old_status, f"Unknown ({old_status})")
        new_label = STATUS_LABELS.get(new_status, f"Unknown ({new_status})")
        logger.info(f"[{title}]({url}) - {old_label} -> {new_label}")

    # Save after logging so errors can be retried, at the cost of possible repeats.
    save_status(
        status_path,
        {
            "user_id": user_id,
            "checked_at": started_at.isoformat(),
            "requests": {
                request_id: updated_at.isoformat()
                for request_id, updated_at in timestamps.items()
            },
            "statuses": statuses,
        },
    )
    return len(updates)


def response_json(response):
    if 300 <= response.status_code < 400:
        raise requests.HTTPError("Unexpected API redirect", response=response)
    response.raise_for_status()
    return response.json()


def authenticate(session, auth: dict) -> None:
    data = response_json(
        session.post(
            f"{AUTH_URI}token/",
            json={"username": auth["username"], "password": auth["password"]},
            timeout=TIMEOUT,
            allow_redirects=False,
        )
    )
    if not isinstance(data, dict) or not isinstance(data.get("access"), str):
        raise ValueError("Authentication response has no access token")
    if not data["access"]:
        raise ValueError("Authentication response has an empty access token")
    session.headers["Authorization"] = f"Bearer {data['access']}"


def iter_user_requests(session, user_id: int):
    url = f"{API_URI}requests/"
    params = {"user": user_id}
    origin = urlsplit(API_URI)
    visited = set()
    while url is not None:
        target = urlsplit(url)
        if (target.scheme, target.netloc) != (
            origin.scheme,
            origin.netloc,
        ) or not target.path.startswith(origin.path):
            raise ValueError("Pagination points outside the MuckRock API")
        if url in visited:
            raise ValueError("Pagination contains a loop")
        visited.add(url)
        data = response_json(
            session.get(
                url,
                params=params,
                timeout=TIMEOUT,
                allow_redirects=False,
            )
        )
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("Invalid request list response")
        for item in data["results"]:
            if not isinstance(item, dict):
                raise ValueError("Invalid request record")
            if type(item.get("id")) is not int or item["id"] < 1:
                raise ValueError("Invalid request ID")
            if not all(isinstance(item.get(key), str) for key in ("title", "status")):
                raise ValueError("Invalid request title or status")
            yield SimpleNamespace(
                id=item["id"],
                title=item["title"],
                status=item["status"],
                datetime_updated=item["datetime_updated"],
                url=urljoin(
                    API_URI,
                    item.get("absolute_url") or f"/foi/request/{item['id']}/",
                ),
            )
        next_url = data["next"]
        if next_url is not None and (not isinstance(next_url, str) or not next_url):
            raise ValueError("Invalid pagination URL")
        url = urljoin(url, next_url) if next_url is not None else None
        params = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--status", type=Path, default=Path("status.json"))
    args = parser.parse_args(argv)
    if args.config.resolve() == args.status.resolve():
        parser.error("--status must not point to the credentials config")

    try:
        config = load_config(args.config)
        logging.config.dictConfig(config["logging"])
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        ImportError,
    ) as error:
        print(
            f"Configuration failed ({type(error).__name__}). Expected auth_info "
            "credentials and a valid logging dictConfig under the 'logging' key.",
            file=sys.stderr,
        )
        return 1
    logger = logging.getLogger("muckrock_updates")
    if not logger.isEnabledFor(logging.INFO) or not logger.hasHandlers():
        print(
            "Configure the 'muckrock_updates' logger or root logger with an "
            "INFO level and a handler in config.json.",
            file=sys.stderr,
        )
        return 1

    started_at = datetime.now(timezone.utc)
    stage = "authenticating"
    try:
        with requests.Session() as session:
            session.headers["Accept"] = "application/json"
            authenticate(session, config["auth_info"])
            stage = "identifying the logged-in user"
            user = response_json(
                session.get(
                    f"{API_URI}users/me/",
                    timeout=TIMEOUT,
                    allow_redirects=False,
                )
            )
            if (
                not isinstance(user, dict)
                or type(user.get("id")) is not int
                or user["id"] < 1
            ):
                raise ValueError("Invalid user response")
            stage = "checking requests"
            check_request_updates(
                user["id"],
                lambda: iter_user_requests(session, user["id"]),
                args.status,
                started_at,
            )
    except (
        requests.RequestException,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        logger.error(
            f"Update check failed while {stage} "
            f"({describe_api_error(error)}); status was not advanced."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
