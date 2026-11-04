"""A city someone TYPED in @tryalgoviz's meet DM flow, matched to the meet tool's cities. Pure.

The flow asks "Which city is your group in?" with eight chips, and its "Other" reply asks them to type
it. A typed answer used to get nothing back (4 Nov: "Trivandrum", "İstanbul"). This reads the text,
finds the meet tool's city it names, and builds the reply:

- a city the tool has: what that city's chip sends, or the same template for a city with no chip;
- anything else that reads as a place: "<their city> is on our list" and the map without a city.

Owner's rule: a reply must never leave someone thinking their city isn't covered — no counts, no
"we don't have it". Text that doesn't read as a city ("thanks", a question, a sentence) gets no reply.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel

from app.services.dm_content import DmButton, DmMessage

CHANNEL_ID = "TryAlgoViz"
# Both prompts start with this: the chip question and the "Other" reply that asks them to type it.
PROMPT_PREFIX = "Which city is your group in?"
MAP_URL = "https://tryalgoviz.com/algorithms/graph/city-map/meet/"
UTM = "utm_source=instagram&utm_medium=dm&utm_campaign=comment-dm"
MAP_BUTTON = "🗺️ Open the map"

# The meet tool's REAL_CITIES, slug -> how a reply names it.
CITIES: dict[str, str] = {
    "mumbai": "Mumbai",
    "bengaluru": "Bengaluru",
    "delhi": "Delhi",
    "kolkata": "Kolkata",
    "hyderabad": "Hyderabad",
    "chennai": "Chennai",
    "pune": "Pune",
    "jaipur": "Jaipur",
    "nagpur": "Nagpur",
    "ludhiana": "Ludhiana",
    "indore": "Indore",
    "new-york": "New York",
    "paris": "Paris",
    "london": "London",
    "tokyo": "Tokyo",
    "amsterdam": "Amsterdam",
    "san-francisco": "San Francisco",
    "chicago": "Chicago",
    "barcelona": "Barcelona",
    "manhattan": "Manhattan",
    "dubai": "Dubai",
    "berlin": "Berlin",
    "singapore": "Singapore",
}
ALIASES: dict[str, str] = {
    "bangalore": "bengaluru",
    "bengaluru": "bengaluru",
    "blr": "bengaluru",
    "bombay": "mumbai",
    "new delhi": "delhi",
    "gurgaon": "delhi",
    "gurugram": "delhi",
    "noida": "delhi",
    "greater noida": "delhi",
    "ghaziabad": "delhi",
    "ncr": "delhi",
    "delhi ncr": "delhi",
    "hyd": "hyderabad",
    "secunderabad": "hyderabad",
    "madras": "chennai",
    "calcutta": "kolkata",
    "poona": "pune",
    "nyc": "new-york",
    "new york city": "new-york",
    "sf": "san-francisco",
}
# Every name that means a city, normalised -> slug. A slug's own spaced form is one of them.
NAMES: dict[str, str] = {**{s.replace("-", " "): s for s in CITIES}, **ALIASES}
LONGEST_NAME = max(len(n.split()) for n in NAMES)

# Words around a city name that aren't part of it: "I'm from Pune", "Pune city", "Thane district".
FILLER = {
    "i", "im", "i'm", "am", "we", "were", "we're", "are", "our", "my", "group", "is", "its", "it's", "in",
    "from", "at", "the", "bro", "bhai", "sir", "mam", "ma'am", "near", "based", "live", "stay",
}  # fmt: skip
SUFFIXES = {"city", "district", "dist", "area", "region", "metro", "town", "india", "state", "side"}
# Replies that are not a place. A chip tap never reaches here; these are typed.
NOT_A_CITY = {
    "other", "others", "ok", "okay", "k", "thanks", "thank", "thank you", "thx", "ty", "yes", "no", "yeah",
    "yep", "hi", "hello", "hey", "done", "nice", "cool", "wow", "great", "good", "hmm", "lol", "test",
}  # fmt: skip
# Words no place name is made of: a reply holding one is a sentence or a question, not "<city> is on our list".
NOT_PLACE_WORDS = {
    "what", "how", "why", "when", "where", "who", "which", "this", "that", "it", "you", "u", "me", "link", "map",
    "not", "working", "open", "send", "please", "pls", "plz", "can", "do", "does", "have", "has", "there",
    "here", "and", "or", "but", "to", "for", "of", "a", "an", "will", "want", "need", "know", "see", "show",
}  # fmt: skip
MAX_CHARS = 40
MAX_WORDS = 3

Outcome = Literal["matched", "unlisted", "ignored"]


class CityAnswer(BaseModel):
    outcome: Outcome
    # Their words with the filler taken off, as a reply names it ("İstanbul", "Trivandrum").
    name: str = ""
    # Accent-free, lower case, the key a tally groups by ("istanbul").
    normalised: str = ""
    slug: str | None = None


def fold(text: str) -> str:
    """Lower case without accents: "İstanbul" -> "istanbul", "Bengalūru" -> "bengaluru"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    # Letters (any script), apostrophes inside a word, and hyphens; everything else (emoji, dots, commas) splits.
    return re.findall(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", text)


def _strip(words: list[str]) -> list[str]:
    kept = [w for w in words if fold(w).replace("’", "'") not in FILLER]
    while kept and fold(kept[-1]) in SUFFIXES:
        kept.pop()
    return kept


def _match(folded: list[str]) -> str | None:
    """The slug the longest run of words names ("greater noida" before "noida"), if any."""
    for size in range(min(LONGEST_NAME, len(folded)), 0, -1):
        for start in range(len(folded) - size + 1):
            run = folded[start : start + size]
            slug = NAMES.get(" ".join(run)) or NAMES.get(" ".join(run).replace("-", " ")) or None
            if slug is None and size == 1:
                # "newyork", "sanfrancisco", "newdelhi": written without the space.
                slug = next((s for n, s in NAMES.items() if " " in n and n.replace(" ", "") == run[0]), None)
            if slug:
                return slug
    return None


def read_answer(text: str) -> CityAnswer:
    """What a typed reply to the city question is: one of our cities, another place, or not a place."""
    raw = text.strip()
    if not raw or len(raw) > MAX_CHARS or "http" in raw.casefold():
        return CityAnswer(outcome="ignored", normalised=fold(raw)[:MAX_CHARS])
    words = _strip(_words(raw))
    folded = [fold(w) for w in words]
    normalised = " ".join(folded)
    if not words:
        return CityAnswer(outcome="ignored", normalised=fold(raw))
    slug = _match(folded)
    if slug is not None:
        return CityAnswer(outcome="matched", name=CITIES[slug], normalised=normalised, slug=slug)
    if (
        len(words) > MAX_WORDS
        or normalised in NOT_A_CITY
        or any(w in NOT_PLACE_WORDS for w in folded)
        # "Ahmedabad??" asks for a city; a longer question is about something else.
        or ("?" in raw and len(words) > 1)
    ):
        return CityAnswer(outcome="ignored", normalised=normalised)
    # "Kochi, Kerala": the place is the part before the comma, the rest is where it is.
    first = _strip(_words(re.split(r"[,/|]", raw)[0])) or words
    name = " ".join(first)
    normalised = " ".join(fold(w) for w in first)
    # "trivandrum" reads better as "Trivandrum"; a name they cased themselves ("İstanbul", "McLeod") is kept.
    if name == name.lower():
        name = " ".join(w[:1].upper() + w[1:] for w in first)
    return CityAnswer(outcome="unlisted", name=name, normalised=normalised)


def map_url(content: str, slug: str | None = None) -> str:
    city = f"city={slug}&" if slug else ""
    return f"{MAP_URL}?{city}{UTM}&utm_content={content}"


def chip_content(slug: str) -> str:
    return f"utm_content=meet-chip-{slug}"


def typed_reply(answer: CityAnswer) -> DmMessage | None:
    """The reply for a typed city that has no chip of its own (a chip city sends its chip's reply)."""
    if answer.outcome == "matched" and answer.slug:
        city = answer.name
        text = (
            f"{city} it is 🗺️\n\nThe map is already set to {city}. Drop your 4 friends' areas and see who travels "
            "the most 😅\n\nMost-asked city gets the next reel 👀"
        )
        url = map_url(f"meet-typed-{answer.slug}", answer.slug)
    elif answer.outcome == "unlisted":
        text = f"{answer.name} is on our list ✅ We're adding it soon, and the most-asked cities get their own reel 🙌"
        url = map_url("meet-typed-other")
    else:
        return None
    return DmMessage(text=text, buttons=[DmButton(type="url", title=MAP_BUTTON, url=url)])


def is_prompt(text: str) -> bool:
    return text.strip().startswith(PROMPT_PREFIX)
