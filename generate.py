import datetime as dt
import email.utils
import html
import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


API_ROOT = "https://api.stackexchange.com/2.3"
CONFIG_FILE = Path("feeds.json")
STATE_FILE = Path("state/seen.json")
OUTPUT_DIRECTORY = Path("docs")


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def isoformat(value):
    return value.isoformat().replace("+00:00", "Z")


def parse_isoformat(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def rss_date(value):
    return email.utils.format_datetime(value)


def api_get(path, parameters):
    api_key = os.environ.get("STACKEXCHANGE_KEY")

    if api_key:
        parameters["key"] = api_key

    url = (
        API_ROOT
        + path
        + "?"
        + urllib.parse.urlencode(parameters)
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Personal Stack Exchange RSS feed generator"
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)

    if "error_message" in result:
        raise RuntimeError(result["error_message"])

    # Stack Exchange sometimes instructs clients to pause.
    if result.get("backoff"):
        time.sleep(int(result["backoff"]))

    remaining = result.get("quota_remaining")

    if remaining is not None:
        print(f"Stack Exchange API quota remaining: {remaining}")

    return result


def chunks(values, size):
    values = list(values)

    for position in range(0, len(values), size):
        yield values[position:position + size]


def fetch_answers(feed):
    beginning = utc_now() - dt.timedelta(
        days=int(feed.get("lookback_days", 90))
    )

    parameters = {
        "site": feed["site"],
        "sort": "votes",
        "order": "desc",
        "min": int(feed["minimum_score"]),
        "fromdate": int(beginning.timestamp()),
        "pagesize": 100,
    }

    answers = []
    page = 1
    maximum_pages = int(feed.get("maximum_api_pages", 20))

    while page <= maximum_pages:
        parameters["page"] = page
        result = api_get("/answers", parameters.copy())
        answers.extend(result.get("items", []))

        if not result.get("has_more"):
            break

        page += 1

    if page > maximum_pages:
        print(
            f"Warning: {feed['slug']} reached its configured "
            f"page limit of {maximum_pages}."
        )

    return answers


def fetch_questions(site, question_ids):
    questions = {}

    for group in chunks(sorted(set(question_ids)), 100):
        if not group:
            continue

        joined_ids = ";".join(str(value) for value in group)

        result = api_get(
            f"/questions/{joined_ids}",
            {
                "site": site,
                "pagesize": 100,
            },
        )

        for question in result.get("items", []):
            questions[str(question["question_id"])] = question

    return questions


def answer_matches_tags(
    question,
    required_tags,
    excluded_tags,
):
    question_tags = set(question.get("tags", []))

    has_required_tags = set(required_tags).issubset(
        question_tags
    )

    has_excluded_tags = not question_tags.isdisjoint(
        excluded_tags
    )

    return has_required_tags and not has_excluded_tags

def make_entry(feed, answer, question, first_seen):
    answer_id = str(answer["answer_id"])
    question_id = str(answer["question_id"])

    owner = answer.get("owner", {})
    owner_name = html.unescape(
        owner.get("display_name", "an unknown user")
    )

    question_title = html.unescape(
        question.get("title", f"Question {question_id}")
    )

    link = f"{feed['site_url'].rstrip('/')}/a/{answer_id}"

    return {
        "answer_id": answer_id,
        "question_id": question_id,
        "question_title": question_title,
        "link": link,
        "owner": owner_name,
        "score": int(answer.get("score", 0)),
        "answer_created": isoformat(
            dt.datetime.fromtimestamp(
                answer["creation_date"],
                tz=dt.timezone.utc,
            )
        ),
        "first_seen": first_seen,
    }


def write_rss(feed, entries, checked_at):
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:atom": "http://www.w3.org/2005/Atom",
        },
    )

    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = feed["title"]
    ET.SubElement(channel, "link").text = feed["site_url"]
    ET.SubElement(channel, "description").text = (
        f"Answers from {feed['site']} that reached a score of "
        f"{feed['minimum_score']} or more."
    )
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = rss_date(checked_at)

    for entry in entries:
        item = ET.SubElement(channel, "item")

        ET.SubElement(item, "title").text = (
            f"{entry['question_title']} — answer reached "
            f"score {entry['score']}"
        )

        ET.SubElement(item, "link").text = entry["link"]

        guid = ET.SubElement(
            item,
            "guid",
            {"isPermaLink": "false"},
        )
        guid.text = (
            f"stackexchange:{feed['site']}:answer:"
            f"{entry['answer_id']}"
        )

        ET.SubElement(item, "pubDate").text = rss_date(
            parse_isoformat(entry["first_seen"])
        )

        ET.SubElement(item, "description").text = (
            f"Answer by {entry['owner']}. "
            f"Score when last checked: {entry['score']}. "
            f"Originally posted: {entry['answer_created']}."
        )

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")

    output_path = OUTPUT_DIRECTORY / f"{feed['slug']}.xml"

    tree.write(
        output_path,
        encoding="utf-8",
        xml_declaration=True,
    )


