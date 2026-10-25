#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "genanki>=0.13.1",
#     "pillow>=10.0",
#     "requests>=2.31",
# ]
# ///
"""Build an Anki package from the flashcards in `flashcards.json`.

Every card gets two pieces of media that are not in the JSON:

* a picture, searched for with `--source` (Openverse, or Pixabay or Unsplash
  with an access key), using the card's `visual_link` as the query, and
* an mp3 of the Brazilian Portuguese sentence, from Google Translate's TTS
  endpoint.

Both are attached to the side of the card showing the Portuguese sentence, and
both cards of a pair (pt→en and en→pt) share the same files.

New cards are queued with the most frequent words first.

Openverse, the default source, needs no credentials, but anonymous clients only
get about 200 requests a day, so a deck with many new pictures will outrun it.

`--source pixabay` allows 100 requests a minute with no daily cap, which makes
it the one to use for a big batch; a key is free and instant from
<https://pixabay.com/api/docs/>. `--source unsplash` has the better pictures,
but a demo key is good for only about 25 new pictures an hour.

Both read their key from `--access-key`, from `PIXABAY_API_KEY` or
`UNSPLASH_ACCESS_KEY` in the environment, or from a `.pixabay-key` or
`.unsplash-key` file next to this script.

Usage:

    uv run create_apkg.py --input flashcards.json --output flashcards.apkg

Downloaded media is cached in `--cache-dir`, so re-runs only fetch what is new.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import genanki
import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UNSPLASH_SEARCH = "https://api.unsplash.com/search/photos"
OPENVERSE_SEARCH = "https://api.openverse.org/v1/images/"
PIXABAY_SEARCH = "https://pixabay.com/api/"
GOOGLE_TTS = "https://translate.google.com/translate_tts"
# Unsplash allows 50 requests/hour for a demo app, 5000 once it is approved for
# production. Searching and reporting a download are one request each, so a
# demo key is good for about 25 new pictures per hour. Cached ones are free.
UNSPLASH_PER_MINUTE = 20
# Anonymous Openverse clients get 20 requests/minute, but only about 200 a day,
# which is the limit that actually bites on a big deck.
OPENVERSE_PER_MINUTE = 15
# Pixabay allows 100 requests/60s per key with no daily cap, which makes it the
# source to use when there are a lot of new pictures to fetch; stay under it.
PIXABAY_PER_MINUTE = 60
# Unsplash asks that applications identify themselves in the UTM parameters of
# the attribution links.
UTM = "?utm_source=flashcards-pt-br&utm_medium=referral"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36"
)

# Fixed ids so that re-importing an updated package updates the existing deck
# and notes instead of creating duplicates.
DECK_ID = 1_691_307_441
MODEL_ID_PT_EN = 1_691_307_442
MODEL_ID_EN_PT = 1_691_307_443

CSS = """
.card {
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 22px;
  text-align: center;
  color: #1a1a1a;
  background-color: #fdfdfd;
}
.sentence { line-height: 1.5; }
.sentence b { color: #1565c0; font-weight: 700; }
.meaning { font-size: 26px; font-weight: 600; }
.pic { margin: 16px 0 4px; }
.pic img { max-width: 512px; border-radius: 6px; }
.credit { font-size: 11px; color: #9a9a9a; line-height: 1.3; }
.credit a { color: inherit; text-decoration: none; }
.nightMode.card { color: #e8e8e8; background-color: #2f2f31; }
.nightMode .sentence b { color: #64b5f6; }
.nightMode .credit { color: #7d7d7d; }
"""

MEDIA_TEMPLATE = """
{{#Image}}<div class="pic">{{Image}}</div>{{/Image}}
{{#Credit}}<div class="credit">{{Credit}}</div>{{/Credit}}
{{Audio}}
"""

FIELDS = [{"name": n} for n in ("Word", "Front", "Back", "Image", "Credit", "Audio")]

# Portuguese on the front: picture and audio go on the front.
MODEL_PT_EN = genanki.Model(
    MODEL_ID_PT_EN,
    "Brazilian Portuguese → English",
    fields=FIELDS,
    templates=[
        {
            "name": "pt → en",
            "qfmt": '<div class="sentence">{{Front}}</div>' + MEDIA_TEMPLATE,
            "afmt": '{{FrontSide}}<hr id=answer><div class="meaning">{{Back}}</div>',
        }
    ],
    css=CSS,
)

# Portuguese on the back: picture and audio go on the back.
MODEL_EN_PT = genanki.Model(
    MODEL_ID_EN_PT,
    "English → Brazilian Portuguese",
    fields=FIELDS,
    templates=[
        {
            "name": "en → pt",
            "qfmt": '<div class="sentence">{{Front}}</div>',
            "afmt": '{{FrontSide}}<hr id=answer><div class="sentence">{{Back}}</div>'
            + MEDIA_TEMPLATE,
        }
    ],
    css=CSS,
)

class Throttled(Exception):
    """An API asked us to slow down, and for how long."""

    def __init__(self, retry_after: float, detail: str) -> None:
        super().__init__(detail)
        self.retry_after = max(1.0, retry_after)
        self.detail = detail


class RateLimiter:
    """Spreads calls out so that at most `per_minute` happen in any minute.

    Can also be paused, which holds back every thread at once: when one of them
    is throttled the others would only be throttled too.
    """

    def __init__(self, per_minute: int) -> None:
        self._interval = 60.0 / per_minute
        self._lock = threading.Lock()
        self._next = 0.0
        self._paused_until = 0.0

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                pause = self._paused_until - now
                if pause <= 0:
                    delay = max(0.0, self._next - now)
                    self._next = max(now, self._next) + self._interval
                    break
            # Re-check periodically rather than sleeping the whole pause, in
            # case another thread extends it.
            time.sleep(min(pause, 5.0))
        if delay:
            time.sleep(delay)

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._paused_until = max(self._paused_until, time.monotonic() + seconds)


def retry_after_seconds(response: requests.Response, default: float) -> float:
    """The Retry-After header, in seconds; `default` if it is missing or odd."""
    header = response.headers.get("Retry-After")
    if not header:
        return default
    try:
        return float(header)
    except ValueError:
        pass
    try:  # The header may be an HTTP date instead of a number of seconds.
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(1.0, (when - datetime.now(timezone.utc)).total_seconds())


def seconds_to_next_hour() -> float:
    """Until the next clock hour, which is when Unsplash's quota resets.

    Unsplash reports no reset time, so this is the best guess available; the
    margin keeps us from retrying a second too early.
    """
    return 3600.0 - (time.time() % 3600.0) + 5.0


def human(seconds: float) -> str:
    minutes, secs = divmod(int(seconds + 0.5), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def waiting_out_throttles(
    action: Callable[[], object],
    limiter: RateLimiter,
    max_wait: float,
    attempts: int = 4,
) -> object:
    """Run `action`, waiting and retrying while an API throttles us."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Throttled as throttled:
            if attempt == attempts:
                raise RuntimeError(
                    f"{throttled.detail}; still throttled after {attempts} tries"
                ) from None
            if throttled.retry_after > max_wait:
                raise RuntimeError(
                    f"{throttled.detail}; it wants {human(throttled.retry_after)}, "
                    f"more than the --max-wait of {human(max_wait)}"
                ) from None
            print(
                f"  … {throttled.detail} — waiting {human(throttled.retry_after)}",
                flush=True,
            )
            # Hold every worker back, not just this one.
            limiter.pause(throttled.retry_after)
            time.sleep(throttled.retry_after)
    raise AssertionError("unreachable")


@dataclass
class Pair:
    """The media shared by the pt→en and en→pt cards of one sentence."""

    word: str
    portuguese: str
    # The hand-written description of the picture the card should show, from
    # the `visual_link` field; both cards of a pair carry the same one.
    visual_link: str = ""
    image: Path | None = None
    credit: str = ""
    audio: Path | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def audio_key(self) -> str:
        return digest(self.portuguese)

    @property
    def image_key(self) -> str:
        # The query is part of the key so that editing a `visual_link` fetches
        # a new picture instead of reusing the one the old wording found.
        return digest(f"{self.portuguese}\n{self.visual_link}")


def digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        # 429 is handled in this script instead, which can wait as long as the
        # API asks for rather than backing off blindly.
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = USER_AGENT
    return session


def plain(sentence: str) -> str:
    """The sentence without the asterisks marking the word being learned."""
    return sentence.replace("*", "")


def to_html(sentence: str) -> str:
    """Escape the sentence and turn *word* into bold."""
    return re.sub(r"\*([^*]+)\*", r"<b>\1</b>", html.escape(sentence))


def load_pairs(cards: list[dict]) -> tuple[list[Pair], dict[str, Pair]]:
    """Group the cards into pairs keyed by their Portuguese sentence.

    A card is an en→pt card when its back is the front of another card for the
    same word; that back is the Portuguese sentence and its own front is the
    English one. Everything else is a pt→en card.
    """
    fronts_by_word: dict[str, set[str]] = {}
    for card in cards:
        fronts_by_word.setdefault(card["word"], set()).add(card["front"])

    pairs: dict[str, Pair] = {}
    by_card: dict[str, Pair] = {}

    def pair_for(word: str, portuguese: str) -> Pair:
        pair = pairs.get(portuguese)
        if pair is None:
            pair = pairs[portuguese] = Pair(word, portuguese)
        return pair

    for card in cards:
        # The Portuguese sentence is the back of an en→pt card and the front of
        # a pt→en one; it is what the two cards of a pair share.
        portuguese = card["back"] if is_en_pt(card, fronts_by_word) else card["front"]
        pair = pair_for(card["word"], portuguese)
        # Both cards of a pair carry the same one, so either may supply it.
        pair.visual_link = pair.visual_link or card["visual_link"].strip()
        by_card[card_id(card)] = pair

    return list(pairs.values()), by_card


def is_en_pt(card: dict, fronts_by_word: dict[str, set[str]]) -> bool:
    return card["back"] in fronts_by_word.get(card["word"], set())


def card_id(card: dict) -> str:
    return f"{card['word']}\x00{card['front']}"


@dataclass
class Photo:
    """One search hit, reduced to what building a card needs."""

    urls: list[str]  # where to download it, best first
    credit: str  # attribution, as HTML
    report: Callable[[], None] | None = None  # called once it has been used


def search_image(
    session: requests.Session,
    limiter: RateLimiter,
    source: Source,
    key: str,
    query: str,
    max_size: int,
    max_wait: float,
) -> Photo | None:
    """The picture the query finds, if it finds one."""

    def once() -> Photo | None:
        limiter.wait()
        return source.search(session, key, query, max_size)

    return waiting_out_throttles(once, limiter, max_wait)  # type: ignore[return-value]


def search_unsplash(
    session: requests.Session, key: str, query: str, max_size: int
) -> Photo | None:
    response = session.get(
        UNSPLASH_SEARCH,
        params={"query": query, "per_page": 5, "content_filter": "high"},
        headers=auth(key),
        timeout=30,
    )
    if response.status_code == 401:
        raise RuntimeError("Unsplash rejected the access key")
    if response.status_code == 403:
        # Unsplash uses 401 for a bad key, so a 403 is the hourly quota.
        raise Throttled(
            retry_after_seconds(response, seconds_to_next_hour()),
            "Unsplash hourly quota used up",
        )
    if response.status_code == 429:
        raise Throttled(retry_after_seconds(response, 60.0), "Unsplash is throttling")
    response.raise_for_status()
    remaining = response.headers.get("X-Ratelimit-Remaining")
    if remaining is not None:
        QUOTA.update("Unsplash", remaining)
    results = response.json().get("results") or []
    if not results:
        return None
    photo = results[0]
    return Photo(
        urls=unsplash_urls(photo, max_size),
        credit=unsplash_credit(photo),
        report=lambda: report_download(session, key, photo),
    )


def search_openverse(
    session: requests.Session, key: str, query: str, max_size: int
) -> Photo | None:
    response = session.get(
        OPENVERSE_SEARCH,
        params={"q": query, "page_size": 5, "mature": "false"},
        timeout=30,
    )
    if response.status_code == 429:
        # Openverse limits anonymous clients per minute and per day, and says
        # how long to hold off in Retry-After.
        raise Throttled(
            retry_after_seconds(response, 60.0), "Openverse is throttling"
        )
    response.raise_for_status()
    results = response.json().get("results") or []
    if not results:
        return None
    result = results[0]
    # The thumbnail is already downscaled; fall back to the original file.
    urls = [u for u in (result.get("thumbnail"), result.get("url")) if u]
    return Photo(urls=urls, credit=openverse_credit(result))


def search_pixabay(
    session: requests.Session, key: str, query: str, max_size: int
) -> Photo | None:
    response = session.get(
        PIXABAY_SEARCH,
        params={
            "key": key,
            "q": query,
            "image_type": "photo",
            "safesearch": "true",
            "lang": "en",
            "per_page": 5,  # Pixabay rejects anything below 3.
        },
        timeout=30,
    )
    if response.status_code == 400:
        # Pixabay reports a bad key as a 400 rather than a 401.
        raise RuntimeError("Pixabay rejected the API key")
    if response.status_code == 429:
        # X-RateLimit-Reset is the seconds left in the current window.
        reset = response.headers.get("X-RateLimit-Reset")
        default = float(reset) if reset and reset.isdigit() else 60.0
        raise Throttled(retry_after_seconds(response, default), "Pixabay is throttling")
    response.raise_for_status()
    remaining = response.headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        QUOTA.update("Pixabay", remaining)
    hits = response.json().get("hits") or []
    if not hits:
        return None
    hit = hits[0]
    # webformatURL is capped at 640px, which is already close to the card size;
    # fall back to the large version if it cannot be downloaded.
    urls = [u for u in (hit.get("webformatURL"), hit.get("largeImageURL")) if u]
    return Photo(urls=urls, credit=pixabay_credit(hit))


class Quota:
    """The last rate limit figure an image source reported back."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.remaining: int | None = None
        self.source = ""

    def update(self, source: str, value: str) -> None:
        with self._lock:
            try:
                self.remaining = int(value)
            except ValueError:
                return
            self.source = source


QUOTA = Quota()


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Client-ID {key}", "Accept-Version": "v1"}


def report_download(session: requests.Session, key: str, photo: dict) -> None:
    """Tell Unsplash the photo was used, as their API guidelines require."""
    location = (photo.get("links") or {}).get("download_location")
    if not location:
        return
    try:
        session.get(location, headers=auth(key), timeout=30)
    except requests.RequestException:
        # Not worth failing the build over; the picture is already downloaded.
        pass


def unsplash_credit(photo: dict) -> str:
    """The attribution HTML Unsplash requires: photographer and Unsplash, linked."""
    user = photo.get("user") or {}
    name = html.escape((user.get("name") or "Unknown").strip())
    profile = (user.get("links") or {}).get("html") or "https://unsplash.com"
    return (
        f'Photo by <a href="{html.escape(profile)}{UTM}">{name}</a> '
        f'on <a href="https://unsplash.com{UTM}">Unsplash</a>'
    )


def openverse_credit(result: dict) -> str:
    """Attribution for a CC-licensed picture: title, creator and licence."""
    title = html.escape((result.get("title") or "Untitled").strip())
    creator = html.escape((result.get("creator") or "unknown").strip())
    licence = (result.get("license") or "").upper()
    version = result.get("license_version") or ""
    licence = html.escape(" ".join(part for part in (licence, version) if part))
    landing = result.get("foreign_landing_url") or ""
    credit = f"“{title}” by {creator}"
    if licence:
        credit += f" ({licence})"
    if landing:
        credit = f'<a href="{html.escape(landing)}">{credit}</a>'
    return credit


def pixabay_credit(hit: dict) -> str:
    """Attribution for a Pixabay picture.

    The Pixabay licence does not require it, but they ask for it and the card
    has a place for it anyway.
    """
    user = html.escape((hit.get("user") or "unknown").strip())
    page = hit.get("pageURL") or "https://pixabay.com"
    return (
        f"Image by {user} on "
        f'<a href="{html.escape(page)}">Pixabay</a>'
    )


def unsplash_urls(photo: dict, max_size: int) -> list[str]:
    """Picture URLs to try, already sized down by Unsplash where possible."""
    urls = photo.get("urls") or {}
    candidates = []
    if raw := urls.get("raw"):
        # Unsplash resizes on the fly, so ask for the size the card needs.
        candidates.append(f"{raw}&w={max_size}&h={max_size}&fit=clip&q=85&fm=jpg")
    candidates += [u for u in (urls.get("small"), urls.get("regular")) if u]
    return candidates


@dataclass(frozen=True)
class Source:
    """An image provider: how to search it and how hard it may be hit."""

    name: str
    search: Callable[[requests.Session, str, str, int], Photo | None]
    per_minute: int
    # Where the key comes from when the source needs one, besides --access-key.
    key_env: str = ""
    key_file: str = ""

    @property
    def needs_key(self) -> bool:
        return bool(self.key_env)


SOURCES = {
    source.name: source
    for source in (
        Source("openverse", search_openverse, OPENVERSE_PER_MINUTE),
        Source(
            "pixabay",
            search_pixabay,
            PIXABAY_PER_MINUTE,
            key_env="PIXABAY_API_KEY",
            key_file=".pixabay-key",
        ),
        Source(
            "unsplash",
            search_unsplash,
            UNSPLASH_PER_MINUTE,
            key_env="UNSPLASH_ACCESS_KEY",
            key_file=".unsplash-key",
        ),
    )
}


def fetch_image(
    session: requests.Session,
    limiter: RateLimiter,
    source: Source,
    key: str,
    pair: Pair,
    cache_dir: Path,
    max_size: int,
    max_wait: float,
) -> None:
    # The source is part of the file name so that switching sources fetches a
    # new picture instead of reusing the one the other source found.
    path = cache_dir / f"{source.name}-{pair.image_key}.jpg"
    credit_path = path.with_suffix(".credit.txt")
    if path.exists() and credit_path.exists():
        pair.image, pair.credit = path, credit_path.read_text("utf-8")
        return

    photo = search_image(
        session, limiter, source, key, pair.visual_link, max_size, max_wait
    )
    if photo is None:
        pair.problems.append("no image found")
        return

    last: Exception | None = None
    for url in photo.urls:
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            data = shrink(response.content, max_size)
        except (requests.RequestException, OSError) as exc:
            last = exc
            continue
        path.write_bytes(data)
        credit_path.write_text(photo.credit, "utf-8")
        pair.image, pair.credit = path, photo.credit
        if photo.report:
            photo.report()
        return
    pair.problems.append(f"image download failed ({last if photo.urls else 'no url'})")


def shrink(data: bytes, max_size: int) -> bytes:
    """Scale the image down to fit in max_size × max_size, keeping the ratio."""
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            flattened = Image.new("RGB", image.size, (255, 255, 255))
            flattened.paste(image, mask=image.split()[-1])
            image = flattened
        else:
            image = image.convert("RGB")
        image.thumbnail((max_size, max_size), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85, optimize=True)
        return buffer.getvalue()


def fetch_audio(
    session: requests.Session,
    limiter: RateLimiter,
    pair: Pair,
    cache_dir: Path,
    max_wait: float,
) -> None:
    path = cache_dir / f"ptbr-{pair.audio_key}.mp3"
    if path.exists():
        pair.audio = path
        return

    sentence = plain(pair.portuguese)
    url = f"{GOOGLE_TTS}?ie=UTF-8&tl=pt&client=tw-ob&q={urllib.parse.quote(sentence)}"

    def once() -> requests.Response:
        reply = session.get(url, timeout=60)
        if reply.status_code == 429:
            raise Throttled(
                retry_after_seconds(reply, 60.0), "Google Translate is throttling"
            )
        reply.raise_for_status()
        return reply

    try:
        response = waiting_out_throttles(once, limiter, max_wait)
    except requests.RequestException as exc:
        pair.problems.append(f"audio download failed ({exc})")
        return
    if "audio" not in response.headers.get("Content-Type", ""):
        pair.problems.append("audio download failed (not an mp3)")
        return
    path.write_bytes(response.content)
    pair.audio = path


def fetch_image_step(
    session: requests.Session, limiter: RateLimiter, pair: Pair, args: argparse.Namespace
) -> None:
    fetch_image(
        session,
        limiter,
        SOURCES[args.source],
        args.access_key or "",
        pair,
        args.cache_dir,
        args.image_size,
        args.max_wait,
    )


def fetch_audio_step(
    session: requests.Session, limiter: RateLimiter, pair: Pair, args: argparse.Namespace
) -> None:
    fetch_audio(session, limiter, pair, args.cache_dir, args.max_wait)


def collect_media(pairs: list[Pair], args: argparse.Namespace) -> None:
    session = make_session()
    limiter = RateLimiter(SOURCES[args.source].per_minute)
    done = threading.Lock()
    counter = 0

    def work(pair: Pair) -> None:
        nonlocal counter
        # A card without a picture or without audio is still worth having, so
        # nothing in here is allowed to abort the build.
        for step, fetch in (("image", fetch_image_step), ("audio", fetch_audio_step)):
            try:
                fetch(session, limiter, pair, args)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                pair.problems.append(f"{step} failed ({exc})")
        with done:
            counter += 1
            status = "; ".join(pair.problems) if pair.problems else "ok"
            print(
                f"  [{counter}/{len(pairs)}] {pair.word} → "
                f"“{pair.visual_link}”: {status}",
                flush=True,
            )

    print(f"Fetching media for {len(pairs)} sentences from {args.source}…")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, pairs))


def anki_tags(card: dict, direction: str) -> list[str]:
    tags = [f"direction::{direction}", f"frequency::{card['frequency']}"]
    for key, value in (card.get("tags") or {}).items():
        text = value if isinstance(value, str) else str(value).lower()
        # Anki tags cannot contain whitespace.
        tags.append("{}::{}".format(key, re.sub(r"\s+", "-", text.strip())))
    return tags


def build_deck(
    cards: list[dict], by_card: dict[str, Pair], deck_name: str
) -> tuple[genanki.Deck, list[str]]:
    deck = genanki.Deck(DECK_ID, deck_name)
    fronts_by_word: dict[str, set[str]] = {}
    for card in cards:
        fronts_by_word.setdefault(card["word"], set()).add(card["front"])

    media: dict[str, str] = {}
    # The most frequent words first, so Anki introduces them first; cards of
    # the same frequency keep the order they have in the JSON, which keeps the
    # two cards of a sentence together.
    ordered = sorted(cards, key=lambda card: -card["frequency"])
    for position, card in enumerate(ordered, start=1):
        pair = by_card[card_id(card)]
        en_pt = is_en_pt(card, fronts_by_word)
        image = audio = ""
        if pair.image:
            media[pair.image.name] = str(pair.image)
            image = f'<img src="{pair.image.name}">'
        if pair.audio:
            media[pair.audio.name] = str(pair.audio)
            audio = f"[sound:{pair.audio.name}]"
        deck.add_note(
            genanki.Note(
                model=MODEL_EN_PT if en_pt else MODEL_PT_EN,
                fields=[
                    card["word"],
                    to_html(card["front"]),
                    to_html(card["back"]),
                    image,
                    # Already HTML, with the links Unsplash asks for.
                    pair.credit if image else "",
                    audio,
                ],
                tags=anki_tags(card, "en-pt" if en_pt else "pt-en"),
                guid=genanki.guid_for(card["word"], card["front"]),
                due=position,
            )
        )
    return deck, sorted(media.values())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create an Anki package from a flashcards.json file."
    )
    parser.add_argument(
        "--input", type=Path, required=True, help="input JSON file with the flashcards"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Anki package (.apkg) to write"
    )
    parser.add_argument(
        "--deck-name", default="Brazilian Portuguese", help="name of the Anki deck"
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".media-cache"),
        help="where downloaded pictures and audio are kept between runs",
    )
    parser.add_argument(
        "--image-size",
        type=int,
        default=512,
        help="pictures larger than this many pixels are scaled down",
    )
    parser.add_argument(
        "--workers", type=int, default=4, help="parallel downloads"
    )
    parser.add_argument(
        "--source",
        choices=sorted(SOURCES),
        default="openverse",
        help="where the pictures come from (default: openverse, which needs "
        "no credentials but is limited to about 200 pictures a day; pixabay "
        "needs a free key and allows 100 requests a minute; unsplash has the "
        "better pictures but a demo key only allows about 25 an hour)",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=3600.0,
        metavar="SECONDS",
        help="how long to wait out an API throttle before giving up on a "
        "picture (default: 3600, enough for Unsplash's hourly quota to reset; "
        "0 never waits)",
    )
    parser.add_argument(
        "--access-key",
        default=None,
        help="API key for the sources that need one; defaults to "
        "$PIXABAY_API_KEY or a .pixabay-key file for --source pixabay, and to "
        "$UNSPLASH_ACCESS_KEY or a .unsplash-key file for --source unsplash",
    )
    return parser.parse_args(argv)


def resolve_access_key(given: str | None, source: Source) -> str | None:
    if given:
        return given.strip()
    if not source.needs_key:
        return None
    from_env = os.environ.get(source.key_env, "").strip()
    if from_env:
        return from_env
    for directory in (Path.cwd(), Path(__file__).resolve().parent):
        key_file = directory / source.key_file
        if key_file.is_file():
            key = key_file.read_text("utf-8").strip()
            if key:
                return key
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = SOURCES[args.source]
    args.access_key = resolve_access_key(args.access_key, source)
    if source.needs_key and not args.access_key:
        print(
            f"--source {args.source} needs an access key. Pass --access-key, "
            f"set {source.key_env}, or put the key in a {source.key_file} file.",
            file=sys.stderr,
        )
        return 1

    try:
        cards = json.loads(args.input.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read {args.input}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(cards, list) or not cards:
        print(f"{args.input} does not contain a non-empty list of cards", file=sys.stderr)
        return 1
    for index, card in enumerate(cards):
        missing = {"word", "front", "back", "frequency", "visual_link"} - set(card)
        if missing:
            print(f"Card {index} is missing {', '.join(sorted(missing))}", file=sys.stderr)
            return 1
        # It is the only thing the picture is searched for, so an empty one
        # would quietly search for nothing.
        link = card["visual_link"]
        if not isinstance(link, str) or not link.strip():
            print(
                f"Card {index} ({card['word']}) has an empty visual_link",
                file=sys.stderr,
            )
            return 1

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    pairs, by_card = load_pairs(cards)
    collect_media(pairs, args)

    deck, media = build_deck(cards, by_card, args.deck_name)
    package = genanki.Package(deck)
    package.media_files = media
    args.output.parent.mkdir(parents=True, exist_ok=True)
    package.write_to_file(args.output)

    incomplete = [p for p in pairs if p.problems]
    print(
        f"\nWrote {args.output} — {len(cards)} cards, "
        f"{len(media)} media files, deck “{args.deck_name}”."
    )
    if QUOTA.remaining is not None:
        print(f"{QUOTA.remaining} {QUOTA.source} requests left in the current window.")
    if incomplete:
        print(f"{len(incomplete)} sentences are missing media:")
        for pair in incomplete:
            print(f"  {pair.word}: {'; '.join(pair.problems)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
