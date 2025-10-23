# Shengcai Reader Crawler Guide

This document is a beginner-friendly walkthrough of the current Shengcai crawler.  
It explains how we discovered the site’s protections, how the Python/Playwright/BeautifulSoup toolchain is wired together, and what to tweak if you want to extend the extractor.

---

## 1. Environment Setup

1. **Python environment**
   - Activate the project virtual environment:  
     ```bash
     source .venv/bin/activate
     ```
   - Install the Python dependencies:
     ```bash
     pip install requests beautifulsoup4 playwright
     playwright install chromium
     ```

2. **Node.js bridge dependencies**
   - Install the JS packages one time (they are already bundled in `node_modules`, but this shows the command):  
     ```bash
     npm install jsdom@22 jquery crypto-js
     ```

3. **Project layout cheat sheet**
   - `crawler.py` – main Python entry point.
   - `getChapCore.js` – patched copy of the site’s decryption helper.
   - `readercryp.min.js` – original obfuscated helper kept for reference.
   - `sample_output/` – example JSON, Markdown, and downloaded figures.

> Tip: if you ever need to refresh the virtual environment, remove `.venv/` and rerun the pip commands above.

---

## 2. Understanding the Reader Page & Encryption

1. Open any reader URL such as  
   `http://reading.sc.zzstep.com/reader/reader.aspx?id=...&chapId=chap3&pId=chap3_1`.
2. Inspect the page source: the HTML does **not** contain the chapter body.  
   Instead, an inline script defines `getChapCore()` which decrypts a Base64 blob.
3. Inside `getChapCore()`:
   - AES key: `9WRBGBFW27BGPMZB`
   - AES IV: `1N30NVU69CT36D2O`
   - The function expects a chapter id (`chap3`), a host (`apiHost.eshu`), and a “can note” flag.
4. The decrypted output is standard HTML where images are wrapped in `<span class="img" data-src="...">`.

We copied the entire `getChapCore` function into `getChapCore.js`, removing the anti-tamper guard so it can run inside Node.js.

---

## 3. Discovery Process: How We Found the Building Blocks

Beginners often ask “how did you even know which key or API to use?”  
Here is the exact process we followed so you can repeat it on similar sites.

### 3.1 Locate the encrypted payload
1. Open Chrome DevTools (F12) on the reader page.
2. Search the Sources panel for keywords like `chapStr` or `Base64`.
3. You will find an inline script that calls `getChapCore()` and writes the HTML into the page.  
   Setting a breakpoint on that function and refreshing confirmed it ran client-side.

### 3.2 Extract AES parameters
1. With DevTools still open, step into `getChapCore()`.  
   The function lives inside a bundled file (`readercryp.min.js`).
2. Copy the minified script and beautify it (Chrome’s “Pretty print” `{}` button).
3. Search for `CryptoJS.AES.decrypt` – the call includes the key and IV as plain strings.  
   That revealed the constants `9WRBGBFW27BGPMZB` (key) and `1N30NVU69CT36D2O` (IV).

### 3.3 Identify the resource host
1. In the decrypted HTML, every image URL starts with `https://eshu.sc.zzstep.com/`.
2. Back in the original reader page, hidden inputs (`hiddenHostUrl`, `hiddenChapId`) store the same info.  
   Inspecting the DOM under the Network tab confirmed `getChapCore(host, ...)` expects that host string.

### 3.4 Understand the WAF
1. A simple `curl` against an image returned HTTP 405.  
   Looking at the Network tab showed that after the page loads, a request to `/acw_sc/` sets the `acw_sc__v2` cookie.
2. The response headers mention “Yidun” style protection, hinting at a JavaScript challenge.
3. Capturing HAR files proved that once Chromium solved the challenge, all subsequent image requests succeeded.  
   That inspired the Playwright approach where the browser session fetches images for us.

Armed with those facts, we mirrored the site’s own workflow instead of attempting to reverse-engineer the encrypted payload offline.

---

## 4. Reusing Site JavaScript from Python

`crawler.py` embeds a tiny Node.js “bridge”:

```javascript
const {JSDOM} = require('jsdom');
// ... load jquery, crypto-js, readercryp.min.js, and our getChapCore copy
const result = getChapCore(host, "#ffffff", true);
```

Python pipes the necessary data (chapter id, host URL, the site’s `readercryp.min.js` text, and our `getChapCore.js`) into this bridge, which returns the decrypted `chapStr`.  
The decrypted HTML is persisted to `tmp.html` while debugging so you can inspect it directly.

Key takeaways for beginners:
- **JSDOM** simulates the browser DOM in Node.
- **jQuery** is required because the original script references `$`.
- **crypto-js** provides AES decryption identical to what runs in the browser.

---

## 5. Working Around the WAF for Images

Image URLs live on `https://eshu.sc.zzstep.com/.../mobileAES/epub/...`.  
Direct `requests.get` calls often return HTTP 405 because the CDN expects a challenge cookie (`acw_sc__v2`).

### How we solved it

1. Launch headless Chromium via Playwright.
2. Visit the same `reader.aspx` page. The JavaScript challenge runs and sets the required cookies (`acw_tc`, `acw_sc__v2`).
3. Use Playwright’s `context.request.get(url)` with those live cookies to download each figure.
4. Save the bytes to disk as `images/image_###.png`.

Python falls back to plain `requests` if Playwright is unavailable; the code logs warnings instead of crashing so you can still capture text-only chapters.

