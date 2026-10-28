#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "fastapi",
#     "rich",
#     "uvicorn",
# ]
# ///
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, final

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from pydantic.networks import UrlConstraints
from rich.console import Console
from rich.logging import RichHandler

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent.resolve()
DB_PATH = (HERE / "data.db").resolve()


class AbsoluteHttpUrl(HttpUrl):
    _constraints = UrlConstraints(max_length=None, allowed_schemes=["http", "https"], host_required=True)

    @property
    def path_qs(self) -> str:
        return (f"{self.path}?{self.query}" if self.query else self.path) or "/"

    @property
    def parts(self) -> list[str]:
        return (self.path or "/").split("/")

    @property
    def domain(self) -> str:
        assert self.host
        return self.host


@contextlib.contextmanager
def setup_logger() -> Generator[None]:
    logger.setLevel(logging.DEBUG)
    now = dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%SZ")
    logs = HERE / "logs"
    logs.mkdir(exist_ok=True)
    with (logs / f"server_{now}.log").open("w", encoding="utf8") as fp:
        logger.addHandler(RichHandler(console=Console(file=fp, width=280)))
        logger.addHandler(RichHandler(show_time=False, show_path=False))
        yield


@contextlib.contextmanager
def connect_db() -> Generator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@final
class Queries:
    @final
    class ForumURLs:
        INSERT = "INSERT OR IGNORE INTO forum_urls (host, id, name, page, url, path_qs) VALUES (?, ?, ?, ?, ?, ?)"
        CREATE = """
            CREATE TABLE IF NOT EXISTS forum_urls (
                host TEXT NOT NULL,
                id NUMBER NOT NULL,
                name TEXT NOT NULL,
                page NUMBER NOT NULL,
                url TEXT NOT NULL,
                date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                path_qs TEXT NOT NULL,
                UNIQUE(host, id, page, url)
            )
        """

    @final
    class OtherURLs:
        INSERT = "INSERT OR IGNORE INTO other_urls (source, url) VALUES (?, ?)"
        CREATE = """
            CREATE TABLE IF NOT EXISTS other_urls (
                source TEXT NOT NULL,
                url TEXT NOT NULL,
                date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, url)
            )
        """


def _init_db() -> None:
    logger.info("Using database: '%s'", DB_PATH)
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute(Queries.ForumURLs.CREATE)
        cursor.execute(Queries.OtherURLs.CREATE)
        conn.commit()


class DatabaseEntry(BaseModel):
    source: AbsoluteHttpUrl
    urls: list[str]


@dataclasses.dataclass(slots=True, frozen=True, order=True)
class XenforoThread:
    THREAD_PARTS: ClassVar[tuple[str, ...]] = "threads", "topic"

    host: str
    id: int
    name: str | None
    path_qs: str
    page: int = 1

    @property
    def slug(self) -> str:
        return f"{self.name}.{self.id}" if self.name else str(self.id)

    @classmethod
    def from_url(cls, url: AbsoluteHttpUrl) -> Self:
        msg = f"Invalid forum thread URL: {url}"
        if not url.host:
            raise ValueError(msg)

        for part in cls.THREAD_PARTS:
            try:
                thread_index = url.parts.index(part)
            except ValueError:
                pass
            else:
                break

        else:
            raise ValueError(msg)

        match url.parts[thread_index + 1 :]:
            case [name, *rest]:
                name, _, thread_id = name.rpartition(".")
                try:
                    thread_id = int(thread_id)
                except ValueError as e:
                    raise ValueError(msg) from e
                match rest:
                    case [part, *_] if "page-" in part:
                        try:
                            page = int(part.replace("page-", "").strip())
                        except ValueError:
                            page = 1
                    case _:
                        page = 1

            case _:
                raise ValueError(msg)

        return cls(
            host=url.host,
            name=name.strip() or None,
            page=page,
            id=thread_id,
            path_qs=url.path_qs,
        )


def save_to_db(entry: DatabaseEntry) -> None:
    try:
        thread = XenforoThread.from_url(entry.source)
    except ValueError:
        logger.warning("Unable to parse thread from %s, saving as unknown URL", entry.source)
        _save_other_urls(entry.source, entry.urls)
    else:
        _save_thread_urls(thread, entry.urls)


def _save_thread_urls(thread: XenforoThread, urls: list[str]) -> None:
    with connect_db() as conn:
        cursor = conn.cursor()
        for url in sorted(urls):
            cursor.execute(
                Queries.ForumURLs.INSERT,
                (
                    thread.host,
                    thread.id,
                    thread.name,
                    thread.page,
                    url,
                    thread.path_qs,
                ),
            )
            _check_result(cursor, thread.host, thread.path_qs, url)

        conn.commit()


def _save_other_urls(origin: AbsoluteHttpUrl, urls: list[str]) -> None:
    with connect_db() as conn:
        cursor = conn.cursor()
        for url in sorted(urls):
            cursor.execute(Queries.OtherURLs.INSERT, (str(origin), url))
            _check_result(cursor, origin.domain, origin.path_qs, url)
        conn.commit()


def _check_result(cursor: sqlite3.Cursor, host: str, slug: str, url: str) -> None:
    if cursor.rowcount:
        level = logging.INFO
        msg = "Added new URL from '%s' (%s): %s"
    else:
        level = logging.DEBUG
        msg = "Skipped duplicate URL from '%s' (%s): %s"

    logger.log(level, msg, host, slug, url)


app = FastAPI()


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
