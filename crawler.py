#!/usr/bin/env python3
"""
Crawler for extracting text and figures from the Shengcai reader pages.

The script fetches the encrypted chapter content, uses a lightweight Node.js
bridge to reuse the site's own decryption helper (getChapCore), and then
parses the resulting HTML with BeautifulSoup to collect text paragraphs and
download linked images.

Prerequisites:
  - Python 3 with requests and beautifulsoup4 installed.
  - Node.js available on PATH, with the packages jsdom, jquery and crypto-js
    (install via `npm install jsdom@22 jquery crypto-js` once).

Usage:
  python crawler.py \
      --url "http://reading.sc.zzstep.com/reader/reader.aspx?id=1003244&From=Ebook&UserName=...&chapId=chap3&pId=chap3_1" \
      --output ./output
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Tuple, Dict, Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import re
from functools import lru_cache

import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def load_cookies_from_file(path: Path) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    if not path.exists():
        raise FileNotFoundError(f"Cookie file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, flag, path_value, secure_flag, expiry, name, value = parts
        if domain.startswith("#HttpOnly_"):
            domain = domain[len("#HttpOnly_") :]
        secure = secure_flag.upper() == "TRUE"
        expires = None
        if expiry and expiry.isdigit():
            try:
                expires = int(expiry)
            except ValueError:
                expires = None
        cookie = requests.cookies.create_cookie(
            domain=domain,
            name=name,
            value=value,
            path=path_value or "/",
            secure=secure,
            expires=expires,
        )
        jar.set_cookie(cookie)
    return jar


def parse_inline_cookie(cookie_str: str) -> Tuple[str, str]:
    if "=" not in cookie_str:
        raise ValueError(f"Cookie must be in name=value format: {cookie_str}")
    name, value = cookie_str.split("=", 1)
    return name.strip(), value.strip()


def jar_to_playwright_cookies(
    jar: requests.cookies.RequestsCookieJar,
) -> List[Dict[str, Any]]:
    cookies: List[Dict[str, Any]] = []
    for cookie in jar:
        domain = cookie.domain or ""
        if domain.startswith("."):
            domain_value = domain
        elif domain:
            domain_value = domain
        else:
            continue
        cookie_dict: Dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": domain_value,
            "path": cookie.path or "/",
        }
        if cookie.expires:
            cookie_dict["expires"] = cookie.expires
        if cookie.secure is not None:
            cookie_dict["secure"] = bool(cookie.secure)
        cookies.append(cookie_dict)
    return cookies


NODE_BRIDGE = r"""
const {JSDOM} = require('jsdom');
const jqueryFactory = require('jquery');
const CryptoJS = require('crypto-js');