---

## 6. Parsing & Structuring Content with BeautifulSoup

After decryption we hand the HTML to BeautifulSoup. The parser walks the nodes in order and builds a structured dictionary (`chapter_structure`):

| HTML class        | Parser behaviour                                                   |
|-------------------|--------------------------------------------------------------------|
| `ArtH1` / `ArtH2` | Chapter and section titles.                                        |
| `QuestionTitle`   | Starts a new question (`qa` item). Extracts the number + text.     |
| `TagBoxP` + `AnsTag` | Captures the answer text and deduplicates it.                  |
| `TagBoxP` + `ResolveTag` | Adds解析 paragraphs.                                       |
| Inline text before答案 | Treated as either choice options (`A. ...`) or question extra. |
| `<span class="img">` | Recorded as images with width/height metadata.                 |

### Recent improvements

- **Inline choice media:** Images that appear inside options now stay attached to their choice text.
- **Answer cleanup:** Duplicate answers like “C, B” collapse to the authoritative label automatically.
- **Image bookkeeping:** Each question stores only the figures it actually references, along with context tags (`question`, `choice:B`, `analysis`).
- **Markdown renderer:** Produces clean bullet lists for choices, embeds images in-place, and keeps multi-line 解析 content under a single bullet.
- **Math markup:** `<sub>/<sup>` fragments are normalized into MathJax-ready `$...$` expressions, so exponents like $(2E-A)^{-1}$ render cleanly in Markdown.

This structured data is written to `output/chapter.json`. The Markdown version (`output/chapter.md`) is rendered from the same structure for quick review or publishing.

---

## 7. Running the Crawler

Basic usage:

```bash
./.venv/bin/python crawler.py \
  --url "http://reading.sc.zzstep.com/reader/reader.aspx?id=1003244&From=Ebook&UserName=...&chapId=chap3&pId=chap3_1" \
  --output ./output
```

- To follow the “下一章节” button automatically, add `--max-chapters 2` (or a larger number) and the crawler will move through the nav map in order, saving each chapter under `output/<chapId>/`.
- To capture the entire book in one go, append `--all`. The crawler will read the catalog embedded in `hiddenNcxStr`, queue every chapter id exactly once, and download them in TOC order (ignoring `--max-chapters` if you pass it).

### Full book example

```bash
./.venv/bin/python crawler.py \
  --url "http://reading.sc.zzstep.com/reader/reader.aspx?id=1003244&From=Ebook&UserName=...&chapId=chap1&pId=chap1_1" \
  --output ./output \
  --all
```

- Output is organized under `output/<chapId>/` (JSON, Markdown, images) for each chapter.
- Progress prints include the chapter id so you can tail the log for long books.
- The crawler reuses the first page load to avoid re-solving the captcha for chapter 1, and will fetch subsequent reader pages as needed.

What you get:
- Default run (single chapter): `output/chapter.json`, `output/chapter.md`, and `output/images/`.
- Multi-chapter run: each chapter is saved under `output/<chapId>/` with its own JSON, Markdown, and images directory.

Playwright will open Chromium headlessly; the run finishes once all figures have been saved.  
Check stderr for `[warn]` lines if any image fails to download or if the fallback HTTP path was used.

---

## 8. Extending the Project

- **Caching decrypted HTML:** Save intermediate `chapStr` blobs so reruns can skip the Playwright step when no images changed.
- **Metadata export:** Store question difficulty, source tags (e.g., “2010年真题”), or section numbering separately.
- **Automated tests:** Add lightweight pytest cases to exercise `_split_choice`, `finalize_qa_items`, and the Markdown renderer with sample fixtures.
- **Resumable runs:** Persist a crawl manifest so `--all` can pick up where it left off if the network drops mid-book.
- **Progress reporting:** Surface per-chapter timers and counts (questions, figures) to monitor large-scale exports.

---

## 9. Handy Debugging Script

When you need to compare the raw reader HTML with the structured output, run the helper we use during development:

```bash
./.venv/bin/python scripts/inspect_question.py --question 4 --limit 1
```

- `--question` filters by the visible number in the reader.
- `--index` selects a specific QuestionTitle occurrence if the same number appears multiple times.
- `--html` / `--json` let you point at alternate files (defaults are `tmp.html` and `output/chapter.json`).
- The script prints the prettified HTML, a flattened view with `<sub>/<sup>` markup preserved, and the matching `question_rich`, answers, and analysis from the JSON.

This is the quickest way to inspect inline math, images, or other tricky formatting issues without digging through ad-hoc snippets.

---

## Troubleshooting Checklist

- **Playwright errors:** Re-run `playwright install chromium`. On Linux, ensure system libs like `libnss3` and `libasound2` are present.
- **Node bridge failure:** Confirm `node` is on PATH and `node_modules` contains `jsdom`, `jquery`, and `crypto-js`.
- **Empty output:** Inspect `tmp.html` to verify the decrypted HTML contains content. If not, the AES key/IV or host URL may have changed.
- **Wrong answers:** Check the raw `<span class="answer">` values in the HTML. Update the dedupe heuristic in `finalize_qa_items` if the site introduces new formats.

---

## TODO for Next Session

- Add caching to reuse decrypted HTML and already-downloaded figures between runs.
- Export richer metadata (difficulty, question tags) for downstream analytics.
- Integrate lightweight pytest coverage for choice parsing and Markdown rendering.

Happy crawling!***
