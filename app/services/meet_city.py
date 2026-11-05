"""A city someone named, matched to the meet tool's cities, and the DM that answers it. Pure.

Two flows ask for a city. @tryalgoviz's meet DM flow asks "Which city is your group in?" and its
answer is TYPED in the chat (below). A comment rule with a ``CityMap`` (@themapworld: "Comment your
city and I'll send you the map") reads the city from the COMMENT and DMs that city's map
(``city_in``, ``answer_for_place``, ``comment_dm``).

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

from pydantic import BaseModel, Field

from app.services.dm_content import DmButton, DmCard, DmMedia, DmMessage

# The one channel whose DM flow asks the city question; typed answers elsewhere are left alone.
TYPED_ANSWER_CHANNEL = "TryAlgoViz"
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


def _match_span(folded: list[str]) -> tuple[str, int, int] | None:
    """(slug, start, length) of the longest run of words naming a city ("greater noida" before "noida")."""
    for size in range(min(LONGEST_NAME, len(folded)), 0, -1):
        for start in range(len(folded) - size + 1):
            run = folded[start : start + size]
            slug = NAMES.get(" ".join(run)) or NAMES.get(" ".join(run).replace("-", " ")) or None
            if slug is None and size == 1:
                # "newyork", "sanfrancisco", "newdelhi": written without the space.
                slug = next((s for n, s in NAMES.items() if " " in n and n.replace(" ", "") == run[0]), None)
            if slug:
                return slug, start, size
    return None


def _match(folded: list[str]) -> str | None:
    """The slug the longest run of words names, if any."""
    span = _match_span(folded)
    return span[0] if span else None


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


# --- a city named in a comment ---

FOLLOW_BUTTON = "🔔 Follow for more"
OPEN_CITY_BUTTON = "📍 Open the map"
CARD_SUBTITLE = "Drop your friends' areas and find the fairest place to meet. Free, no signup."
# A place name, as the model hands it back, is a few words at most.
MAX_PLACE_WORDS = 5


class CityMap(BaseModel):
    """On a comment rule: DM each commenter the map of the city their comment names.

    The DM's copy is fixed here so it names no brand (the page is shown to a foreign audience);
    ``utm_prefix`` keeps one channel's traffic apart from another's (``utm_content=<prefix>-<slug>``).
    """

    utm_prefix: str = Field(default="city", pattern=r"^[a-z0-9-]{1,20}$")
    # The card's picture, uploaded through the DM setup's media route. None sends the card without one.
    image: DmMedia | None = None


# The words a comment may hold beside the city and still be a request on its face: the city alone,
# or the city asked for ("London 🔥", "Mumbai please!!", "do Delhi next", "my city Pune"). Any other
# word ("Love London", "London traffic is insane") means the model judges whether they asked.
REQUEST_WORDS = {
    "please", "pls", "plz", "plss", "plsss", "plzz", "pleaseee", "next", "do", "my", "city", "from", "here",
    "i", "im", "i'm", "am", "me", "for", "bro", "map", "the", "of", "one", "yes", "too", "also", "now",
}  # fmt: skip


def city_in(text: str) -> str | None:
    """The meet tool's city a comment names ("London 🔥", "Mumbai please!!", "nyc!!"), if any."""
    return _match([fold(w) for w in _words(text)])


def quick_city(text: str) -> str | None:
    """One of our cities, when the comment is just that city or a plain ask for it. No model call."""
    folded = [fold(w).replace("’", "'") for w in _words(text)]
    span = _match_span(folded)
    if span is None:
        return None
    slug, start, size = span
    rest = folded[:start] + folded[start + size :]
    return slug if all(w in REQUEST_WORDS for w in rest) else None


class PlaceRead(BaseModel):
    """What the model read in a comment: the place as they named it, the city it is in, and whether they asked."""

    # As they named it, in its usual spelling: "Battersea", "Lagos", "Nigeria".
    place: str | None = None
    # The city that place is in: itself for a city, the parent city for a neighbourhood or borough
    # ("Battersea" -> "London"), None for a region or a country.
    city: str | None = None
    # False when they only mention it ("getting to battersea is a nightmare tho"): no DM then.
    asks: bool = True


def has_words(text: str) -> bool:
    """Whether a comment has any letters at all: "🔥🔥" and "!!!" can name no place."""
    return bool(_words(text))


def answer_for_slug(slug: str, said: str = "") -> CityAnswer:
    return CityAnswer(
        outcome="matched", name=CITIES[slug], normalised=" ".join(fold(w) for w in _words(said)), slug=slug
    )


def answer_for_place(place: str) -> CityAnswer:
    """A place name the model read out of a comment: one of our cities, or another place."""
    words = _strip(_words(place.strip()))
    folded = [fold(w) for w in words]
    if not words or len(words) > MAX_PLACE_WORDS or len(place) > MAX_CHARS:
        return CityAnswer(outcome="ignored", normalised=" ".join(folded))
    slug = _match(folded)
    if slug is not None:
        return answer_for_slug(slug, place)
    name = " ".join(words)
    if name == name.lower():
        name = " ".join(w[:1].upper() + w[1:] for w in words)
    return CityAnswer(outcome="unlisted", name=name, normalised=" ".join(folded))


def answer_for_read(read: PlaceRead) -> CityAnswer:
    """One of our cities (a neighbourhood counts as its city), another place, or nothing to answer.

    Tallied under the city, with the place they named as ``normalised``: Battersea is demand for London.
    """
    named = (read.place or read.city or "").strip()
    if not read.asks or not named:
        return CityAnswer(outcome="ignored", normalised=fold(named))
    for candidate in (read.city, read.place):
        if candidate:
            answer = answer_for_place(candidate)
            if answer.outcome == "matched" and answer.slug:
                return answer_for_slug(answer.slug, named)
    # Not on the map: name the city when it is a neighbourhood of one ("Ikeja" -> "Lagos is on our list").
    answer = answer_for_place(read.city or named)
    if answer.outcome == "unlisted" and read.city and read.place:
        return answer.model_copy(update={"normalised": " ".join(fold(w) for w in _words(read.place))})
    return answer


def comment_dm(answer: CityAnswer, city_map: CityMap) -> DmMessage | None:
    """The DM a comment naming *answer* gets: that city's map as a card, or the map with the city on our list."""
    follow = DmButton(type="profile", title=FOLLOW_BUTTON)
    prefix = city_map.utm_prefix
    if answer.outcome == "matched" and answer.slug:
        url = map_url(f"{prefix}-{answer.slug}", answer.slug)
        card = DmCard(
            title=f"Your {answer.name} map is ready 🗺️",
            subtitle=CARD_SUBTITLE,
            image=city_map.image,
            link=url,
            buttons=[DmButton(type="url", title=OPEN_CITY_BUTTON, url=url), follow],
        )
        return DmMessage(cards=[card])
    if answer.outcome == "unlisted":
        text = f"{answer.name} is on our list ✅, we're adding it. Try the cities that are live meanwhile 👇"
        return DmMessage(
            text=text, buttons=[DmButton(type="url", title=MAP_BUTTON, url=map_url(f"{prefix}-other")), follow]
        )
    return None


def sample_comment_dm(city_map: CityMap) -> DmMessage:
    """What a city rule sends for London: checked when the rule is saved, so a bad image fails then."""
    message = comment_dm(answer_for_slug("london", "London"), city_map)
    if message is None:  # London is one of CITIES, so this is always a card.
        raise RuntimeError("London is missing from CITIES")
    return message