async function main() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  const input = JSON.parse(chunks.join(''));

  const dom = new JSDOM(input.html, { url: 'https://reading.sc.zzstep.com/reader/reader.aspx' });
  const { window } = dom;
  global.window = window;
  global.document = window.document;
  global.navigator = { language: 'zh-CN', userAgent: 'node.js' };

  const $ = jqueryFactory(window);
  global.$ = $;
  global.jQuery = $;
  if (!$.fn.draggable) {
    $.fn.draggable = function() { return this; };
  }
  if (!$.fn.resizable) {
    $.fn.resizable = function() { return this; };
  }
  if (!$.fn.sortable) {
    $.fn.sortable = function() { return this; };
  }

  global.readercrypto = CryptoJS;
  global.defaultVideoPoster = '';
  global.chapId = input.chapId;

  const storage = new Map();
  global.localStorage = {
    getItem: key => storage.has(key) ? storage.get(key) : null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: key => storage.delete(key),
    clear: () => storage.clear(),
  };
  global.sessionStorage = {
    getItem: key => storage.has(`sess_${key}`) ? storage.get(`sess_${key}`) : null,
    setItem: (key, value) => storage.set(`sess_${key}`, String(value)),
    removeItem: key => storage.delete(`sess_${key}`),
    clear: () => {
      for (const key of Array.from(storage.keys())) {
        if (key.startsWith('sess_')) {
          storage.delete(key);
        }
      }
    },
  };

  const vueStub = function() {
    return { $nextTick: () => {}, $watch: () => {}, $el: null };
  };
  vueStub.component = () => {};
  vueStub.use = () => {};
  vueStub.directive = () => {};
  vueStub.mixin = () => {};
  vueStub.config = { productionTip: false };
  global.Vue = vueStub;
  window.Vue = vueStub;

  eval(input.confirmAlert);
  eval(input.errorCorrection);
  eval(input.readerMin);
  eval(input.readerCryp);
  eval(input.inlineScript);

  const result = getChapCore(input.host, '#ffffff', true);
  process.stdout.write(JSON.stringify(result));
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
"""


def fetch_page(url: str, session: requests.Session) -> BeautifulSoup:
    resp = session.get(url)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


@lru_cache(maxsize=1)
def load_reader_min_script() -> str:
    script_path = Path(__file__).with_name("reader.min.js")
    if not script_path.exists():
        raise FileNotFoundError("reader.min.js not found next to crawler.py")
    return script_path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def fetch_readercryp_script() -> str:
    resp = requests.get("http://reading.sc.zzstep.com/reader/js/readercryp.min.js")
    resp.raise_for_status()
    return resp.text


@lru_cache(maxsize=1)
def fetch_confirm_alert_script() -> str:
    resp = requests.get(
        "http://reading.sc.zzstep.com/reader/js/scConfirmAndAlert.js?v=202508210821"
    )
    resp.raise_for_status()
    return resp.text


@lru_cache(maxsize=1)
def fetch_error_correction_script() -> str:
    resp = requests.get(
        "http://reading.sc.zzstep.com/reader/js/errorCorrectionList.min.js?v=202508210821"
    )
    resp.raise_for_status()
    return resp.text


def _extract_inline_script(reader_page: BeautifulSoup) -> str:
    for script in reader_page.find_all("script"):
        content = script.string
        if not content:
            continue
        if "getChapCore" in content:
            return re.sub(r"function t\(\)[\s\S]+?\}\s*t\(\);", "", content, count=1)
    raise RuntimeError("Inline getChapCore script not found in reader page.")


def decrypt_chapter(
    reader_page: BeautifulSoup, node_bridge: str
) -> str:
    chap_id = reader_page.find("input", {"id": "hiddenChapId"})["value"]
    host_root = reader_page.find("input", {"id": "hiddenHostUrl"})["value"]
    host_url = f"https://eshu.{host_root}"

    inline_script = _extract_inline_script(reader_page)

    payload = json.dumps(
        {
            "html": str(reader_page),
            "readerMin": load_reader_min_script(),
            "readerCryp": fetch_readercryp_script(),
            "confirmAlert": fetch_confirm_alert_script(),
            "errorCorrection": fetch_error_correction_script(),
            "inlineScript": inline_script,
            "chapId": chap_id,
            "host": host_url,
        }
    )

    result = subprocess.run(
        ["node", "-e", node_bridge],
        input=payload,
        text=True,
        capture_output=True,
        check=True,
    )
    bridge_output = json.loads(result.stdout)
    status = str(bridge_output.get("chapStatus"))
    if status != "1":
        err = bridge_output.get("chapErrMsg") or "unknown error"
        raise RuntimeError(f"getChapCore returned status {status}: {err}")
    chap_str = bridge_output.get("chapStr")
    if chap_str is None:
        raise RuntimeError("getChapCore did not return chapter content.")
    return chap_str


def extract_chapter_sequence(reader_page: BeautifulSoup) -> List[Dict[str, Any]]:
    """Extract ordered chapter metadata from the hidden navMap block."""

    nav_div = reader_page.find("div", {"id": "hiddenNcxStr"})
    if not nav_div:
        return []

    nav_content = nav_div.decode_contents() if hasattr(nav_div, "decode_contents") else str(nav_div)
    nav_soup = BeautifulSoup(nav_content, "xml")

    def iter_navpoints(parent: Tag) -> Iterable[Tag]:
        tag_name = parent.name or ""
        if tag_name.lower() in {"navmap", "navpoint"}:
            for child in parent.find_all("navPoint", recursive=False):
                yield child
        else:
            for child in parent.find_all("navPoint"):
                yield child

    chapters: List[Dict[str, Any]] = []
    seen: set[str] = set()
    seq_counter = 0

    def child_navpoints(node: Tag) -> List[Tag]:
        children = node.find_all("navpoint", recursive=False)
        if not children:
            children = node.find_all("navPoint", recursive=False)
        return children

    def walk(node: Tag, parents: List[str]) -> None:
        nonlocal seq_counter

        text_tag = node.find("text")
        title = text_tag.get_text(strip=True) if text_tag else None
        current_parents = parents + ([title] if title else [])

        content_tag = node.find("content")
        src = content_tag.get("src") if content_tag else None
        chap_id, section_id = _split_chapter_src(src)

        if chap_id and chap_id not in seen:
            seen.add(chap_id)
            play_order = node.get("playOrder") or node.get("playorder")
            try:
                order = int(play_order) if play_order is not None else None
            except ValueError:
                order = None
            seq_counter += 1
            chapters.append(
                {
                    "chap_id": chap_id,
                    "section_id": section_id,
                    "title": current_parents[-1] if current_parents else None,
                    "full_title": " / ".join(current_parents) if current_parents else None,
                    "order": order,
                    "sequence": seq_counter,
                }
            )

        for child in child_navpoints(node):
            walk(child, current_parents)

    nav_root_candidates = nav_soup.find_all("navMap")
    if not nav_root_candidates:
        nav_root_candidates = nav_soup.find_all("navmap")
    if not nav_root_candidates:
        nav_root_candidates = [nav_soup]

    for root in nav_root_candidates:
        for navpoint in child_navpoints(root):
            walk(navpoint, [])

    chapters.sort(key=lambda item: (item["order"] if item["order"] is not None else float("inf"), item["sequence"]))
    return chapters


def build_chapter_url(base_url: str, chap_id: str, p_id: Optional[str] = None) -> str:
    """Return a reader URL that targets the requested chapter."""
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["chapId"] = [chap_id]
    if p_id:
        query["pId"] = [p_id]
    else:
        query["pId"] = [f"{chap_id}_1"]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _node_text(tag: Tag) -> str:
    return _normalize_text(_stringify_with_markup(tag))


def _split_choice(text: str) -> tuple[str | None, str]:
    if not text:
        return None, ""
    stripped = text.strip()
    if not stripped:
        return None, ""
    label = stripped[0]
    ascii_labels = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    full_labels = "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    if label in ascii_labels or label in full_labels:
        if len(stripped) == 1:
            return label, ""
        if stripped[1] in "．.、)）：: ":
            remainder = stripped[2:].lstrip("．.、)）：: ")
            return label, remainder
    return None, text


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("\xa0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _stringify_with_markup(node: Any) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    if node.name == "br":
        return "\n"
    if node.name in {"sub", "sup"}:
        inner = "".join(_stringify_with_markup(child) for child in node.children)
        inner = inner.strip()
        if not inner:
            return ""
        return f"<{node.name}>{inner}</{node.name}>"
    return "".join(_stringify_with_markup(child) for child in node.children)


def _append_text_part(parts: List[Any], text: str) -> None:
    if not text:
        return
    if parts and isinstance(parts[-1], str):
        parts[-1] += text
    else:
        parts.append(text)


def _split_chapter_src(src: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not src:
        return None, None
    cleaned = src.split("#", 1)[0]
    cleaned = cleaned.rsplit("/", 1)[-1]
    base = cleaned.split(".", 1)[0]
    if not base:
        return None, None
    chap_id = base.split("_", 1)[0]
    section_id = base
    return chap_id, section_id


_FULLWIDTH_REPLACEMENTS = [
    ("（", "("),
    ("）", ")"),
    ("，", ", "),
    ("。", ". "),
    ("．", ". "),
    ("；", "; "),
    ("：", ": "),
    ("＋", "+"),
    ("－", "-"),
    ("＝", "="),
    ("＜", "<"),
    ("＞", ">"),
]

_MATH_LATEX_PATTERN = re.compile(
    r"(\\[A-Za-z]+(?:\{[^}]+\})+|(?:\([^\)]+\)|[A-Za-zΑ-Ωα-ω][A-Za-z0-9Α-Ωα-ω]*)(?:_{[^}]+}|\^{[^}]+})+)",
    re.UNICODE,
)


def _convert_markup_to_latex(text: str) -> str:
    if not text:
        return text
    prefix = re.match(r"^\s*", text).group(0)
    suffix = re.search(r"\s*$", text).group(0)
    core = text.strip()
    if not core:
        return prefix + suffix
    core = core.replace("\xa0", " ").replace("&nbsp;", " ")
    core = re.sub(r"<\s*br\s*/?>", "\n", core, flags=re.IGNORECASE)
    core = re.sub(r"<\s*sub\s*>", "_{", core, flags=re.IGNORECASE)
    core = re.sub(r"<\s*/\s*sub\s*>", "}", core, flags=re.IGNORECASE)
    core = re.sub(r"<\s*sup\s*>", "^{", core, flags=re.IGNORECASE)
    core = re.sub(r"<\s*/\s*sup\s*>", "}", core, flags=re.IGNORECASE)
    core = re.sub(r"<[^>]+>", "", core)
    for src, dst in _FULLWIDTH_REPLACEMENTS:
        core = core.replace(src, dst)
    core = re.sub(r"\{\s+", "{", core)
    core = re.sub(r"\s+\}", "}", core)

    def _merge_adjacent(marker: str, value: str) -> str:
        pattern = re.compile(rf"\{marker}\{{([^}}]+)\}}\{marker}\{{([^}}]+)\}}")
        while True:
            value, count = pattern.subn(lambda m: f"{marker}{{{m.group(1)}{m.group(2)}}}", value)
            if count == 0:
                break
        return value

    core = _merge_adjacent("^", core)
    core = _merge_adjacent("_", core)
    core = re.sub(r"\s+", " ", core).strip()

    def _wrap(match: re.Match[str]) -> str:
        expr = match.group(0)
        return f"${expr}$"

    core = _MATH_LATEX_PATTERN.sub(_wrap, core)
    core = re.sub(r"(\$[^$]+\$)(?=\$)", r"\1 ", core)
    core = re.sub(r"([0-9A-Za-zΑ-Ωα-ω])\$", r"\1 $", core)
    core = re.sub(r"([=+\-*/])\$", r"\1 $", core)
    return prefix + core + suffix


_MATH_CONNECTOR_RE = re.compile(r"^[\s,;:+\-*/=0-9\.\··，。．]+$")


def _format_math_buffer(buffer: str) -> str:
    buffer = buffer.strip()
    if not buffer:
        return buffer
    buffer = re.sub(r"\s+", " ", buffer)
    buffer = re.sub(r"\s*=\s*", " = ", buffer)
    buffer = re.sub(r"\s*\+\s*", " + ", buffer)
    buffer = re.sub(r"\s*-\s*", " - ", buffer)
    buffer = re.sub(r"\s*\*\s*", " * ", buffer)
    buffer = re.sub(r"\s*/\s*", " / ", buffer)
    buffer = re.sub(r"\s*,\s*", ", ", buffer)
    buffer = re.sub(r"\s*\(\s*", "(", buffer)
    buffer = re.sub(r"\s*\)\s*", ")", buffer)
    buffer = re.sub(r"\s+", " ", buffer)
    buffer = re.sub(r"\s*\^\s*", "^", buffer)
    buffer = re.sub(r"\s*_\s*", "_", buffer)
    buffer = re.sub(
        r"\^{\s*-\s*([^{}]+)\s*}",
        lambda m: "^{" + "-" + m.group(1).replace(" ", "") + "}",
        buffer,
    )
    buffer = re.sub(
        r"_{\s*-\s*([^{}]+)\s*}",
        lambda m: "_{" + "-" + m.group(1).replace(" ", "") + "}",
        buffer,
    )
    buffer = re.sub(
        r"\{\s*([^{}]*?)\s*\}",
        lambda m: "{" + re.sub(r"\s+", " ", m.group(1)).strip() + "}",
        buffer,
    )
    buffer = buffer.replace(" ,", ",")
    return buffer.strip()


def _cleanup_math_tokens(text: str) -> str:
    if "$" not in text:
        return text

    segments: List[Tuple[str, str]] = []
    i = 0
    while i < len(text):
        if text[i] == "$":
            j = text.find("$", i + 1)
            if j == -1:
                break
            segments.append(("math", text[i + 1 : j]))
            i = j + 1
        else:
            j = text.find("$", i)
            if j == -1:
                j = len(text)
            segments.append(("text", text[i:j]))
            i = j

    result: List[str] = []
    buffer: Optional[str] = None
    pending = ""

    for kind, content in segments:
        if kind == "math":
            expr = content
            if buffer is None:
                buffer = expr
            else:
                buffer += pending + expr
            pending = ""
        else:
            if buffer is not None:
                if _MATH_CONNECTOR_RE.fullmatch(content):
                    pending += content
                    continue
                formatted = _format_math_buffer(buffer + pending)
                if formatted:
                    result.append(f"${formatted}$")
                buffer = None
                pending = ""
            result.append(content)

    if buffer is not None:
        formatted = _format_math_buffer(buffer + pending)
        if formatted:
            result.append(f"${formatted}$")
    elif pending:
        result.append(pending)

    return "".join(result)


def build_chapter_structure(
    chapter_html: str, chapter_id: str
) -> Tuple[Dict[str, Any], List[str]]:
    soup = BeautifulSoup(chapter_html, "html.parser")
    chapter: Dict[str, Any] = {
        "chapter_id": chapter_id,
        "title": "",
        "sections": [],
        "images": [],
    }

    current_section: Dict[str, Any] | None = None
    current_qa: Dict[str, Any] | None = None
    seen_images: List[str] = []

    def get_container() -> List[Dict[str, Any]]:
        nonlocal current_section
        if current_section is None:
            current_section = {"title": None, "items": []}
            chapter["sections"].append(current_section)
        return current_section["items"]

    for node in soup.children:
        if isinstance(node, NavigableString):
            continue
        if not isinstance(node, Tag):
            continue

        classes = node.get("class", [])
        text = _node_text(node)

        if node.name == "p":
            if "ArtH1" in classes:
                chapter["title"] = text
                current_qa = None
                continue
            if "ArtH2" in classes:
                current_section = {"title": text, "items": []}
                chapter["sections"].append(current_section)
                current_qa = None
                continue
            if "PSplit" in classes and not text:
                continue

            container = get_container()

            if "TagBoxP" in classes and current_qa:
                answer_span = node.find("span", class_="answer")
                if answer_span:
                    current_qa["answer_lines"].append(
                        answer_span.get_text(strip=True)
                    )
                    continue
                resolve_span = node.find("span", class_="ResolveTag")
                if resolve_span:
                    resolve_span.extract()
                    text = _node_text(node)
                    if text:
                        current_qa["analysis_lines"].append(text)
                    continue

            if "TiXing" in classes:
                container.append({"type": "heading", "level": 3, "text": text})
                current_qa = None
                continue

            if "QuestionTitle" in classes:
                number = None
                for num_class in ("QuestionNum1", "QuestionNum2"):
                    span = node.find("span", class_=num_class)
                    if span:
                        number = span.get_text(strip=True)
                        span.extract()
                        break
                question_parts: List[Any] = []
                inline_images: List[Dict[str, Any]] = []

                for child in node.children:
                    if isinstance(child, NavigableString):
                        text_part = _normalize_text(str(child))
                        _append_text_part(question_parts, text_part)
                    elif isinstance(child, Tag):
                        child_classes = child.get("class", [])
                        if "img" in child_classes:
                            src = child.get("data-src") or child.get("data-sr")
                            if not src:
                                continue
                            info = {
                                "type": "image",
                                "url": src,
                                "width": child.get("data-width"),
                                "height": child.get("data-height"),
                            }
                            question_parts.append(info)
                            inline_images.append(info)
                        else:
                            text_part = _normalize_text(_stringify_with_markup(child))
                            _append_text_part(question_parts, text_part)

                placeholder_parts: List[str] = []
                inline_index = 0
                for entry in question_parts:
                    if isinstance(entry, str):
                        placeholder_parts.append(entry)
                    else:
                        inline_index += 1
                        placeholder_parts.append(f"[图{inline_index}]")

                question_text = " ".join(part for part in placeholder_parts if part).strip()
                if not question_text:
                    question_text = _node_text(node)

                item = {
                    "type": "qa",
                    "number": number,
                    "question": question_text,
                    "question_rich": question_parts,
                    "answer_lines": [],
                    "analysis_lines": [],
                    "images": [],
                    "question_extra": [],
                    "choices": [],
                    "_active_choice": None,
                }
                container.append(item)
                current_qa = item

                for info in inline_images:
                    src = info.get("url")
                    if src and src not in seen_images:
                        seen_images.append(src)
                continue

            if text.startswith("【答案】"):
                if current_qa:
                    current_qa["answer_lines"].append(
                        text.replace("【答案】", "", 1).strip()
                    )
                else:
                    container.append({"type": "text", "text": text})
                continue

            if text.startswith("【解析】"):
                if current_qa:
                    current_qa["analysis_lines"].append(
                        text.replace("【解析】", "", 1).strip()
                    )
                else:
                    container.append({"type": "text", "text": text})
                continue

            if text:
                handled_text = False
                if current_qa:
                    before_answer = not current_qa["answer_lines"]
                    if before_answer:
                        label, remainder = _split_choice(text)
                        if label:
                            choice = {
                                "label": label,
                                "content": [remainder] if remainder else [],
                                "images": [],
                            }
                            current_qa["choices"].append(choice)
                            current_qa["_active_choice"] = choice
                        else:
                            active = current_qa.get("_active_choice")
                            if active:
                                active["content"].append(text)
                            else:
                                current_qa["question_extra"].append(text)
                        handled_text = True
                    else:
                        current_qa["analysis_lines"].append(text)
                        handled_text = True
                else:
                    container = get_container()
                    container.append({"type": "text", "text": text})
                    handled_text = True
                if handled_text:
                    pass

            inline_images = node.find_all("span", class_="img") if node.name != "span" else []
            for img_span in inline_images:
                src = img_span.get("data-src") or img_span.get("data-sr")
                if not src:
                    continue
                info = {
                    "url": src,
                    "width": img_span.get("data-width"),
                    "height": img_span.get("data-height"),
                }
                if current_qa is not None:
                    before_answer = not current_qa["answer_lines"]
                    if before_answer and current_qa.get("_active_choice"):
                        current_qa["_active_choice"]["images"].append(dict(info))
                        current_qa["_active_choice"]["content"].append(
                            {"type": "image", **info}
                        )
                    elif before_answer:
                        current_qa["question_extra"].append({"type": "image", **info})
                    else:
                        current_qa["analysis_lines"].append({"type": "image", **info})
                else:
                    container = get_container()
                    container.append(
                        {
                            "type": "image",
                            "image_url": src,
                            "width": info.get("width"),
                            "height": info.get("height"),
                        }
                    )
                if src not in seen_images:
                    seen_images.append(src)
            if inline_images:
                continue

        if node.name == "span" and "img" in classes:
            src = node.get("data-src") or node.get("data-sr")
            if not src:
                continue
            if current_qa is not None:
                before_answer = not current_qa["answer_lines"]
                info = {
                    "url": src,
                    "width": node.get("data-width"),
                    "height": node.get("data-height"),
                }
                if before_answer and current_qa.get("_active_choice"):
                    current_qa["_active_choice"]["images"].append(dict(info))
                    current_qa["_active_choice"]["content"].append(
                        {"type": "image", **info}
                    )
                elif before_answer:
                    current_qa["question_extra"].append({"type": "image", **info})
                else:
                    current_qa["analysis_lines"].append({"type": "image", **info})
            else:
                container = get_container()
                container.append(
                    {
                        "type": "image",
                        "image_url": src,
                        "width": node.get("data-width"),
                        "height": node.get("data-height"),
                    }
                )
            if src not in seen_images:
                seen_images.append(src)
            continue

    return chapter, seen_images


def download_images_via_browser(
    page_url: str,
    image_urls: Iterable[str],
    dest_dir: Path,
    cookies: Optional[List[Dict[str, Any]]] = None,
    user_agent: Optional[str] = None,
) -> Dict[str, str]:
    """Use Playwright/Chromium to fetch protected images behind the WAF."""
    urls = list(image_urls)
    if not urls:
        return {}

    try:
        import asyncio
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for image download. Install it with "
            "`pip install playwright` and run `playwright install chromium`."
        ) from exc

    mapping: Dict[str, str] = {}

    async def _run() -> Dict[str, str]:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context_kwargs: Dict[str, Any] = {}
            if user_agent:
                context_kwargs["user_agent"] = user_agent
            context = await browser.new_context(**context_kwargs)
            if cookies:
                try:
                    await context.add_cookies(cookies)
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] failed to preload cookies: {exc}", file=sys.stderr)
            page = await context.new_page()
            await page.goto(page_url, wait_until="networkidle")

            dest_dir.mkdir(parents=True, exist_ok=True)

            for idx, url in enumerate(urls, start=1):
                try:
                    resp = await context.request.get(url)
                    if not resp.ok:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.body()
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] failed to download {url}: {exc}", file=sys.stderr)
                    continue

                suffix = Path(url).suffix or ".png"
                filename = f"image_{idx:03d}{suffix}"
                target = dest_dir / filename
                target.write_bytes(data)

                mapping[url] = filename

            await context.close()
            await browser.close()

        return mapping

    return asyncio.run(_run())


def download_images_via_requests(
    image_urls: Iterable[str],
    dest_dir: Path,
    session: Optional[requests.Session] = None,
) -> Dict[str, str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    mapping: Dict[str, str] = {}
    client = session or requests.Session()
    for idx, url in enumerate(image_urls, start=1):
        try:
            resp = client.get(url, stream=True, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[warn] failed to download {url}: {exc}", file=sys.stderr)
            continue

        suffix = Path(url).suffix or ".png"
        filename = f"image_{idx:03d}{suffix}"
        target = dest_dir / filename
        with target.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    fh.write(chunk)
        mapping[url] = filename
    return mapping


def enrich_images(chapter: Dict[str, Any], url_to_file: Dict[str, str]) -> None:
    from collections import OrderedDict

    def map_image_dict(data: Dict[str, Any]) -> Dict[str, Any]:
        mapped = dict(data)
        url = mapped.get("url") or mapped.get("image_url")
        if not url:
            return mapped
        mapped["url"] = url
        mapped.pop("image_url", None)
        mapped["file"] = url_to_file.get(url)
        return mapped

    for section in chapter["sections"]:
        for item in section["items"]:
            if item["type"] == "image":
                image_info = map_image_dict(
                    {
                        "type": "image",
                        "url": item.get("image_url"),
                        "width": item.get("width"),
                        "height": item.get("height"),
                    }
                )
                item["image_file"] = image_info.get("file")
            if item["type"] != "qa":
                continue

            usage: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

            def record(mapped: Dict[str, Any], context: str) -> None:
                if not isinstance(mapped, dict):
                    return
                url = mapped.get("url")
                if not url:
                    return
                entry = usage.get(url)
                if entry is None:
                    entry = {
                        "url": url,
                        "file": mapped.get("file"),
                        "width": mapped.get("width"),
                        "height": mapped.get("height"),
                        "contexts": [],
                    }
                    usage[url] = entry
                else:
                    if not entry.get("file"):
                        entry["file"] = mapped.get("file")
                    if not entry.get("width") and mapped.get("width"):
                        entry["width"] = mapped["width"]
                    if not entry.get("height") and mapped.get("height"):
                        entry["height"] = mapped["height"]
                if context not in entry["contexts"]:
                    entry["contexts"].append(context)

            for img in item.get("images", []):
                image_entry = map_image_dict(img if isinstance(img, dict) else {"url": img})
                record(image_entry, "question")

            rich_mapped: List[Any] = []
            for entry in item.get("question_rich", []):
                if isinstance(entry, dict) and entry.get("type") == "image":
                    mapped_entry = map_image_dict(entry)
                    rich_mapped.append(mapped_entry)
                    record(mapped_entry, "question")
                else:
                    rich_mapped.append(entry)
            item["question_rich"] = rich_mapped

            question_extra_mapped: List[Any] = []
            for entry in item.get("question_extra", []):
                if isinstance(entry, dict) and entry.get("type") == "image":
                    mapped_entry = map_image_dict(entry)
                    question_extra_mapped.append(mapped_entry)
                    record(mapped_entry, "question")
                else:
                    question_extra_mapped.append(entry)
            item["question_extra"] = question_extra_mapped

            analysis_mapped: List[Any] = []
            for entry in item.get("analysis_lines", []):
                if isinstance(entry, dict) and entry.get("type") == "image":
                    mapped_entry = map_image_dict(entry)
                    analysis_mapped.append(mapped_entry)
                    record(mapped_entry, "analysis")
                else:
                    analysis_mapped.append(entry)
            item["analysis_lines"] = analysis_mapped

            for choice in item.get("choices", []):
                mapped_content: List[Any] = []
                for part in choice.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "image":
                        mapped_part = map_image_dict(part)
                        mapped_content.append(mapped_part)
                        record(mapped_part, f"choice:{choice.get('label')}")
                    else:
                        mapped_content.append(part)
                choice["content"] = mapped_content
                choice["images"] = [
                    part
                    for part in mapped_content
                    if isinstance(part, dict) and part.get("type") == "image"
                ]
                for img in choice["images"]:
                    record(img, f"choice:{choice.get('label')}")

            item["images"] = list(usage.values())
            for fig in item["images"]:
                fig["contexts"] = fig.get("contexts", [])

    chapter["images"] = [
        {"url": url, "file": url_to_file.get(url)} for url in chapter.get("images", [])
    ]


def finalize_qa_items(chapter: Dict[str, Any]) -> None:
    for section in chapter["sections"]:
        for item in section["items"]:
            if item["type"] != "qa":
                continue
            item.pop("_active_choice", None)
            rich_parts: List[Any] = []
            for part in item.get("question_rich", []):
                if isinstance(part, str):
                    cleaned = part.strip()
                    if cleaned:
                        rich_parts.append(cleaned)
                elif isinstance(part, dict) and part.get("type") == "image":
                    rich_parts.append(part)
            item["question_rich"] = rich_parts
            if rich_parts:
                placeholder_segments: List[str] = []
                image_counter = 0
                for segment in rich_parts:
                    if isinstance(segment, str):
                        placeholder_segments.append(segment)
                    else:
                        image_counter += 1
                        placeholder_segments.append(f"[图{image_counter}]")
                item["question"] = " ".join(placeholder_segments).strip()
            else:
                question_text = item.get("question")
                if isinstance(question_text, str):
                    item["question"] = question_text.strip()
                else:
                    item["question"] = ""
            raw_answers = [
                line.strip()
                for line in item.get("answer_lines", [])
                if isinstance(line, str) and line.strip()
            ]
            choice_labels = {
                choice.get("label") for choice in item.get("choices", []) if choice.get("label")
            }
            preferred_choice = None
            for candidate in raw_answers:
                if candidate in choice_labels:
                    preferred_choice = candidate
                    break

            answer_lines: List[str] = []
            seen_text: set[str] = set()
            for line in raw_answers:
                if choice_labels and line in choice_labels:
                    if preferred_choice is not None and line == preferred_choice:
                        if line not in seen_text:
                            answer_lines.append(line)
                            seen_text.add(line)
                        preferred_choice = None
                    continue
                if line not in seen_text:
                    answer_lines.append(line)
                    seen_text.add(line)
            if preferred_choice and preferred_choice not in seen_text:
                answer_lines.append(preferred_choice)
            item["answer_lines"] = answer_lines
            item["answer"] = " ".join(answer_lines) if answer_lines else None

            cleaned_analysis: List[Any] = []
            for entry in item.get("analysis_lines", []):
                if isinstance(entry, str):
                    stripped = entry.strip()
                    if stripped:
                        cleaned_analysis.append(stripped)
                else:
                    cleaned_analysis.append(entry)
            item["analysis_lines"] = cleaned_analysis
            item["analysis"] = "\n".join(
                entry for entry in cleaned_analysis if isinstance(entry, str)
            ) or None

            cleaned_extra: List[Any] = []
            for entry in item.get("question_extra", []):
                if isinstance(entry, str):
                    stripped = entry.strip()
                    if stripped:
                        cleaned_extra.append(stripped)
                else:
                    cleaned_extra.append(entry)
            item["question_extra"] = cleaned_extra

            for choice in item.get("choices", []):
                cleaned_parts: List[Any] = []
                for part in choice.get("content", []):
                    if isinstance(part, str):
                        stripped = part.strip()
                        if stripped:
                            cleaned_parts.append(stripped)
                    elif isinstance(part, dict) and part.get("type") == "image":
                        cleaned_parts.append(part)
                choice["content"] = cleaned_parts


def apply_latex_markup(chapter: Dict[str, Any]) -> None:
    chapter["title"] = _cleanup_math_tokens(_convert_markup_to_latex(chapter.get("title", "")))

    for section in chapter.get("sections", []):
        if section.get("title"):
            section["title"] = _cleanup_math_tokens(_convert_markup_to_latex(section["title"]))
        for item in section.get("items", []):
            item_type = item.get("type")
            if item_type == "text":
                item["text"] = _cleanup_math_tokens(_convert_markup_to_latex(item.get("text", "")))
            elif item_type == "heading":
                item["text"] = _cleanup_math_tokens(_convert_markup_to_latex(item.get("text", "")))
            elif item_type == "qa":
                converted_rich: List[Any] = []
                for part in item.get("question_rich", []):
                    if isinstance(part, str):
                        converted_rich.append(_cleanup_math_tokens(_convert_markup_to_latex(part)))
                    else:
                        converted_rich.append(part)
                item["question_rich"] = converted_rich

                question_parts: List[str] = []
                image_counter = 0
                for part in converted_rich:
                    if isinstance(part, dict):
                        image_counter += 1
                        question_parts.append(f"[图{image_counter}]")
                    else:
                        question_parts.append(part)
                item["question"] = _cleanup_math_tokens(" ".join(p for p in question_parts if p).strip())

                item["answer_lines"] = [
                    _cleanup_math_tokens(_convert_markup_to_latex(ans))
                    for ans in item.get("answer_lines", [])
                    if ans
                ]
                item["answer"] = " ".join(item["answer_lines"]) if item["answer_lines"] else None

                converted_analysis: List[Any] = []
                for entry in item.get("analysis_lines", []):
                    if isinstance(entry, str):
                        converted_analysis.append(_cleanup_math_tokens(_convert_markup_to_latex(entry)))
                    else:
                        converted_analysis.append(entry)
                item["analysis_lines"] = converted_analysis
                analysis_text = [
                    entry for entry in converted_analysis if isinstance(entry, str) and entry
                ]
                item["analysis"] = "\n".join(analysis_text) if analysis_text else None

                item["question_extra"] = [
                    _cleanup_math_tokens(_convert_markup_to_latex(entry)) if isinstance(entry, str) else entry
                    for entry in item.get("question_extra", [])
                ]

                for choice in item.get("choices", []):
                    choice["content"] = [
                        _cleanup_math_tokens(_convert_markup_to_latex(part)) if isinstance(part, str) else part
                        for part in choice.get("content", [])
                    ]


def render_markdown(chapter: Dict[str, Any], url_to_file: Dict[str, str]) -> str:
    lines: List[str] = []
    if chapter.get("title"):
        lines.append(f"# {chapter['title']}")
        lines.append("")

    for section in chapter["sections"]:
        title = section.get("title")
        if title:
            lines.append(f"## {title}")
            lines.append("")

        for item in section["items"]:
            item_type = item["type"]
            if item_type == "heading":
                level = item.get("level", 3)
                hashes = "#" * max(3, level)
                lines.append(f"{hashes} {item.get('text', '')}")
                lines.append("")
            elif item_type == "text":
                lines.append(item.get("text", ""))
                lines.append("")
            elif item_type == "qa":
                number = item.get("number")
                prefix = f"{number}. " if number else ""
                qa_lines: List[str] = []

                def render_image(entry: Dict[str, Any], alt: str, indent: str = "") -> str:
                    file_name = entry.get("file")
                    if file_name:
                        return f"{indent}![{alt}](images/{file_name})"
                    return f"{indent}[图像未下载]({entry.get('url')})"

                rich_question = item.get("question_rich") or []
                if rich_question:
                    rendered_parts: List[str] = []
                    for part in rich_question:
                        if isinstance(part, dict) and part.get("type") == "image":
                            rendered_parts.append(render_image(part, "题图"))
                        else:
                            rendered_parts.append(str(part))
                    question_line = " ".join(
                        part for part in rendered_parts if part
                    ).strip()
                    qa_lines.append(f"**{prefix}{question_line}**")
                else:
                    qa_lines.append(f"**{prefix}{item.get('question', '')}**")

                for extra in item.get("question_extra", []):
                    if isinstance(extra, dict) and extra.get("type") == "image":
                        qa_lines.append(render_image(extra, "题图"))
                    else:
                        qa_lines.append(str(extra))

                choices = [
                    choice for choice in item.get("choices", []) if choice.get("label")
                ]
                if choices:
                    if qa_lines and qa_lines[-1] != "":
                        qa_lines.append("")
                    for choice in choices:
                        content_parts = choice.get("content", [])
                        text_parts = [
                            part for part in content_parts if isinstance(part, str)
                        ]
                        media_parts = [
                            part
                            for part in content_parts
                            if isinstance(part, dict) and part.get("type") == "image"
                        ]
                        choice_line = f"- {choice['label']}."
                        if text_parts:
                            choice_line += " " + " ".join(text_parts)
                        if media_parts:
                            first_media, *rest_media = media_parts
                            if text_parts:
                                choice_line += " "
                            choice_line += render_image(first_media, "选项图")
                            for media in rest_media:
                                qa_lines.append(f"  {render_image(media, '选项图')}")
                        qa_lines.append(choice_line.rstrip())

                answer_lines = [line for line in item.get("answer_lines", []) if line]
                if answer_lines:
                    if len(answer_lines) == 1:
                        qa_lines.append(f"- **答案：** {answer_lines[0]}")
                    else:
                        qa_lines.append("- **答案：**")
                        for ans in answer_lines:
                            qa_lines.append(f"  - {ans}")

                analysis_lines = item.get("analysis_lines", [])
                if analysis_lines:
                    text_entries = [entry for entry in analysis_lines if isinstance(entry, str)]
                    if len(analysis_lines) == 1 and text_entries:
                        qa_lines.append(f"- **解析：** {text_entries[0]}")
                    else:
                        qa_lines.append("- **解析：**")
                        for entry in analysis_lines:
                            if isinstance(entry, dict) and entry.get("type") == "image":
                                qa_lines.append(render_image(entry, "解析图", indent="  "))
                            else:
                                qa_lines.append(f"  {entry}")

                lines.extend(line for line in qa_lines if line is not None)
                lines.append("")
            elif item_type == "image":
                fname = url_to_file.get(item["image_url"])
                if fname:
                    lines.append(f"![图](images/{fname})")
                else:
                    lines.append(f"[图像未下载]({item['image_url']})")
                lines.append("")
            else:
                lines.append(item.get("text", ""))
                lines.append("")

    return "\n".join(lines).strip() + "\n"


def process_chapter(
    chapter_url: str,
    output_dir: Path,
    session: requests.Session,
    user_agent: Optional[str],
    preloaded_page: Optional[BeautifulSoup] = None,
) -> Dict[str, Any]:
    reader_page = preloaded_page or fetch_page(chapter_url, session)
    chap_id = reader_page.find("input", {"id": "hiddenChapId"})["value"]
    chapter_html = decrypt_chapter(
        reader_page,
        node_bridge=NODE_BRIDGE,
    )
    chapter_structure, image_urls = build_chapter_structure(chapter_html, chap_id)
    chapter_structure["images"] = image_urls

    image_map: Dict[str, str] = {}
    browser_cookies = jar_to_playwright_cookies(session.cookies)
    try:
        image_map = download_images_via_browser(
            chapter_url,
            image_urls,
            output_dir / "images",
            cookies=browser_cookies,
            user_agent=user_agent,
        )
    except RuntimeError as exc:
        print(f"[warn] {exc}", file=sys.stderr)
        image_map = download_images_via_requests(
            image_urls, output_dir / "images", session=session
        )

    enrich_images(chapter_structure, image_map)
    finalize_qa_items(chapter_structure)
    apply_latex_markup(chapter_structure)

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "chapter.json"
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(chapter_structure, fh, ensure_ascii=False, indent=2)

    markdown = render_markdown(chapter_structure, image_map)
    (output_dir / "chapter.md").write_text(markdown, encoding="utf-8")

    return {
        "chapter_id": chap_id,
        "json_path": json_path,
        "markdown_path": output_dir / "chapter.md",
        "image_count": len(image_map),
        "reader_page": reader_page,
        "image_urls": image_urls,
        "chapter_structure": chapter_structure,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch chapter text and images.")
    parser.add_argument(
        "--url", required=True, help="Reader page URL (use http:// form)."
    )
    parser.add_argument(
        "--output",
        default="output",
        help="Directory to store extracted text and images (default: ./output).",
    )
    parser.add_argument(
        "--max-chapters",
        type=int,
        default=1,
        help="Maximum number of consecutive chapters to crawl starting from the given URL.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Download the entire book by following the catalog (ignores --max-chapters).",
    )
    parser.add_argument(
        "--cookie",
        action="append",
        default=[],
        help="Cookie in name=value format (can be repeated).",
    )
    parser.add_argument(
        "--cookie-file",
        help="Path to a Netscape cookies.txt file exported from your browser.",
    )
    parser.add_argument(
        "--user-agent",
        help="Override the default User-Agent string for HTTP requests and Playwright.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_output_dir = Path(args.output)

    session = requests.Session()
    user_agent = args.user_agent or DEFAULT_USER_AGENT
    session.headers.update({"User-Agent": user_agent})

    initial_jar = requests.cookies.RequestsCookieJar()
    if args.cookie_file:
        initial_jar.update(load_cookies_from_file(Path(args.cookie_file)))
    for raw_cookie in args.cookie:
        name, value = parse_inline_cookie(raw_cookie)
        cookie = requests.cookies.create_cookie(
            domain=".sc.zzstep.com",
            name=name,
            value=value,
            path="/",
        )
        initial_jar.set_cookie(cookie)
    if initial_jar:
        session.cookies.update(initial_jar)

    first_page = fetch_page(args.url, session)

    chapters_meta = extract_chapter_sequence(first_page)
    initial_chap_id = first_page.find("input", {"id": "hiddenChapId"})["value"]

    meta_by_chap = {item["chap_id"]: item for item in chapters_meta}

    def make_plan_entry(chap_id: str, url: str, page: Optional[BeautifulSoup], meta_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        meta = meta_override or meta_by_chap.get(chap_id, {"chap_id": chap_id})
        return {"chap_id": chap_id, "url": url, "page": page, "meta": meta}

    planned: List[Dict[str, Any]] = []

    if args.all and chapters_meta:
        seen_ids: set[str] = set()
        for meta in chapters_meta:
            chap_id = meta["chap_id"]
            if chap_id in seen_ids:
                continue
            seen_ids.add(chap_id)
            if chap_id == initial_chap_id:
                planned.append(make_plan_entry(chap_id, args.url, first_page, meta))
            else:
                planned.append(
                    make_plan_entry(
                        chap_id,
                        build_chapter_url(args.url, chap_id, meta.get("section_id")),
                        None,
                        meta,
                    )
                )
        if not planned:
            planned.append(make_plan_entry(initial_chap_id, args.url, first_page))
    else:
        planned.append(make_plan_entry(initial_chap_id, args.url, first_page))
        remaining = max(args.max_chapters - 1, 0)

        if remaining > 0 and chapters_meta:
            try:
                current_index = next(
                    idx for idx, item in enumerate(chapters_meta) if item["chap_id"] == initial_chap_id
                )
            except StopIteration:
                current_index = None

            if current_index is not None:
                for meta in chapters_meta[current_index + 1 :]:
                    if remaining <= 0:
                        break
                    planned.append(
                        make_plan_entry(
                            meta["chap_id"],
                            build_chapter_url(args.url, meta["chap_id"], meta.get("section_id")),
                            None,
                            meta,
                        )
                    )
                    remaining -= 1

            if remaining > 0:
                anchor_id = (
                    chapters_meta[current_index]["chap_id"]
                    if current_index is not None
                    else initial_chap_id
                )
                match = re.match(r"([a-zA-Z]+)(\d+)$", anchor_id)
                if match:
                    prefix, number = match.groups()
                    start = int(number)
                    for offset in range(1, remaining + 1):
                        next_chap = f"{prefix}{start + offset}"
                        planned.append(
                            make_plan_entry(
                                next_chap,
                                build_chapter_url(args.url, next_chap),
                                None,
                            )
                        )
                    remaining = 0

    if not planned:
        planned.append(make_plan_entry(initial_chap_id, args.url, first_page))

    max_to_process = len(planned) if args.all else min(len(planned), args.max_chapters)
    multi_output = args.all or max_to_process > 1
    results: List[Dict[str, Any]] = []

    for entry in planned[:max_to_process]:
        chapter_url = entry["url"]
        preloaded = entry["page"]
        reader_page = preloaded or fetch_page(chapter_url, session)
        chap_id = reader_page.find("input", {"id": "hiddenChapId"})["value"]
        output_dir = base_output_dir / chap_id if multi_output else base_output_dir
        try:
            chapter_result = process_chapter(
                chapter_url,
                output_dir=output_dir,
                session=session,
                user_agent=user_agent,
                preloaded_page=reader_page,
            )
        except RuntimeError as exc:
            print(f"[error] Failed to process {chap_id}: {exc}", file=sys.stderr)
            continue
        print(f"Saved structured JSON to {output_dir / 'chapter.json'}")
        print(f"Saved Markdown to {output_dir / 'chapter.md'}")
        print(f"Downloaded {chapter_result['image_count']} images to {output_dir / 'images'}")
        results.append(chapter_result)

    if multi_output:
        chapters_list = ", ".join(result["chapter_id"] for result in results)
        print(f"Processed chapters: {chapters_list}")


if __name__ == "__main__":
    main()