def write_index(feeds):
    links = []

    for feed in feeds:
        filename = f"{feed['slug']}.xml"
        links.append(
            f'<li><a href="{html.escape(filename)}">'
            f'{html.escape(feed["title"])}</a></li>'
        )

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Stack Exchange feeds</title>
</head>
<body>
  <h1>Stack Exchange feeds</h1>
  <ul>
    {''.join(links)}
  </ul>
</body>
</html>
"""

    (OUTPUT_DIRECTORY / "index.html").write_text(
        document,
        encoding="utf-8",
    )


def process_feed(feed, state, checked_at):
    slug = feed["slug"]

    feed_state = state.setdefault(
        slug,
        {
            "known": {},
            "entries": [],
        },
    )

    answers = fetch_answers(feed)

    question_ids = [
        answer["question_id"]
        for answer in answers
    ]

    questions = fetch_questions(feed["site"], question_ids)
    required_tags = feed.get("tags", [])
    excluded_tags = feed.get("exclude_tags", [])

    qualifying = []
    
    for answer in answers:
        question = questions.get(str(answer["question_id"]), {})
    
        if answer_matches_tags(
            question,
            required_tags,
            excluded_tags,
        ):
            qualifying.append((answer, question))
        
    # Prefer newer answers when initially constructing the feed.
    qualifying.sort(
        key=lambda pair: pair[0]["creation_date"],
        reverse=True,
    )

    entries_by_id = {
        entry["answer_id"]: entry
        for entry in feed_state.get("entries", [])
    }

    now_text = isoformat(checked_at)

    for answer, question in qualifying:
        answer_id = str(answer["answer_id"])

        if answer_id not in feed_state["known"]:
            feed_state["known"][answer_id] = now_text

            entries_by_id[answer_id] = make_entry(
                feed,
                answer,
                question,
                now_text,
            )

        elif answer_id in entries_by_id:
            # Keep the score and title reasonably current.
            entries_by_id[answer_id]["score"] = int(
                answer.get("score", 0)
            )

            if question.get("title"):
                entries_by_id[answer_id]["question_title"] = (
                    html.unescape(question["title"])
                )

    entries = list(entries_by_id.values())

    entries.sort(
        key=lambda entry: (
            parse_isoformat(entry["first_seen"]),
            parse_isoformat(entry["answer_created"]),
        ),
        reverse=True,
    )

    maximum_items = int(feed.get("maximum_feed_items", 100))
    entries = entries[:maximum_items]

    feed_state["entries"] = entries
    feed_state["last_checked"] = now_text

    write_rss(feed, entries, checked_at)

    print(
        f"{slug}: {len(qualifying)} qualifying answers; "
        f"{len(entries)} RSS entries."
    )


def main():
    with CONFIG_FILE.open(encoding="utf-8") as source:
        feeds = json.load(source)

    if STATE_FILE.exists():
        with STATE_FILE.open(encoding="utf-8") as source:
            state = json.load(source)
    else:
        state = {}

    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    checked_at = utc_now()

    for feed in feeds:
        process_feed(feed, state, checked_at)

    write_index(feeds)

    with STATE_FILE.open("w", encoding="utf-8") as destination:
        json.dump(
            state,
            destination,
            indent=2,
            sort_keys=True,
        )
        destination.write("\n")


if __name__ == "__main__":
    main()
