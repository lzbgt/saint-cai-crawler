#!/usr/bin/env python3
"""
Utility to inspect Shengcai reader chapters during development.

The script mirrors the quick-and-dirty snippets we used while debugging:
it pretty-prints the raw HTML of a QuestionTitle block from `tmp.html`
and, when available, shows the corresponding structured entry from
`output/chapter.json`.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from bs4 import BeautifulSoup, Tag


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect a QuestionTitle node and matching structured output."
    )
    parser.add_argument(
        "--html",
        default="tmp.html",
        help="HTML source to inspect (default: tmp.html).",
    )
    parser.add_argument(
        "--json",
        default="output/chapter.json",
        help="Structured JSON file for cross-reference (default: output/chapter.json).",
    )
    parser.add_argument(
        "--question",
        "-q",
        help="Question number to inspect (e.g. 4). If omitted, list all available numbers.",
    )
    parser.add_argument(
        "--index",
        type=int,
        help="1-based index of the QuestionTitle node to inspect (overrides --question filter).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit the number of results shown when --question is not provided.",
    )
    return parser.parse_args()


def iter_question_nodes(soup: BeautifulSoup) -> Iterable[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for idx, node in enumerate(soup.find_all("p", class_="QuestionTitle"), start=1):
        number = None
        for cls in ("QuestionNum1", "QuestionNum2"):
            span = node.find("span", class_=cls)
            if span:
                number = span.get_text(strip=True)
                break
        occurrence = None
        if number:
            counts[number] = counts.get(number, 0) + 1
            occurrence = counts[number]
        yield {
            "index": idx,
            "number": number,
            "occurrence": occurrence,
            "node": node,
            "html": node.prettify(),
        }


def build_structured_lookup(json_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    if not json_path.exists():
        return {}
    with json_path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    lookup: Dict[str, List[Dict[str, Any]]] = {}
    for section in data.get("sections", []):
        for item in section.get("items", []):
            if item.get("type") != "qa":
                continue
            key = item.get("number")
            if key:
                lookup.setdefault(key, []).append(item)
    return lookup


def highlight_question(
    entry: Dict[str, Any], structured: Optional[Dict[str, Any]]
) -> None:
    number = entry.get("number") or f"#{entry['index']}"
    occurrence = entry.get("occurrence")
    print("=" * 72)
    print(f"Question {number}")
    print("=" * 72)
    if occurrence:
        print(f"(Occurrence {occurrence} among QuestionTitle nodes numbered {number})")
    print("Raw HTML block:")
    print(entry["html"])

    clean_clone = BeautifulSoup(entry["html"], "html.parser")
    for cls in ("QuestionNum1", "QuestionNum2"):
        for span in clean_clone.find_all("span", class_=cls):
            span.decompose()
    rich_text = _stringify_with_markup(clean_clone)
    print("\nFlattened text (markup preserved):")
    print(textwrap.fill(rich_text, width=80))

    if structured:
        print("\nStructured JSON fields:")
        print(f"- question: {structured.get('question')!r}")
        if structured.get("question_rich"):
            print("- question_rich:")
            for part in structured["question_rich"]:
                if isinstance(part, dict) and part.get("type") == "image":
                    print(
                        f"    • [image] url={part.get('url')} file={part.get('file')} "
                        f"size={part.get('width')}x{part.get('height')}"
                    )
                else:
                    print(f"    • {part!r}")
        if structured.get("analysis_lines"):
            print("- analysis_lines:")
            for part in structured["analysis_lines"]:
                if isinstance(part, dict) and part.get("type") == "image":
                    print(
                        f"    • [image] url={part.get('url')} file={part.get('file')}"
                    )
                else:
                    print(f"    • {part!r}")
        if structured.get("answer_lines"):
            print(f"- answer_lines: {structured.get('answer_lines')}")
    else:
        print("\nStructured JSON entry not found.")


def _stringify_with_markup(node: Any) -> str:
    if getattr(node, "name", None) == "br":
        return "\n"
    if isinstance(node, str):
        return node
    if isinstance(node, Tag) and node.name in {"sub", "sup"}:
        inner = "".join(_stringify_with_markup(child) for child in node.children).strip()
        return f"<{node.name}>{inner}</{node.name}>"
    if isinstance(node, Tag):
        return "".join(_stringify_with_markup(child) for child in node.children)
    return ""


def main() -> None:
    args = parse_args()

    html_path = Path(args.html)
    if not html_path.exists():
        raise SystemExit(f"HTML file not found: {html_path}")
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

    structured_lookup = build_structured_lookup(Path(args.json))

    matches: List[Dict[str, Any]] = []
    for entry in iter_question_nodes(soup):
        if args.index and entry["index"] != args.index:
            continue
        number = entry.get("number")
        if args.question and number != args.question:
            continue
        matches.append(entry)

    if not matches:
        available = ", ".join(
            filter(None, (entry.get("number") for entry in iter_question_nodes(soup)))
        )
        hint = f" Available question numbers: {available}" if available else ""
        raise SystemExit(f"No matching questions found.{hint}")

    if args.limit:
        matches = matches[: args.limit]

    for entry in matches:
        structured = None
        number = entry.get("number")
        occurrence = entry.get("occurrence")
        if number and occurrence:
            candidates = structured_lookup.get(number, [])
            if occurrence - 1 < len(candidates):
                structured = candidates[occurrence - 1]
        highlight_question(entry, structured)


if __name__ == "__main__":
    main()
