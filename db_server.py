#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "fastapi",
#     "rich",
#     "uvicorn",
#     "yarl",
# ]
# ///
from __future__ import annotations

import contextlib
import dataclasses
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from rich.console import Console
from rich.logging import RichHandler
from yarl import URL

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

DB_PATH = (Path(__file__).parent / "data.db").resolve()


app = FastAPI()


@contextlib.contextmanager
def setup_logger() -> Generator[None]:
    logger.setLevel(logging.DEBUG)
    with (Path(__file__).parent / "server.log").open("w", encoding="utf8") as fp:
        logger.addHandler(RichHandler(console=Console(file=fp, width=280)))
        logger.addHandler(RichHandler(show_time=False))
        yield


@contextlib.contextmanager
def connect_db() -> Generator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _init_db() -> None:
    logger.info("Using database: '%s'", DB_PATH)
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS forum_threads (
                host TEXT,
                name TEXT,
                id NUMBER,
                page NUMBER,
                post NUMBER,
                url TEXT,
                date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                path_qs TEXT,
                UNIQUE(host, name, page, url)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS other_urls (
                origin TEXT,
                url TEXT,
                date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(origin, url)
            )
        """)
        conn.commit()


class DatabaseEntry(BaseModel):
    source: HttpUrl
    urls: list[str]


@dataclasses.dataclass(slots=True, frozen=True, order=True)
class XenforoThread:
    THREAD_PARTS: ClassVar[tuple[str, ...]] = "threads", "topic"

    host: str
    name: str
    id: int
    page: int = dataclasses.field(default=1)
    path: str = "/"
    post: int | None = dataclasses.field(default=None, compare=False, hash=False)
    path_qs: str = dataclasses.field(default="", compare=False, hash=False)

    @classmethod
    def from_url(cls, origin: HttpUrl) -> Self:
        url = str(origin)
        url = URL(url, encoded="%" in url)
        msg = f"Invalid forum thread URL: {url}"
        if not url.host or not any(part in url.parts for part in cls.THREAD_PARTS):
            raise ValueError(msg)

        found_part = next(part for part in cls.THREAD_PARTS if part in url.parts)
        name_index = url.parts.index(found_part) + 1
        name = url.parts[name_index]
        if "." not in name:
            raise ValueError(msg)

        post_number = None
        if post_string := next((sec for sec in {url.fragment, *url.parts} if "post-" in sec), None):
            post_number = int(post_string.replace("post-", "").strip())

        page = 1
        if len(url.parts) > name_index + 1 and "page-" in url.parts[name_index + 1]:
            page = int(url.parts[name_index + 1].replace("page-", "").strip())

        name, thread_id = name.rsplit(".")

        return cls(
            host=url.host,
            name=name.strip(),
            post=post_number,
            page=page,
            id=int(thread_id.strip()),
            path="/" + "/".join(url.parts[1 : name_index + 1]),
            path_qs=url.path_qs,
        )


def save_to_db(entry: DatabaseEntry) -> None:
    try:
        thread = XenforoThread.from_url(entry.source)
    except ValueError:
        _save_other_urls(entry.source, entry.urls)
    else:
        _save_thread_urls(thread, entry.urls)


def _save_thread_urls(thread: XenforoThread, urls: list[str]) -> None:
    query = "INSERT OR IGNORE INTO forum_threads (host, name, id, page, post, path_qs, url, date) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)"
    with connect_db() as conn:
        cursor = conn.cursor()
        for url in sorted(urls):
            cursor.execute(
                query,
                (
                    thread.host,
                    thread.name,
                    thread.id,
                    thread.page,
                    thread.post,
                    thread.path_qs,
                    url,
                ),
            )

        conn.commit()


def _save_other_urls(origin: HttpUrl, urls: list[str]) -> None:
    query = "INSERT OR IGNORE INTO other_urls (origin, url, date) VALUES (?, ?, CURRENT_TIMESTAMP)"
    with connect_db() as conn:
        cursor = conn.cursor()
        for url in sorted(urls):
            cursor.execute(query, (str(origin), url))
        conn.commit()


@app.post("/submit")
async def submit(entry: DatabaseEntry) -> dict[str, str]:
    logger.info("Received %s URLs from %s", len(entry.urls), entry.source)
    try:
        save_to_db(entry)
        return {"status:": "OK"}
    except Exception as e:
        logger.exception(f"{e}")
        details = f"status: ERROR - message: {e}"
        raise HTTPException(status_code=500, detail=details) from None


def main() -> None:
    with setup_logger():
        _init_db()
        uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
