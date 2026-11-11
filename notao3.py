#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Sara Kay (https://sarakay.me)
"""I Can't Believe It's Not AO3 — turn a folder of files into an AO3-style reading archive.

Drop Markdown, text, saved web pages (HTML), EPUB or PDF files into a folder (the "shelf").
This script turns them into a static website that looks like the Archive of Our Own:
works with chapters, fandoms, tags, blurbs, kudos and Mark for Later.

    python3 notao3.py build              # shelf/ (or demo-shelf/) -> site/
    python3 notao3.py serve              # build, open in the browser, rebuild when files change
    python3 notao3.py selftest           # build the demo shelf, check every page, write an audit log

Not affiliated with the Organization for Transformative Works or the Archive of Our Own.
The look comes from AO3's own open-source stylesheet (otwcode/otwarchive, GPL-2.0-or-later).
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import logging
import math
import os
import posixpath
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree as ET

VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
SITE_NAME = "I Can't Believe It's Not AO3"
REPO_URL = "https://github.com/ssskay/not-ao3"
FORMATS = {".md": "Markdown", ".markdown": "Markdown", ".txt": "Text", ".html": "HTML", ".htm": "HTML",
           ".epub": "EPUB", ".pdf": "PDF"}

log = logging.getLogger("notao3")
e = html.escape


def setup_logging(verbose=False, log_dir=None):
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    log_dir = Path(log_dir or ROOT / "logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "notao3.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as ex:  # read-only checkout, CI, etc.
        log.warning("not writing a log file (%s)", ex)


# ---------------------------------------------------------------- metadata files

def parse_meta_text(text):
    """`key: value` lines. Lists are comma-separated. Lines starting with # are comments."""
    meta = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower().replace(" ", "_").replace("-", "_")] = v.strip()
    return meta


def split_front_matter(text):
    m = re.match(r"\A﻿?---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}, text
    return parse_meta_text(m.group(1)), text[m.end():]


LIST_KEYS = {"author", "authors", "fandom", "fandoms", "tags", "extra_tags", "freeforms", "characters",
             "relationships", "warnings", "categories", "category", "warning"}


def as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if x]
    return [x.strip() for x in re.split(r"\s*,\s*", str(v)) if x.strip()]


def split_authors(v):
    out = []
    for a in as_list(v):
        out += [x.strip() for x in re.split(r"\s+(?:and|&)\s+", a) if x.strip()]
    return out


# ---------------------------------------------------------------- html cleaning

ALLOWED = {"p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "em", "strong", "i", "b", "u", "s", "del", "ins",
           "blockquote", "ul", "ol", "li", "a", "img", "pre", "code", "hr", "table", "thead", "tbody", "tr",
           "th", "td", "figure", "figcaption", "sup", "sub", "dl", "dt", "dd", "abbr", "cite", "q", "small",
           "center", "div", "span", "section"}
DROP_SUBTREE = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "button", "svg",
                "iframe", "object", "embed", "select", "textarea", "template", "head", "title", "menu", "dialog"}
VOID = {"br", "img", "hr"}
BLOCK = {"p", "div", "section", "blockquote", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "tr", "dd", "dt",
         "figcaption", "td", "th"}


class Cleaner(HTMLParser):
    """Keeps a small, safe set of tags. Collects plain text too (for word counts and tags)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.text, self.skip = [], [], 0
        self.skip_tag = None
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag == self.skip_tag:
                self.skip += 1
            return
        a = dict(attrs)
        decorative = (a.get("aria-hidden") or "").lower() == "true" or "hidden" in a
        if (tag in DROP_SUBTREE or decorative) and tag not in VOID:
            # menus, scripts, and anything the page itself hides from screen readers (pull quotes, eyebrow labels)
            self.skip, self.skip_tag = 1, tag
            return
        if tag not in ALLOWED or decorative:
            return
        keep = ""
        if tag == "a" and a.get("href"):
            href = a["href"].strip()
            if re.match(r"(?i)^(https?:|mailto:)", href):
                keep = f' href="{e(href)}" rel="nofollow noopener" target="_blank"'
            else:
                return  # in-page or relative links lead nowhere once the text is moved: keep the words, drop the link
        if tag == "img":
            src = (a.get("src") or "").strip()
            if not re.match(r"(?i)^https?://", src):
                return  # local images inside articles/epubs are not copied in v1
            keep = f' src="{e(src)}" alt="{e(a.get("alt") or "")}" loading="lazy"'
        if tag in ("td", "th"):
            for k in ("colspan", "rowspan"):
                if a.get(k, "").isdigit():
                    keep += f' {k}="{a[k]}"'
        if tag in BLOCK:
            self.text.append("\n")
        self.out.append(f"<{tag}{keep}>")
        if tag not in VOID:
            self.stack.append(tag)
        elif tag == "br":
            self.text.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and tag in ALLOWED and not self.skip and self.stack and self.stack[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip_tag:
                self.skip -= 1
                if not self.skip:
                    self.skip_tag = None
            return
        if tag not in ALLOWED or tag in VOID or tag not in self.stack:
            return  # also covers <a> tags that were dropped on the way in
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break
        if tag in BLOCK:
            self.text.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(e(data, quote=False))
        self.text.append(data)

    def result(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        h = "".join(self.out)
        h = re.sub(r"<(div|span|section|p)>\s*</\1>", "", h)
        return h, re.sub(r"[ \t\r\f\v]+", " ", "".join(self.text)).strip()


def clean_html(fragment):
    c = Cleaner()
    c.feed(fragment)
    c.close()
    return c.result()


def text_to_html(text):
    """Plain text -> paragraphs. Wrapped prose lines are joined; verse (short lines) keeps its line breaks."""
    out = []
    for p in re.split(r"\n\s*\n", text):
        lines = [l.strip() for l in p.strip().split("\n") if l.strip()]
        if not lines:
            continue
        if len(lines) >= 2 and max(len(l) for l in lines) < 60:
            out.append("<p>" + "<br>".join(e(l, quote=False) for l in lines) + "</p>")
        else:
            out.append(f"<p>{e(' '.join(lines), quote=False)}</p>")
    return "".join(out)


def html_text(fragment):
    return clean_html(fragment)[1]


# ---------------------------------------------------------------- readers

class Doc:
    """What a reader hands back: metadata it found in the file, plus chapters [(title, html)]."""

    def __init__(self):
        self.meta = {}
        self.chapters = []


TXT_CHAPTER = re.compile(r"^\s*((?:chapter|part|book|act|actus)\s+[\divxlc]+\b.*|(?:actus|scena|scoena|scene)\s+\w+.*|[IVXLC]+\.\s+\S.*|[IVXLC]+\s*)$", re.I)
CAPS_TITLE = re.compile(r"^\s*[A-Z][A-Z0-9 ,.'’!?:;\-]{3,110}$")
SHORT_PREFACE = 80  # words; a shorter bit before the first heading joins chapter 1 instead of being its own chapter


def find_heads(lines, pattern):
    heads = []
    for i, line in enumerate(lines):
        before = i == 0 or not lines[i - 1].strip()
        after = i + 1 >= len(lines) or not lines[i + 1].strip()
        if line.strip() and len(line.strip()) < 112 and before and after and pattern.match(line):
            heads.append(i)
    return heads


def merge_empty_chapters(chapters):
    """A heading with nothing under it ("Songs of Experience") is a section title: fold it into the next chapter's title."""
    out, pending = [], ""
    for t, h in chapters:
        if len(html_text(h).split()) < 5 and t:
            pending = f"{pending}: {t}" if pending else t
            continue
        if pending:
            t = f"{pending}: {t}" if t else pending
            pending = ""
        out.append((t, h))
    if pending and out:
        out[-1] = (out[-1][0], out[-1][1])  # a trailing empty heading is dropped
    return out or chapters


def merge_short_preface(chapters):
    """[("Preface", html), (t1, h1), ...] -> preface folded into chapter 1 when it's only a line or two."""
    if len(chapters) > 2 and chapters[0][0] == "Preface" and len(html_text(chapters[0][1]).split()) < SHORT_PREFACE:
        (_, pre), (t1, h1) = chapters[0], chapters[1]
        return [(t1, pre + h1)] + chapters[2:]
    return chapters


def read_text(path, raw=None):
    doc = Doc()
    text = raw if raw is not None else path.read_text(encoding="utf-8", errors="replace")
    doc.meta, text = split_front_matter(text)
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    heads = find_heads(lines, TXT_CHAPTER)
    # one kind of heading per book: "CHAPTER 89" books often have "I. A Fast-Fish..." lists inside chapters
    kind = lambda i: ("word" if re.match(r"\s*(chapter|part|book)\b", lines[i], re.I) else
                      "play" if re.match(r"\s*(act|actus|scena|scoena|scene)\b", lines[i], re.I) else "roman")
    if heads:
        main = Counter(kind(i) for i in heads).most_common(1)[0][0]
        heads = [i for i in heads if kind(i) == main]
    if len(heads) < 2:
        caps = find_heads(lines, CAPS_TITLE)   # stories or poems titled in capitals
        if len(caps) >= 3:
            heads = caps
    if len(heads) >= 2:
        if heads[0] > 0 and "\n".join(lines[:heads[0]]).strip():
            doc.chapters.append(("Preface", text_to_html("\n".join(lines[:heads[0]]))))
        for n, start in enumerate(heads):
            end = heads[n + 1] if n + 1 < len(heads) else len(lines)
            title = lines[start].strip()
            body_lines = lines[start + 1:end]
            # "XII" on its own line followed by a title line in CAPS ("BUSTER BEAR GOES FISHING")
            first = next((j for j, l in enumerate(body_lines) if l.strip()), None)
            if re.fullmatch(r"[IVXLC]+", title) and first is not None:
                cand = body_lines[first].strip()
                if cand.isupper() and len(cand) < 80:
                    title = f"{title}. {cand.title()}"
                    body_lines = body_lines[first + 1:]
            title = tidy_chapter_title(title)
            # "CHAPTER 1" with the real title on its own short line underneath ("Loomings.")
            first = next((j for j, l in enumerate(body_lines) if l.strip()), None)
            if not title and first is not None:
                cand = body_lines[first].strip()
                nxt = body_lines[first + 1].strip() if first + 1 < len(body_lines) else ""
                if len(cand) < 60 and not nxt:
                    title = cand.rstrip(".")
                    body_lines = body_lines[first + 1:]
            if title.isupper():
                title = smart_title(title)
            doc.chapters.append((title, text_to_html("\n".join(body_lines))))
        doc.chapters = merge_empty_chapters(merge_short_preface(doc.chapters))
    else:
        doc.chapters.append(("", text_to_html(text)))
    return doc


SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "with"}


def smart_title(t):
    words = t.lower().split()
    return " ".join(w if (i and w in SMALL_WORDS) else w[:1].upper() + w[1:] for i, w in enumerate(words))


def tidy_chapter_title(t):
    """'CHAPTER IV. The Purple Wig' -> 'The Purple Wig'; a bare 'CHAPTER 4' -> '' (shown as Chapter N)."""
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(?:chapter|part|book)\s+[\divxlc]+\s*[.:\-–—]?\s*", "", t, flags=re.I)
    t = re.sub(r"^[IVXLC]+\.\s+", "", t)
    t = t.rstrip(".") if t.count(".") == 1 else t
    return "" if re.fullmatch(r"[IVXLC\d]+\.?", t or "") else t


def read_markdown(path):
    try:
        import markdown
    except ImportError:
        log.warning("%s: the 'markdown' package isn't installed (pip3 install markdown); reading it as plain text", path.name)
        return read_text(path)
    doc = Doc()
    text = path.read_text(encoding="utf-8", errors="replace")
    doc.meta, body = split_front_matter(text)
    md = lambda s: clean_html(markdown.markdown(s, extensions=["extra", "sane_lists"]))[0]
    in_code = False
    h1, h2 = [], []
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_code = not in_code
        if in_code:
            continue
        if re.match(r"^# \S", line):
            h1.append(i)
        elif re.match(r"^## \S", line):
            h2.append(i)
    level = h1 if len(h1) >= 2 else (h2 if len(h2) >= 2 else [])
    if len(h1) == 1 and (not level or level is h2) and "title" not in doc.meta:
        doc.meta["title"] = lines[h1[0]][2:].strip()
        lines[h1[0]] = ""
    if level:
        pre = "\n".join(lines[:level[0]]).strip()
        if pre:
            doc.chapters.append(("Preface", md(pre)))
        for n, start in enumerate(level):
            end = level[n + 1] if n + 1 < len(level) else len(lines)
            doc.chapters.append((lines[start].lstrip("#").strip(), md("\n".join(lines[start + 1:end]))))
        doc.chapters = merge_short_preface(doc.chapters)
    else:
        doc.chapters.append(("", md("\n".join(lines))))
    return doc


class HeadParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.title, self._in_title = {}, "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"].strip())
        elif tag == "link" and (a.get("rel") or "").lower() == "canonical" and a.get("href"):
            self.meta.setdefault("canonical", a["href"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def pick_main(raw):
    """Return the inner HTML of <article>, else <main>, else <body>, else everything."""
    for tag in ("article", "main", "body"):
        m = re.search(rf"<{tag}\b[^>]*>(.*)</{tag}\s*>", raw, re.S | re.I)
        if m and len(html_text(m.group(1)).split()) > 40:
            return m.group(1)
    return raw


def split_html_chapters(cleaned, min_heads=2):
    for tag in ("h1", "h2"):
        parts = re.split(rf"(<{tag}>.*?</{tag}>)", cleaned, flags=re.S)
        heads = [p for p in parts if p.startswith(f"<{tag}>")]
        if len(heads) >= min_heads:
            out, pre = [], parts[0]
            if html_text(pre).strip():
                out.append(("Preface", pre))
            for i in range(1, len(parts), 2):
                out.append((html_text(parts[i]).strip(), parts[i + 1] if i + 1 < len(parts) else ""))
            # cutting at headings leaves wrapper tags half-open; re-clean each piece so it closes its own tags
            out = [(t, clean_html(h)[0]) for t, h in out]
            return merge_short_preface(out)
    return [("", cleaned)]


def read_html(path):
    doc = Doc()
    raw = path.read_text(encoding="utf-8", errors="replace")
    hp = HeadParser()
    try:
        hp.feed(raw[:200000])
    except Exception as ex:  # malformed head; keep going with what we have
        log.debug("%s: head parse stopped early (%s)", path.name, ex)
    m = hp.meta
    first = lambda *keys: next((m[k] for k in keys if m.get(k)), "")
    doc.meta = {k: v for k, v in {
        "title": first("og:title", "twitter:title") or hp.title.strip(),
        "author": first("author", "article:author", "parsely-author", "dc.creator", "byl"),
        "fandom": first("og:site_name", "application-name"),
        "summary": first("og:description", "description", "twitter:description"),
        "published": first("article:published_time", "date", "dc.date", "datepublished", "pubdate"),
        "url": first("og:url", "canonical"),
        "tags": first("keywords", "news_keywords"),
    }.items() if v}
    if doc.meta.get("author", "").lower().startswith("by "):
        doc.meta["author"] = doc.meta["author"][3:]
    if re.match(r"https?://", doc.meta.get("author", "")):
        doc.meta.pop("author")
    cleaned, _ = clean_html(pick_main(raw))
    # leftover "← Back to ..." navigation from the page (its link is already gone)
    cleaned = re.sub(r"<(span|p|div)>\s*(?:←|&larr;)\s*Back to [^<]{0,60}</\1>", "", cleaned, flags=re.I)
    # a leading <h1> that repeats the title is the article's own headline: drop it
    t = doc.meta.get("title", "")
    mh = re.search(r"<h1>(.*?)</h1>", cleaned[:1500], re.S)
    if mh and t and html_text(mh.group(1)).strip().lower() in t.lower():
        cleaned = cleaned[mh.end():]
    doc.chapters = split_html_chapters(cleaned)
    return doc


def read_epub(path):
    doc = Doc()
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container", "opf": "http://www.idpf.org/2007/opf",
          "dc": "http://purl.org/dc/elements/1.1/"}
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.find(".//c:rootfile", ns).get("full-path")
        opf = ET.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)
        md = opf.find("opf:metadata", ns)
        dc = lambda k: [x.text.strip() for x in md.findall(f"dc:{k}", ns) if x is not None and x.text and x.text.strip()]
        doc.meta = {k: v for k, v in {
            "title": (dc("title") or [""])[0], "author": ", ".join(dc("creator")),
            "summary": html_text((dc("description") or [""])[0]), "published": (dc("date") or [""])[0],
            "language": (dc("language") or [""])[0], "tags": ", ".join(dc("subject")),
        }.items() if v}
        items = {i.get("id"): i for i in opf.find("opf:manifest", ns)}
        for ref in opf.find("opf:spine", ns):
            item = items.get(ref.get("idref"))
            if item is None or "html" not in (item.get("media-type") or ""):
                continue
            if "nav" in (item.get("properties") or ""):
                continue
            name = posixpath.normpath(posixpath.join(base, unquote(item.get("href"))))
            try:
                raw = z.read(name).decode("utf-8", errors="replace")
            except KeyError:
                log.warning("%s: spine item %s is missing", path.name, name)
                continue
            body = re.search(r"<body\b[^>]*>(.*)</body\s*>", raw, re.S | re.I)
            cleaned, text = clean_html(body.group(1) if body else raw)
            if len(text.split()) < 30:
                continue  # cover pages, title pages, empty separators
            title = ""
            mh = re.search(r"<(h[1-3])>(.*?)</\1>", cleaned, re.S)
            if mh and mh.start() < 200:
                title = html_text(mh.group(2)).strip()
                cleaned = cleaned[:mh.start()] + cleaned[mh.end():]
            doc.chapters.append((title, cleaned))
    if not doc.chapters:
        doc.chapters.append(("", ""))
    return doc


PDF_SECTION = re.compile(r"^(?:\d{1,2}\.?\s+[A-Z][^.]{2,90}|(?:Abstract|References|Bibliography|Conclusions?|Discussion"
                         r"|Acknowledge?ments|Appendix(?: [A-Z])?)\s*:?)$")
PDF_SUBSECTION = re.compile(r"^\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\.?\s+[A-Z][^.]{2,90}$")


def strip_running_lines(pages):
    """Drop running headers/footers: lines (numbers ignored) that repeat on many pages, plus bare page numbers."""
    norm = lambda l: re.sub(r"[\d\s.…]+", " ", l).strip().lower()
    counts = Counter()
    for p in pages:
        counts.update({norm(l) for l in p.split("\n") if norm(l)})
    limit = max(3, int(len(pages) * 0.4))
    repeated = {k for k, c in counts.items() if c >= limit and len(k) > 3} if len(pages) >= 4 else set()
    out = []
    for p in pages:
        keep = [l for l in p.split("\n") if norm(l) not in repeated and not re.fullmatch(r"\s*\d{1,4}\s*", l)]
        out.append("\n".join(keep))
    if repeated:
        log.debug("pdf: dropped %d running header/footer lines", len(repeated))
    return out


def pdf_paragraphs(text):
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    lines = [l.rstrip() for l in text.split("\n")]
    width = max((len(l) for l in lines if l.strip()), default=80)
    blocks, cur = [], []

    def flush():
        if cur:
            blocks.append(("p", re.sub(r"\s+", " ", " ".join(cur)).strip()))
            cur.clear()
    for l in lines:
        st = l.strip()
        if not st:
            flush()
            continue
        if PDF_SUBSECTION.match(st):
            flush()
            blocks.append(("h3", st))
            continue
        cur.append(st)
        if re.search(r"[.!?:\"'”’)]$", l) and len(l) < width * 0.75:
            flush()
    flush()
    return "".join(f"<{t}>{e(x, quote=False)}</{t}>" for t, x in blocks if x)


def split_pdf_sections(text):
    """No bookmarks: split at numbered section headings ("2. Literature Review") and References etc."""
    lines = text.split("\n")
    heads, last = [], 0
    for i, l in enumerate(lines):
        st = l.strip()
        if not PDF_SECTION.match(st) or len(st) > 70 or st.endswith(("—", "-", ",")):
            continue
        m = re.match(r"(\d{1,2})\.?\s", st)
        if m:  # numbered headings must count up 1, 2, 3 ... (skips footnotes, list items and table rows)
            if int(m.group(1)) != last + 1:
                continue
            last = int(m.group(1))
        heads.append(i)
    if len(heads) < 3:
        return None
    out = []
    if "\n".join(lines[:heads[0]]).strip():
        out.append(("Front Matter", "\n".join(lines[:heads[0]])))
    for n, i in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        title = re.sub(r"^\d{1,2}\.?\s+", "", lines[i].strip()).rstrip(":")
        out.append((title, "\n".join(lines[i + 1:end])))
    return out


def read_pdf(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("the 'pypdf' package isn't installed (pip3 install pypdf)")
    doc = Doc()
    r = PdfReader(str(path))
    info = r.metadata or {}
    g = lambda k: str(info.get(k) or "").strip()
    created = ""
    try:
        if r.metadata and r.metadata.creation_date:
            created = r.metadata.creation_date.strftime("%Y-%m-%d")
    except Exception:
        pass
    doc.meta = {k: v for k, v in {"title": g("/Title"), "author": g("/Author"), "summary": g("/Subject"),
                                  "tags": g("/Keywords"), "published": created}.items() if v}
    pages = strip_running_lines([(p.extract_text() or "") for p in r.pages])
    marks = []
    try:
        for item in r.outline:
            if isinstance(item, list):
                continue
            marks.append((item.title, r.get_destination_page_number(item)))
    except Exception as ex:
        log.debug("%s: no usable outline (%s)", path.name, ex)
    marks = sorted({(n, t) for t, n in marks})
    doc.from_outline = len(marks) >= 2
    if len(marks) >= 2:
        if marks[0][0] > 0:
            doc.chapters.append(("Front Matter", pdf_paragraphs("\n\n".join(pages[:marks[0][0]]))))
        for i, (start, title) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(pages)
            body = "\n\n".join(pages[start:max(end, start + 1)])
            # the heading usually repeats as the first line of the page
            body = re.sub(rf"^\s*{re.escape(title)}\s*\n", "", body, count=1)
            doc.chapters.append((title.strip(), pdf_paragraphs(body)))
    else:
        sections = split_pdf_sections("\n\n".join(pages))
        if sections:
            doc.chapters = [(t, pdf_paragraphs(body)) for t, body in sections]
        else:
            doc.chapters.append(("", pdf_paragraphs("\n\n".join(pages))))
    return doc


READERS = {"Markdown": read_markdown, "Text": read_text, "HTML": read_html, "EPUB": read_epub, "PDF": read_pdf}


# ---------------------------------------------------------------- clean copies (convert once, keep edits)

CLEAN_DIR = ".clean"
CLEANED_FORMATS = {"HTML", "PDF"}   # the messy ones; Markdown, text and EPUB are read directly
CFG = {}                            # the shelf's _archive.meta, set by build()
CHAPTER_MARK = re.compile(r"^<!-- chapter: (.*?) -->\s*$", re.M)
DOCLING_SCRIPT = ("import sys\nfrom docling.document_converter import DocumentConverter\n"
                  "print(DocumentConverter().convert(sys.argv[1]).document.export_to_markdown())")


def md_to_html(md):
    try:
        import markdown
        return clean_html(markdown.markdown(md, extensions=["extra", "sane_lists"]))[0]
    except ImportError:
        return text_to_html(md)


def split_md_chapters(md, numbered_only=False):
    """Markdown -> [(title, markdown)]. Splits at '#' headings (used twice or more), else at '##'.
    numbered_only: for converted PDFs, split only at numbered sections ("## 2. Methods") and named ones
    (Abstract, References...), and keep every other heading inside its chapter."""
    lines, in_code, heads = md.split("\n"), False, {1: [], 2: []}
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,2}) (\S.*)$", line)
        if m:
            heads[len(m.group(1))].append(i)
    level = heads[1] if len(heads[1]) >= 2 else heads[2]
    if numbered_only:
        def top(i):
            t = re.sub(r"^#+\s*", "", lines[i]).strip()
            return PDF_SECTION.match(t) and not PDF_SUBSECTION.match(t)
        picked = [i for i in level if top(i)]
        if len(picked) >= 3:
            for i in level:   # the other headings become sub-headings inside their chapter
                if i not in picked:
                    lines[i] = "###" + lines[i].lstrip("#")
            level = picked
    if len(level) < 2:
        return [("", md)]
    out = []
    pre = "\n".join(lines[:level[0]]).strip()
    if pre:
        out.append(("Preface", pre))
    for n, start in enumerate(level):
        end = level[n + 1] if n + 1 < len(level) else len(lines)
        title = re.sub(r"^#+\s*", "", lines[start]).strip()
        title = re.sub(r"^\d{1,2}\.?\s+(?=[A-Z])", "", title).rstrip(":")
        out.append((title, "\n".join(lines[start + 1:end]).strip()))
    if len(out) > 2 and out[0][0] == "Preface" and len(out[0][1].split()) < SHORT_PREFACE:
        out = [(out[1][0], out[0][1] + "\n\n" + out[1][1])] + out[2:]
    return out


def rejoin_split_words(text):
    """'Healt h' -> 'Health' when the joined word appears elsewhere in the same text (PDF kerning artifacts)."""
    counts = Counter(w.lower() for w in re.findall(r"[A-Za-z]+", text))
    known = COMMON | STOP

    def fix(m):
        a, b = m.group(1), m.group(2)
        j = (a + b).lower()
        if counts[j] >= 3 or (counts[j] >= 1 and (a.lower() not in known or b.lower() not in known) and
                              not (a.lower() in known and b.lower() in known)):
            return a
        return m.group(0)
    text = re.sub(r"\b([A-Za-z]{2,}) (?=([a-z]{1,12})\b)", fix, text)
    return re.sub(r"\b([b-zB-HJ-Z]) (?=([a-z]{2,12})\b)", fix, text)


def drop_repeated_lines(md):
    """Running headers that survive conversion (a journal's logo text on every page): short lines repeated 4+ times."""
    lines = md.split("\n")
    short = Counter(l.strip() for l in lines if l.strip() and len(l.split()) <= 4 and not l.lstrip().startswith(("|", "#", "-", "*", "<")))
    repeated = {l for l, n in short.items() if n >= 4 and not re.fullmatch(r"\d+\.?", l)}
    if repeated:
        log.debug("dropped repeated lines: %s", ", ".join(sorted(repeated))[:200])
    return "\n".join(l for l in lines if l.strip() not in repeated)


def tidy_markdown(md, title=""):
    md = re.sub(r"(?m)^\s*\[?\s*(?:←|&larr;)\s*Back to [^\n]{0,80}$\n?", "", md)          # "← Back to ..." links
    md = re.sub(r"\[([^\]]+)\]\((?!https?:|mailto:|#)[^)]*\)", r"\1", md)                # relative links lead nowhere here
    md = re.sub(r"(?m)^\s*<!-- image -->\s*$\n?", "", md)                                 # docling image placeholders
    m = re.match(r"\s*#{1,2} (.+)\n", md)
    if m and title and m.group(1).strip().lower() in title.lower():
        md = md[m.end():]                                                                 # headline repeating the title
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def convert_html(path):
    """Saved web page -> (meta, [(title, markdown)], converter). Trafilatura when installed, else the built-in reader."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    head = read_html(path)          # head metadata (and the fallback body)
    meta = dict(head.meta)
    try:
        import trafilatura
        md = trafilatura.extract(raw, output_format="markdown", include_formatting=True, include_links=True,
                                 include_tables=True, include_images=False, include_comments=False, favor_recall=True)
        if md and len(md.split()) >= 40:
            tm = trafilatura.extract_metadata(raw)
            for k, v in (("title", tm.title), ("author", tm.author), ("fandom", tm.sitename),
                         ("summary", tm.description), ("url", tm.url)):
                if v and not meta.get(k):
                    meta[k] = v
            if tm.tags and not meta.get("tags"):
                meta["tags"] = ", ".join(tm.tags)
            md = tidy_markdown(md, meta.get("title", ""))
            # Trafilatura is best on cluttered pages; on tidy ones it sometimes drops lines (link rows, small print).
            # Keep the built-in result when it's junk-free and trafilatura missed some of its sentences.
            builtin_text = " ".join(html_text(h) for _, h in head.chapters)
            sents = [x for x in re.split(r"(?<=[.!?])\s+", builtin_text) if len(x.split()) >= 6]
            flat = re.sub(r"\s+", " ", re.sub(r"[*_`#>\[\]]|\]\([^)]*\)", " ", md))
            found = sum(1 for x in sents if re.sub(r"\s+", " ", x)[:40] in flat)
            coverage = found / len(sents) if sents else 1.0
            junk = len(NAV_JUNK.findall(builtin_text))
            if coverage < 0.97 and junk == 0:
                log.info("%s: built-in reader kept more of this page (trafilatura got %.0f%% of it)", path.name, coverage * 100)
                return meta, [(t, raw_html_block(h)) for t, h in head.chapters], "built-in"
            return meta, split_md_chapters(md), f"trafilatura {trafilatura.__version__}"
        log.info("%s: trafilatura found too little text, using the built-in reader", path.name)
    except ImportError:
        log.debug("trafilatura not installed (pip3 install trafilatura); using the built-in HTML reader")
    except Exception as ex:
        log.warning("%s: trafilatura failed (%s); using the built-in reader", path.name, ex)
    return meta, [(t, raw_html_block(h)) for t, h in head.chapters], "built-in"


def raw_html_block(h):
    """Keep already-clean HTML inside a Markdown clean copy (one line, so Markdown passes it through untouched)."""
    return '<div class="nao-raw">' + re.sub(r"\s*\n\s*", " ", h) + "</div>"


def docling_markdown(path):
    try:
        from docling.document_converter import DocumentConverter
        import importlib.metadata as im
        md = DocumentConverter().convert(str(path)).document.export_to_markdown()
        return md, f"docling {im.version('docling')}"
    except ImportError:
        pass
    py = CFG.get("docling_python") or os.environ.get("NOTAO3_DOCLING_PYTHON")
    if not py:
        log.debug("docling not installed; using the built-in PDF reader")
        return None, None
    import subprocess
    log.info("%s: converting with docling via %s (first run downloads its models)", path.name, py)
    r = subprocess.run([py, "-c", DOCLING_SCRIPT, str(path)], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        log.warning("%s: docling failed: %s", path.name, r.stderr.strip().splitlines()[-1:] or r.returncode)
        return None, None
    ver = subprocess.run([py, "-c", "import importlib.metadata as m; print(m.version('docling'))"],
                         capture_output=True, text=True).stdout.strip()
    return r.stdout, f"docling {ver}"


def convert_pdf(path):
    """PDF -> (meta, [(title, markdown)], converter). Docling when available (keeps tables), else the built-in reader."""
    base = read_pdf(path)
    meta = dict(base.meta)
    md, conv = None, None
    if getattr(base, "from_outline", False):
        # the PDF's own bookmarks are the most reliable chapter list (books); docling is for papers and reports
        return meta, [(t, raw_html_block(rejoin_split_words(h))) for t, h in base.chapters], "built-in (bookmarks)"
    try:
        md, conv = docling_markdown(path)
    except Exception as ex:
        log.warning("%s: docling failed (%s); using the built-in reader", path.name, ex)
    if md and len(md.split()) >= 40:
        if not meta.get("title"):
            m = re.search(r"(?m)^#{1,2} (.+)$", md)
            if m and not PDF_SECTION.match(m.group(1)):
                meta["title"] = m.group(1).strip()
        md = rejoin_split_words(drop_repeated_lines(tidy_markdown(md, meta.get("title", ""))))
        return meta, split_md_chapters(md, numbered_only=True), conv
    chapters = [(t, raw_html_block(rejoin_split_words(h))) for t, h in base.chapters]
    return meta, chapters, "built-in"


CONVERTERS = {"HTML": convert_html, "PDF": convert_pdf}
NAV_JUNK = re.compile(r"(?:←|&larr;)\s*back to|skip to (?:main )?content|subscribe (?:to|now)|sign up for|accept (?:all )?cookies"
                      r"|share (?:this|on)|follow us|related (?:articles|posts|stories)|advertisement|all rights reserved"
                      r"|read more|log ?in to", re.I)
META_KEYS = ("title", "author", "fandom", "summary", "published", "url", "tags", "language")


def clean_copy_path(path, shelf):
    return shelf / CLEAN_DIR / (path.relative_to(shelf).as_posix() + ".md")


def doc_from_clean(meta, body):
    doc = Doc()
    doc.meta = {k: v for k, v in meta.items() if not k.startswith("clean_")}
    parts = CHAPTER_MARK.split(body)
    # parts: [before, title1, body1, title2, body2, ...]
    for i in range(1, len(parts), 2):
        doc.chapters.append((parts[i].strip(), md_to_html(parts[i + 1].strip())))
    if not doc.chapters:
        doc.chapters.append(("", md_to_html(body)))
    return doc


def read_cleaned(path, shelf, fmt):
    """Read a messy format through its clean copy in shelf/.clean/: converted once, then reused.
    If you edit the clean copy, your version is used from then on (even if the original changes)."""
    cp = clean_copy_path(path, shelf)
    rel = path.relative_to(shelf).as_posix()
    src_sha = hashlib.sha1(path.read_bytes()).hexdigest()[:16]
    if cp.exists():
        meta, body = split_front_matter(cp.read_text(encoding="utf-8"))
        if meta.get("clean_body_sha") and hashlib.sha1(body.encode()).hexdigest()[:16] != meta["clean_body_sha"]:
            log.info("%s: using your edited clean copy", rel)
            return doc_from_clean(meta, body), cp, True
        if meta.get("clean_source_sha") == src_sha:
            return doc_from_clean(meta, body), cp, False
        log.info("%s: the original changed, making a new clean copy", rel)
    t0 = time.time()
    meta, chapters, conv = CONVERTERS[fmt](path)
    body = (f"<!-- Clean copy of {rel}, made by {conv}. Edit anything below; your edits are kept. "
            f"Lines like 'chapter: ...' start a chapter. -->\n\n"
            + "\n\n".join(f"<!-- chapter: {t} -->\n\n{m.strip()}\n" for t, m in chapters))
    front = {k: re.sub(r"\s+", " ", str(meta[k])).strip() for k in META_KEYS if meta.get(k)}
    front.update(clean_source=rel, clean_converter=conv, clean_source_sha=src_sha,
                 clean_body_sha=hashlib.sha1(body.encode()).hexdigest()[:16])
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in front.items()) + "---\n" + body, encoding="utf-8")
    log.info("%s: clean copy made with %s in %.1fs -> %s", rel, conv, time.time() - t0, cp.relative_to(shelf))
    return doc_from_clean(front, body), cp, False


def read_any(path, shelf, fmt):
    if fmt in CLEANED_FORMATS and not CFG.get("no_clean_copies"):
        return read_cleaned(path, shelf, fmt)
    return READERS[fmt](path), None, False


# ---------------------------------------------------------------- automatic tags

STOP = set("""a about above after again against all almost also although always am among an and another any anybody
anyone anything are around as at away back be became because become been before began being below between both
but by came can cannot could did do does doing done down during each either else enough even ever every
everything few for from further get gets getting go goes going gone got had has have having he her here hers
herself him himself his how however i if in into is it its itself just know like little made make many may me
might mine more most much must my myself never new no nor not nothing now of off often oh on once one only or
other others our ours ourselves out over own perhaps quite rather really said same say says see seemed seems
shall she should since so some something soon still such than that the their theirs them themselves then there
these they thing things think this those though thought through thus till to too toward towards under until
up upon us very was way we well went were what whatever when where whether which while who whom whose why will
with within without would yes yet you your yours yourself yourselves first last next again ever once twice
mr mrs miss sir dr st chapter page part book www http https com org html amp nbsp also let lets made upon
come comes coming came look looked looking looks went gone take took taken took tell told asked replied
cried answered began seemed felt left right great good old young long time times day days man men woman
women people little large small great good went thing things moment moments""".split())
NOT_NAMES = STOP | set("""The A An And But Or If In On At Of To For With From By As It He She They We You I His Her
Their Our Your My This That These Those There Here Then When Where What Who Why How Yes No Oh Well Now So Not
Chapter Page Part Book Mr Mrs Miss Sir Dr Saint God Lord Lady King Queen Monday Tuesday Wednesday Thursday
Friday Saturday Sunday January February March April May June July August September October November December
English French German American British Christian Catholic""".split())
def _load_common():
    f = ROOT / "data" / "common-words.txt"
    try:
        return {l.strip() for l in f.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}
    except OSError:
        log.warning("data/common-words.txt is missing; automatic tags will include everyday words")
        return set()


COMMON = _load_common()
WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*[A-Za-z]|[A-Za-z]")


def guess_characters(text, limit=6):
    """Names: words that are almost always capitalized and not at the start of a sentence."""
    caps, lower, starts = Counter(), Counter(), Counter()
    bigrams = Counter()
    prev_end = True
    prev_cap = None
    for m in re.finditer(r"[A-Za-z][A-Za-z'’]*|[.!?\"“”:;—\n]", text):
        tok = m.group(0)
        if not tok[0].isalpha():
            if tok in ".!?\"“”:\n":
                prev_end = True
            prev_cap = None
            continue
        tok = re.sub(r"['’]s$", "", tok)
        if re.search(r"['’]", tok):
            prev_cap, prev_end = None, False
            continue
        if tok[0].isupper():
            if prev_end:
                starts[tok] += 1
            else:
                caps[tok] += 1
                if prev_cap:
                    bigrams[(prev_cap, tok)] += 1
            prev_cap = tok
        else:
            lower[tok.lower()] += 1
            prev_cap = None
        prev_end = False
    total_words = max(1, sum(caps.values()) + sum(lower.values()))
    floor = max(4, total_words // 6000)
    names = []
    for w, n in caps.most_common(80):
        if n < floor or w in NOT_NAMES or len(w) < 3 or w.isupper():
            continue
        if lower.get(w.lower(), 0) > n * 0.25:
            continue
        names.append((n + starts.get(w, 0), w))
    names.sort(reverse=True)
    picked, used = [], set()
    for n, w in names:
        if w in used:
            continue
        best = None
        for (a, b), c in bigrams.items():
            if (a == w or b == w) and c >= max(3, n * 0.4) and a not in NOT_NAMES - {"Mock", "March", "Father"}:
                if best is None or c > best[1]:
                    best = (f"{a} {b}", c, a, b)
        if best and best[2] not in STOP and best[3] not in STOP:
            picked.append(best[0])
            used |= {best[2], best[3]}
        else:
            picked.append(w)
            used.add(w)
        if len(picked) >= limit:
            break
    return picked


def keyword_counts(text):
    c = Counter()
    for w in WORD.findall(text):
        lw = w.lower().strip("'’-")
        if "-" in lw and any(part in STOP or part in COMMON for part in lw.split("-")):
            continue
        if len(lw) >= 5 and lw not in STOP and lw not in COMMON and not w[0].isupper() and not re.search(r"['’]", lw) and not lw.endswith("ly"):
            c[lw] += 1
    return c


def length_tags(words, n_chapters):
    tags = []
    if words <= 100:
        tags.append("Drabble")
    elif words < 1000:
        tags.append("Ficlet")
    if n_chapters <= 1:
        tags.append("One Shot")
    if words >= 50000:
        tags.append("Long Fic")
    return tags


# ---------------------------------------------------------------- the shelf -> works

RATINGS = {"g": "General Audiences", "general": "General Audiences", "general audiences": "General Audiences",
           "t": "Teen And Up Audiences", "teen": "Teen And Up Audiences", "teen and up audiences": "Teen And Up Audiences",
           "m": "Mature", "mature": "Mature", "e": "Explicit", "explicit": "Explicit", "not rated": "Not Rated"}
LANGS = {"en": "English", "fr": "Français", "de": "Deutsch", "es": "Español", "ja": "日本語", "zh": "中文",
         "ko": "한국어", "it": "Italiano", "pt": "Português", "ru": "Русский"}


def slugify(s, maxlen=60):
    s = re.sub(r"[^\w\s-]", "", s.lower(), flags=re.U)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return (s[:maxlen].rstrip("-")) or "x"


def tag_slug(t):
    return slugify(t, 80) + "-" + hashlib.sha1(t.encode()).hexdigest()[:4]


def nice_date(s):
    s = str(s or "")
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.match(r"^(\d{4})$", s.strip())
    return m.group(1) if m else ""  # year only: shown as just the year


def find_sidecar(path):
    for cand in (path.with_name(path.name + ".meta"), path.with_suffix(".meta")):
        if cand.exists() and cand != path:
            return parse_meta_text(cand.read_text(encoding="utf-8", errors="replace"))
    return {}


def folder_meta(path, shelf):
    meta = {}
    chain = []
    p = path.parent
    while True:
        chain.append(p)
        if p == shelf or shelf not in p.parents:
            break
        p = p.parent
    for d in reversed(chain):
        f = d / "_folder.meta"
        if f.exists():
            meta.update(parse_meta_text(f.read_text(encoding="utf-8", errors="replace")))
    return meta


def scan_shelf(shelf):
    files = []
    for p in sorted(shelf.rglob("*")):
        if p.is_file() and p.suffix.lower() in FORMATS and not any(part.startswith((".", "_")) for part in p.relative_to(shelf).parts):
            files.append(p)
    return files


def load_work(path, shelf):
    fmt = FORMATS[path.suffix.lower()]
    t0 = time.time()
    doc, clean_copy, edited = read_any(path, shelf, fmt)
    meta = {}
    meta.update({k: v for k, v in doc.meta.items() if v})        # what the file says about itself
    file_fandom = meta.pop("fandom", "")                         # a page's site name only counts at the top of the shelf
    meta.update(folder_meta(path, shelf))                        # folder defaults (_folder.meta)
    meta.update(find_sidecar(path))                              # your overrides win
    rel = path.relative_to(shelf).as_posix()
    chapters = [(t, h) for t, h in doc.chapters]
    texts = [html_text(h) for _, h in chapters]
    full_text = "\n\n".join(texts)
    words = len(WORD.findall(full_text))
    top = path.relative_to(shelf).parts[0] if len(path.relative_to(shelf).parts) > 1 else ""
    title = meta.get("title") or path.stem.replace("-", " ").replace("_", " ").strip().title()
    fandoms = (as_list(meta.get("fandoms") or meta.get("fandom")) or ([top] if top else [])
               or ([file_fandom] if file_fandom else ["Uncategorized Fandom"]))
    summary = meta.get("summary") or ""
    if not summary and full_text:
        sents = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", full_text).strip())
        summary = ""
        for s in sents:
            if len((summary + " " + s).split()) > 55:
                break
            summary = (summary + " " + s).strip()
        summary = summary or " ".join(full_text.split()[:45]) + "…"
    published = nice_date(meta.get("published") or meta.get("date")) or \
        dt.date.fromtimestamp(path.stat().st_mtime).isoformat()
    lang = meta.get("language") or "English"
    lang = LANGS.get(lang.lower().split("-")[0], lang) if len(lang) <= 5 else lang
    rating = RATINGS.get((meta.get("rating") or "general").lower(), meta.get("rating") or "General Audiences")
    w = {
        "id": slugify(title, 40) + "-" + hashlib.sha1(rel.encode()).hexdigest()[:6],
        "title": title,
        "authors": split_authors(meta.get("authors") or meta.get("author")) or ["Anonymous"],
        "fandoms": fandoms,
        "rating": rating,
        "warnings": as_list(meta.get("warnings") or meta.get("warning")) or ["Creator Chose Not To Use Archive Warnings"],
        "categories": as_list(meta.get("categories") or meta.get("category")) or ["Gen"],
        "relationships": as_list(meta.get("relationships")),
        "characters": [] if (meta.get("characters") or "").lower() == "none" else
                      (as_list(meta.get("characters")) or guess_characters(full_text)),
        "freeforms": as_list(meta.get("tags") or meta.get("freeforms")),
        "extra_tags": as_list(meta.get("extra_tags")),
        "auto_tags": "tags" not in meta and "freeforms" not in meta,
        "language": lang,
        "summary": summary,
        "published": published,
        "words": words,
        "complete": (meta.get("complete") or "yes").lower() not in ("no", "false", "wip"),
        "url": meta.get("url") or "",
        "format": fmt,
        "source": rel,
        "clean_copy": clean_copy.relative_to(shelf).as_posix() if clean_copy else "",
        "clean_edited": edited,
        "title_from_filename": not meta.get("title"),
        "file": path,
        "chapters": [{"title": t or (title if len(chapters) == 1 else f"Chapter {i + 1}"), "html": h}
                     for i, (t, h) in enumerate(chapters)],
        "_keywords": keyword_counts(full_text),
    }
    log.info("read %-50s %-8s %3d ch %7s words  %4d ms", rel[:50], fmt, len(chapters), f"{words:,}",
             (time.time() - t0) * 1000)
    return w


def add_auto_tags(works):
    """Keyword tags: words that are frequent in this work and rare across the shelf (tf-idf)."""
    df = Counter()
    for w in works:
        df.update(set(w["_keywords"]))
    n = len(works)
    for w in works:
        if w["auto_tags"]:
            names = {c.lower() for c in w["characters"]} | {x.lower() for c in w["characters"] for x in c.split()}
            scored = []
            for k, c in w["_keywords"].items():
                if c < 3 or k in names:
                    continue
                idf = math.log((1 + n) / (1 + df[k])) + 1
                scored.append((c * idf, k))
            scored.sort(reverse=True)
            picked = []
            for _, k in scored:
                if any(k.startswith(p[:5]) or p.startswith(k[:5]) for p in picked):
                    continue  # skip "garden"/"gardens"
                picked.append(k)
                if len(picked) == 4:
                    break
            w["freeforms"] = [" ".join(x[:1].upper() + x[1:] for x in k.replace("-", " ").split()) for k in picked]
            w["freeforms"] += length_tags(w["words"], len(w["chapters"]))
        w["freeforms"] += [t for t in w["extra_tags"] if t not in w["freeforms"]]
        del w["_keywords"]


# ---------------------------------------------------------------- cleanup report

def check_work(w):
    """Things that usually mean an import came out messy. Returns [(chapter number or 0, message)]."""
    issues = []
    chs = w["chapters"]
    texts = [html_text(c["html"]) for c in chs]
    full = "\n".join(texts)
    messy = w["format"] in ("PDF", "HTML")
    if len(chs) == 1 and w["words"] > 15000 and w["format"] != "Markdown":
        issues.append((0, f"One chapter of {w['words']:,} words: no chapter breaks were found."))
    for i, (c, t) in enumerate(zip(chs, texts), 1):
        n = len(t.split())
        if len(chs) > 1 and n < 15:
            issues.append((i, f"Chapter {i} ({c['title'] or 'untitled'}) has only {n} words."))
        if messy and c["title"] in ("Preface", "Front Matter") and n > 1500:
            issues.append((i, f"Chapter {i} is a {n:,}-word '{c['title']}': the first real heading was probably missed."))
    short_lines = "\n".join(l for l in full.split("\n") if len(l.split()) <= 12)
    junk = sorted({m.group(0).strip() for m in NAV_JUNK.finditer(short_lines)}) if messy else []
    if junk:
        issues.append((0, "Leftover page furniture: " + ", ".join(f"“{j}”" for j in junk[:5]) + "."))
    counts = Counter(x.lower() for x in re.findall(r"[A-Za-z]+", full))
    splits = sorted({f"{a} {b}" for a, b in re.findall(r"\b([A-Za-z]{2,}) ([a-z]{1,6})\b", full)
                     if counts[(a + b).lower()] >= 2 and b.lower() not in COMMON | STOP and len(b) <= 3})
    if len(splits) >= 3 and messy:
        issues.append((0, f"{len(splits)} words look split in two, like " + ", ".join(f"“{x}”" for x in splits[:4]) + "."))
    lines = Counter(l.strip() for t in texts for l in t.split("\n") if 3 <= len(l.split()) <= 15)
    repeats = [l for l, k in lines.items() if k >= 4 and not re.match(r"^(chapter|psalm|act|scene)\b", l, re.I)]
    if repeats and w["format"] in ("PDF", "HTML"):
        issues.append((0, f"Line repeated {lines[repeats[0]]} times (a running header?): “{repeats[0][:60]}”."))
    if w.get("title_from_filename") and messy:
        issues.append((0, "No title found in the file, so the file name is used."))
    return issues


def fix_hint(w):
    if w.get("clean_copy"):
        return f"Fix it in <code>{e(w['clean_copy'])}</code> (your edits are kept)."
    return f"Fix it in the file, or add <code>{e(Path(w['source']).name)}.meta</code> next to it."


# ---------------------------------------------------------------- page building

LOGO = ("<svg class=\"nao-logo\" viewBox=\"0 0 48 42\" width=\"48\" height=\"42\" aria-hidden=\"true\">"
        "<ellipse cx=\"24\" cy=\"12\" rx=\"20\" ry=\"6\" fill=\"#f6e27a\" stroke=\"#990000\" stroke-width=\"2\"/>"
        "<path d=\"M4 12 L8 34 Q24 41 40 34 L44 12\" fill=\"#fff6c9\" stroke=\"#990000\" stroke-width=\"2\"/>"
        "<text x=\"24\" y=\"31\" text-anchor=\"middle\" font-family=\"Georgia,serif\" font-size=\"12\" "
        "fill=\"#990000\">not</text></svg>")


class Site:
    def __init__(self, works, out, cfg):
        self.works = sorted(works, key=lambda w: (w["published"], w["title"]), reverse=True)
        self.by_id = {w["id"]: w for w in self.works}
        self.out = out
        self.cfg = cfg
        self.name = cfg.get("site_name") or SITE_NAME
        self.pages = 0
        self.tags = defaultdict(list)       # (kind, tag) -> works
        for w in self.works:
            for kind, key in (("fandom", "fandoms"), ("character", "characters"), ("relationship", "relationships"),
                              ("freeform", "freeforms"), ("rating", None), ("warning", "warnings"),
                              ("category", "categories")):
                vals = [w["rating"]] if kind == "rating" else w[key]
                for t in vals:
                    self.tags[t].append(w)
        self.authors = defaultdict(list)
        for w in self.works:
            for a in w["authors"]:
                self.authors[a].append(w)

    # ---- urls (all relative, so the site works from file://, a subfolder, or GitHub Pages)
    @staticmethod
    def rel(frm, to):
        return posixpath.relpath(to, posixpath.dirname(frm) or ".")

    def work_url(self, w, ch=None):
        return f"works/{w['id']}/index.html" if ch in (None, 1) else f"works/{w['id']}/chapters/{ch}.html"

    def tag_url(self, t):
        return f"tags/{tag_slug(t)}/works.html"

    def user_url(self, a):
        return f"users/{tag_slug(a)}/works.html"

    def write(self, path, title, body, here, main_cls=""):
        r = lambda to: self.rel(here, to)
        nav = (f"<nav aria-label=\"Site\"><ul class=\"primary navigation actions\">"
               f"<li class=\"dropdown\"><a href=\"{r('media/index.html')}\">Fandoms</a></li>"
               f"<li class=\"dropdown\"><a href=\"{r('works/index.html')}\">Browse</a></li>"
               f"<li class=\"dropdown\"><a href=\"{r('me.html')}\">Marked for Later</a></li>"
               f"<li class=\"search\"><form class=\"search\" id=\"search\" role=\"search\" action=\"{r('works/index.html')}\" method=\"get\">"
               "<fieldset><p><label for=\"site_search\" class=\"landmark\">Work Search</label>"
               "<input class=\"text\" id=\"site_search\" name=\"q\" type=\"text\" placeholder=\"Search works\">"
               "<span class=\"submit actions\"><input type=\"submit\" value=\"Search\" class=\"button\"></span></p></fieldset></form></li>"
               "</ul></nav>")
        robots = "<meta name=\"robots\" content=\"noindex, nofollow\">" if self.cfg.get("noindex") else ""
        page = (
            "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
            f"{robots}<title>{e(title)} | {e(self.name)}</title>"
            f"<link rel=\"stylesheet\" href=\"{r('assets/ao3.css')}\"><link rel=\"stylesheet\" href=\"{r('assets/notao3.css')}\">"
            f"<link rel=\"icon\" href=\"{r('assets/favicon.svg')}\"></head>"
            f"<body class=\"logged-out\" data-root=\"{e(r('index.html')[:-len('index.html')] or './')}\">"
            "<div id=\"outer\" class=\"wrapper\"><ul id=\"skiplinks\"><li><a href=\"#main\">Main Content</a></li></ul>"
            f"<header id=\"header\" class=\"region\"><h1 class=\"heading\"><a href=\"{r('index.html')}\">"
            f"<span>{e(self.name)}</span>{LOGO}</a></h1>{nav}<div class=\"clear\"></div></header>"
            f"<div id=\"inner\" class=\"wrapper\"><div id=\"main\" class=\"{main_cls} region\" role=\"main\">{body}"
            "<div class=\"clear\"></div></div></div>"
            "<div id=\"footer\" role=\"contentinfo\" class=\"region\"><ul class=\"navigation actions\" role=\"navigation\">"
            "<li class=\"module group\"><h4 class=\"heading\">About</h4><ul class=\"menu\">"
            f"<li>Built with <a href=\"{REPO_URL}\">{e(SITE_NAME)}</a>, a reader that shows files in an AO3-style layout.</li>"
            "<li>Every work belongs to its author. Work pages link to the original where there is one.</li>"
            f"<li><a href=\"{r('report.html')}\">Import report</a></li></ul></li>"
            "<li class=\"module group\"><h4 class=\"heading\">Credits</h4><ul class=\"menu\">"
            "<li>Not affiliated with the Organization for Transformative Works or the Archive of Our Own.</li>"
            "<li>Stylesheet from <a href=\"https://github.com/otwcode/otwarchive\">otwcode/otwarchive</a> (GPL-2.0-or-later).</li>"
            "</ul></li></ul></div></div>"
            f"<script src=\"{r('assets/notao3.js')}\"></script></body></html>")
        dest = self.out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page, encoding="utf-8")
        self.pages += 1

    # ---- shared pieces
    def tag_link(self, t, here):
        return f"<a class=\"tag\" href=\"{self.rel(here, self.tag_url(t))}\">{e(t)}</a>"

    def author_links(self, w, here):
        return ", ".join(f"<a rel=\"author\" href=\"{self.rel(here, self.user_url(a))}\">{e(a)}</a>" for a in w["authors"])

    @staticmethod
    def rating_cls(r):
        return {"General Audiences": "rating-general-audience", "Teen And Up Audiences": "rating-teen",
                "Mature": "rating-mature", "Explicit": "rating-explicit"}.get(r, "rating-notrated")

    @staticmethod
    def warn_cls(ws):
        j = " ".join(ws).lower()
        return "warning-no" if "no archive warnings" in j else ("warning-choosenotto" if "chose not to" in j else "warning-yes")

    @staticmethod
    def cat_cls(cs):
        m = {"M/M": "category-slash", "F/F": "category-femslash", "F/M": "category-het", "Gen": "category-gen",
             "Multi": "category-multi", "Other": "category-other"}
        return "category-none" if not cs else ("category-multi" if len(cs) > 1 else m.get(cs[0], "category-other"))

    def symbols(self, w):
        sq = lambda cls, kind, title: (f"<li><span class=\"{cls} {kind}\" title=\"{e(title)}\">"
                                       f"<span class=\"text\">{e(title)}</span></span></li>")
        done = w["complete"]
        return ("<ul class=\"required-tags\">" + sq(self.rating_cls(w["rating"]), "rating", w["rating"])
                + sq(self.warn_cls(w["warnings"]), "warnings", ", ".join(w["warnings"]))
                + sq(self.cat_cls(w["categories"]), "category", ", ".join(w["categories"]))
                + sq("complete-yes" if done else "complete-no", "iswip", "Complete Work" if done else "Work in Progress")
                + "</ul>")

    @staticmethod
    def chapters_text(w):
        n = len(w["chapters"])
        return f"{n}/{n}" if w["complete"] else f"{n}/?"

    def blurb(self, w, here):
        tags = ("".join(f"<li class=\"warnings\"><strong>{self.tag_link(t, here)}</strong></li>" for t in w["warnings"])
                + "".join(f"<li class=\"relationships\">{self.tag_link(t, here)}</li>" for t in w["relationships"])
                + "".join(f"<li class=\"characters\">{self.tag_link(t, here)}</li>" for t in w["characters"])
                + "".join(f"<li class=\"freeforms\">{self.tag_link(t, here)}</li>" for t in w["freeforms"]))
        search = " ".join([w["title"], *w["authors"], *w["fandoms"], *w["characters"], *w["relationships"],
                           *w["freeforms"], w["summary"], w["format"]]).lower()
        stats = (f"<dt class=\"language\">Language:</dt><dd class=\"language\">{e(w['language'])}</dd>"
                 f"<dt class=\"words\">Words:</dt><dd class=\"words\">{w['words']:,}</dd>"
                 f"<dt class=\"chapters\">Chapters:</dt><dd class=\"chapters\">{self.chapters_text(w)}</dd>"
                 f"<dt class=\"kudos nao-kudos-stat\" hidden>Kudos:</dt><dd class=\"kudos nao-kudos-stat\" hidden>1</dd>"
                 f"<dt class=\"hits nao-hits-stat\" hidden>Hits:</dt><dd class=\"hits nao-hits-stat\" hidden>0</dd>")
        return (f"<li class=\"work blurb group\" role=\"article\" id=\"work_{e(w['id'])}\" data-id=\"{e(w['id'])}\" "
                f"data-search=\"{e(search)}\" data-date=\"{w['published']}\" data-words=\"{w['words']}\" "
                f"data-title=\"{e(w['title'].lower())}\" data-fandoms=\"{e('|'.join(w['fandoms']))}\">"
                "<div class=\"header module\"><h4 class=\"heading\">"
                f"<a href=\"{self.rel(here, self.work_url(w))}\">{e(w['title'])}</a> by {self.author_links(w, here)}"
                f" <span class=\"nao-format\">{e(w['format'])}</span></h4>"
                f"<h5 class=\"fandoms heading\"><span class=\"landmark\">Fandoms:</span> "
                f"{', '.join(self.tag_link(f, here) for f in w['fandoms'])} &nbsp;</h5>"
                f"{self.symbols(w)}<p class=\"datetime\">{self.long_date(w['published'])}</p></div>"
                f"<h6 class=\"landmark heading\">Tags</h6><ul class=\"tags commas\">{tags}</ul>"
                f"<h6 class=\"landmark heading\">Summary</h6><blockquote class=\"userstuff summary\"><p>{e(w['summary'])}</p></blockquote>"
                f"<dl class=\"stats\">{stats}</dl></li>")

    @staticmethod
    def long_date(s):
        if re.fullmatch(r"\d{4}", s or ""):
            return s
        try:
            return dt.date.fromisoformat(s).strftime("%d %b %Y")
        except ValueError:
            return e(s)

    def work_list(self, works, here, heading, extra_head=""):
        n = len(works)
        return (f"<h2 class=\"heading\">{n:,} Work{'s' if n != 1 else ''} {heading}</h2>{extra_head}"
                f"<h3 class=\"landmark heading\">Listing Works</h3><ol class=\"work index group\">"
                + "".join(self.blurb(w, here) for w in works) + "</ol>")

    # ---- pages
    def build_home(self):
        here = "index.html"
        fandoms = sorted(((t, ws) for t, ws in self.tags.items() if any(t in w["fandoms"] for w in ws)),
                         key=lambda x: (-len(x[1]), x[0].lower()))
        flist = "".join(f"<li>{self.tag_link(t, here)} ({len(ws)})</li>" for t, ws in fandoms[:20])
        words = sum(w["words"] for w in self.works)
        intro = self.cfg.get("intro") or ("A reading room for files: articles, papers, notes and books, "
                                          "shown the way the Archive of Our Own shows fanworks.")
        body = (f"<div class=\"splash\"><div class=\"module odd\"><h2 class=\"heading\">{e(self.name)}</h2>"
                f"<p>{e(intro)}</p><p>{len(self.works):,} works in {len(fandoms):,} fandoms, {words:,} words.</p>"
                f"<p class=\"nao-actions\"><a class=\"nao-btn\" href=\"works/index.html\">Browse all works</a> "
                f"<a class=\"nao-btn\" href=\"me.html\">Marked for Later</a></p></div>"
                f"<div class=\"module\"><h3 class=\"heading\">Fandoms</h3><ul class=\"nao-fandoms\">{flist}</ul>"
                f"<p><a href=\"media/index.html\">All fandoms →</a></p></div></div>"
                + self.work_list(self.works[:10], here, "Newest First"))
        self.write(here, "Home", body, here, "splash")

    def build_browse(self):
        here = "works/index.html"
        fandom_opts = "".join(f"<option value=\"{e(t)}\">{e(t)}</option>"
                              for t in sorted({f for w in self.works for f in w["fandoms"]}, key=str.lower))
        filters = ("<form class=\"nao-filters\" onsubmit=\"return false\"><fieldset>"
                   "<p><label for=\"nao-q\">Search within results</label> "
                   "<input id=\"nao-q\" type=\"search\" placeholder=\"title, author, tag, summary\"></p>"
                   "<p><label for=\"nao-fandom\">Fandom</label> <select id=\"nao-fandom\"><option value=\"\">All fandoms</option>"
                   f"{fandom_opts}</select> <label for=\"nao-sort\">Sort by</label> <select id=\"nao-sort\">"
                   "<option value=\"date\">Date Added</option><option value=\"title\">Title</option>"
                   "<option value=\"words\">Word Count</option></select></p></fieldset></form>"
                   "<p class=\"nao-count\" aria-live=\"polite\"></p>")
        self.write(here, "Works", self.work_list(self.works, here, "in the Archive", filters), here, "works-index")

    def build_tags(self):
        for t, ws in self.tags.items():
            here = self.tag_url(t)
            self.write(here, t, self.work_list(ws, here, f"in {e(t)}"), here, "works-index")
        for a, ws in self.authors.items():
            here = self.user_url(a)
            self.write(here, a, self.work_list(ws, here, f"by {e(a)}"), here, "works-index")

    def build_media(self):
        here = "media/index.html"
        fandoms = sorted({f for w in self.works for f in w["fandoms"]}, key=str.lower)
        groups = defaultdict(list)
        for f in fandoms:
            ch = f[0].upper() if f[0].isalpha() else "#"
            groups[ch].append(f)
        body = ("<h2 class=\"heading\">Fandoms</h2><p>Each top-level folder in the shelf is a fandom. "
                "Files at the top level use the site name from the page, or Uncategorized Fandom.</p>"
                + "".join(f"<h3 class=\"heading\">{e(k)}</h3><ul class=\"tags index group\">"
                          + "".join(f"<li>{self.tag_link(f, here)} ({len(self.tags[f])})</li>" for f in v) + "</ul>"
                          for k, v in sorted(groups.items())))
        self.write(here, "Fandoms", body, here, "media-index")

    def build_me(self):
        here = "me.html"
        body = ("<h2 class=\"heading\">Marked for Later</h2>"
                "<p class=\"nao-note\">Kudos, Mark for Later and hits are kept in this browser only. Nothing is sent anywhere.</p>"
                "<ol class=\"work index group\" id=\"nao-later\"></ol><p class=\"nao-empty\" id=\"nao-later-empty\" hidden>"
                "Nothing marked yet. Use Mark for Later on any work.</p>"
                "<h2 class=\"heading\">Works You Left Kudos On</h2><ol class=\"work index group\" id=\"nao-kudos\"></ol>"
                "<p class=\"nao-empty\" id=\"nao-kudos-empty\" hidden>No kudos yet.</p>"
                "<template id=\"nao-blurbs\">" + "".join(self.blurb(w, here) for w in self.works) + "</template>")
        self.write(here, "Marked for Later", body, here, "works-index")

    def meta_box(self, w, here):
        def row(cls, label, items):
            if not items:
                return ""
            return (f"<dt class=\"{cls} tags\">{label}:</dt><dd class=\"{cls} tags\"><ul class=\"commas\">"
                    + "".join(f"<li>{self.tag_link(t, here)}</li>" for t in items) + "</ul></dd>")
        src = (f"<dt class=\"nao-source\">Original:</dt><dd class=\"nao-source\"><a href=\"{e(w['url'])}\" rel=\"noopener\">"
               f"{e(urlparse(w['url']).netloc or w['url'])}</a></dd>" if w["url"] else "")
        return ("<h3 class=\"landmark heading\">Work Header</h3><div class=\"wrapper\"><dl class=\"work meta group\">"
                + row("rating", "Rating", [w["rating"]]) + row("warning", "Archive Warning", w["warnings"])
                + row("category", "Category", w["categories"]) + row("fandom", "Fandom", w["fandoms"])
                + row("relationship", "Relationship", w["relationships"]) + row("character", "Characters", w["characters"])
                + row("freeform", "Additional Tags", w["freeforms"])
                + f"<dt class=\"language\">Language:</dt><dd class=\"language\">{e(w['language'])}</dd>{src}"
                + "<dt class=\"stats\">Stats:</dt><dd class=\"stats\"><dl class=\"stats\">"
                + f"<dt class=\"published\">Published:</dt><dd class=\"published\">{e(w['published'])}</dd>"
                + f"<dt class=\"words\">Words:</dt><dd class=\"words\">{w['words']:,}</dd>"
                + f"<dt class=\"chapters\">Chapters:</dt><dd class=\"chapters\">{self.chapters_text(w)}</dd>"
                + "<dt class=\"kudos\">Kudos:</dt><dd class=\"kudos nao-kudos-count\">0</dd>"
                + "<dt class=\"hits\">Hits:</dt><dd class=\"hits nao-hits-count\">0</dd>"
                + "</dl></dd></dl></div>")

    def build_work(self, w):
        chs = w["chapters"]
        multi = len(chs) > 1
        wid = w["id"]
        dl_dir = f"works/{wid}/download"
        orig_name = w["file"].name
        (self.out / dl_dir).mkdir(parents=True, exist_ok=True)
        if w["format"] != "HTML":  # a saved web page can carry scripts; only the cleaned copy is offered
            shutil.copy2(w["file"], self.out / dl_dir / orig_name)
        self.write_standalone(w, f"{dl_dir}/{slugify(w['title'], 60)}.html")
        pages = [(n, self.work_url(w, n)) for n in range(1, len(chs) + 1)]
        if multi:
            pages.append(("full", f"works/{wid}/full.html"))
        for n, here in pages:
            full = n == "full" or not multi
            r = lambda to: self.rel(here, to)
            cl = lambda k: r(self.work_url(w, k)) + "#workskin"
            nav = []
            if multi and not full:
                nav.append(f"<li class=\"chapter entire\"><a href=\"{r(f'works/{wid}/full.html')}\">Entire Work</a></li>")
                if n > 1:
                    nav.append(f"<li class=\"chapter previous\"><a href=\"{cl(n - 1)}\">← Previous Chapter</a></li>")
                if n < len(chs):
                    nav.append(f"<li class=\"chapter next\"><a href=\"{cl(n + 1)}\">Next Chapter →</a></li>")
                opts = "".join(f"<option value=\"{cl(i + 1)}\"{' selected' if i + 1 == n else ''}>"
                               f"{i + 1}. {e('' if c['title'] == f'Chapter {i + 1}' else c['title']) or f'Chapter {i + 1}'}</option>"
                               for i, c in enumerate(chs))
                nav.append(f"<li class=\"chapter\"><form onsubmit=\"return false\"><p><select aria-label=\"Chapter Index\" "
                           f"class=\"nao-jump\">{opts}</select></p></form></li>")
            elif multi:
                nav.append(f"<li class=\"chapter bychapter\"><a href=\"{cl(1)}\">Chapter by Chapter</a></li>")
            tools = (f"<li><a href=\"#feedback\" class=\"nao-kudos-btn\" data-id=\"{e(wid)}\">Kudos ♥</a></li>"
                     f"<li><a href=\"#\" class=\"nao-later-btn\" data-id=\"{e(wid)}\">Mark for Later</a></li>"
                     f"<li><a href=\"{r(dl_dir + '/' + slugify(w['title'], 60) + '.html')}\" download>Download HTML</a></li>"
                     + (f"<li><a href=\"{r(dl_dir + '/' + quote(orig_name))}\" download>Download {e(w['format'])}</a></li>"
                        if w["format"] != "HTML" else "")
                     + (f"<li><a href=\"{e(w['url'])}\" rel=\"noopener\">Original</a></li>" if w["url"] else ""))
            top_nav = f"<h3 class=\"landmark heading\">Actions</h3><ul class=\"work navigation actions\">{''.join(nav)}{tools}</ul>"
            first = full or n == 1
            preface = (f"<div class=\"preface group\"><h2 class=\"title heading\">{e(w['title'])}</h2>"
                       f"<h3 class=\"byline heading\">{self.author_links(w, here)}</h3>"
                       + (f"<div class=\"summary module\"><h3 class=\"heading\">Summary:</h3><blockquote class=\"userstuff\">"
                          f"<p>{e(w['summary'])}</p></blockquote></div>" if first else "") + "</div>")

            def chapter_html(i):
                c = chs[i]
                label = "" if c["title"] == f"Chapter {i + 1}" else f": {e(c['title'])}"
                head = (f"<div class=\"chapter preface group\"><h3 class=\"title\"><a href=\"{cl(i + 1)}\">Chapter {i + 1}</a>"
                        f"{label}</h3></div>" if multi else "")
                return (f"<div class=\"chapter\" id=\"chapter-{i + 1}\">{head}"
                        f"<div class=\"userstuff module\" role=\"article\"><h3 class=\"landmark heading\">Chapter Text</h3>"
                        f"{c['html']}</div></div>")
            body_chs = "".join(chapter_html(i) for i in range(len(chs))) if full else chapter_html(n - 1)
            bottom = []
            if multi and not full:
                if n > 1:
                    bottom.append(f"<li><a href=\"{cl(n - 1)}\">← Previous Chapter</a></li>")
                if n < len(chs):
                    bottom.append(f"<li><a href=\"{cl(n + 1)}\">Next Chapter →</a></li>")
            bottom.append("<li><a href=\"#main\">↑ Top</a></li>")
            bottom.append(f"<li><a href=\"#\" class=\"nao-kudos-btn\" data-id=\"{e(wid)}\">Kudos ♥</a></li>")
            feedback = (f"<div id=\"feedback\" class=\"feedback\"><h3 class=\"landmark heading\">Actions</h3>"
                        f"<ul class=\"actions\">{''.join(bottom)}</ul>"
                        f"<div id=\"kudos\"><p class=\"kudos nao-kudos-msg\" hidden>You left kudos on this work!</p></div></div>")
            body = (f"<div class=\"work\" data-work=\"{e(wid)}\" data-chapter=\"{n}\">{top_nav}{self.meta_box(w, here)}"
                    f"<div id=\"workskin\"><div id=\"work-skin\" class=\"wrapper\">{preface}"
                    f"<div id=\"chapters\" role=\"article\"><h3 class=\"landmark heading\" id=\"work\">Work Text:</h3>"
                    f"{body_chs}</div></div></div></div>{feedback}")
            title = w["title"] + (f" - Chapter {n}" if multi and not full else "")
            self.write(here, title, body, here, "works-show")

    def write_standalone(self, w, path):
        """One self-contained HTML file per work, like AO3's HTML download."""
        meta = "".join(f"<dt>{k}:</dt><dd>{e(', '.join(v) if isinstance(v, list) else v)}</dd>" for k, v in (
            ("Rating", w["rating"]), ("Archive Warning", w["warnings"]), ("Category", w["categories"]),
            ("Fandom", w["fandoms"]), ("Characters", w["characters"]), ("Additional Tags", w["freeforms"]),
            ("Language", w["language"]), ("Published", w["published"]), ("Words", f"{w['words']:,}")) if v)
        chs = "".join((f"<h2>{e(c['title'])}</h2>" if len(w["chapters"]) > 1 else "") + c["html"] for c in w["chapters"])
        doc = ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
               f"<title>{e(w['title'])}</title><style>body{{font-family:Georgia,serif;max-width:40em;margin:2em auto;"
               "padding:0 1em;line-height:1.5}dt{font-weight:bold;float:left;clear:left;margin-right:.5em}dd{margin:0 0 .2em}"
               "#preface{border-bottom:1px solid #999;padding-bottom:1em;margin-bottom:2em}</style></head><body>"
               f"<div id=\"preface\"><h1>{e(w['title'])}</h1><p>by {e(', '.join(w['authors']))}</p><dl>{meta}</dl>"
               f"<p><b>Summary:</b> {e(w['summary'])}</p></div>{chs}"
               f"<p><small>Saved from {e(self.name)}.</small></p></body></html>")
        (self.out / path).write_text(doc, encoding="utf-8")

    def build_report(self):
        here = "report.html"
        self.issues = {w["id"]: check_work(w) for w in self.works}
        flagged = [w for w in self.works if self.issues[w["id"]]]
        rows = []
        for w in flagged:
            items = "".join(f"<li>{e(msg)}"
                            + (f" <a href=\"{self.rel(here, self.work_url(w, ch))}\">Go to chapter {ch}</a>" if ch else "")
                            + "</li>" for ch, msg in self.issues[w["id"]])
            rows.append(f"<li class=\"nao-report-item\"><h4 class=\"heading\"><a href=\"{self.rel(here, self.work_url(w))}\">"
                        f"{e(w['title'])}</a> <span class=\"nao-format\">{e(w['format'])}</span></h4>"
                        f"<ul class=\"nao-issues\">{items}</ul><p class=\"nao-note\">{fix_hint(w)}</p></li>")
        converters = Counter((w.get("clean_copy") and "clean copy") or w["format"] for w in self.works)
        body = ("<h2 class=\"heading\">Import Report</h2>"
                f"<p>{len(self.works):,} works checked, {len(flagged):,} with something worth a look. "
                "These are guesses from the text, so some flags will be fine as they are.</p>"
                + (f"<ul class=\"nao-report\">{''.join(rows)}</ul>" if rows else "<p>Nothing flagged.</p>")
                + "<p class=\"nao-note\">Web pages and PDFs are converted once into Markdown clean copies in "
                  "<code>shelf/.clean/</code>. Edit a clean copy and your version is used from then on.</p>")
        self.write(here, "Import Report", body, here, "works-index")
        lines = [f"# Import report {dt.datetime.now():%Y-%m-%d %H:%M}", "",
                 f"{len(self.works)} works checked, {len(flagged)} flagged.", ""]
        for w in flagged:
            lines.append(f"- **{w['title']}** ({w['source']})")
            lines += [f"  - {msg}" for _, msg in self.issues[w["id"]]]
        self.report_md = "\n".join(lines) + "\n"
        log.info("import report: %d of %d works flagged (report.html)", len(flagged), len(self.works))

    def build_assets(self):
        dst = self.out / "assets"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(ASSETS, dst)

    def build(self):
        t0 = time.time()
        self.build_assets()
        for w in self.works:
            self.build_work(w)
        self.build_home()
        self.build_browse()
        self.build_tags()
        self.build_media()
        self.build_me()
        self.build_report()
        (self.out / ".nojekyll").write_text("")
        catalog = [{k: v for k, v in w.items() if k not in ("chapters", "file", "extra_tags", "auto_tags")}
                   | {"chapters": [c["title"] for c in w["chapters"]], "url_path": self.work_url(w)} for w in self.works]
        (self.out / "works.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("wrote %d pages for %d works in %.1fs", self.pages, len(self.works), time.time() - t0)


def pick_shelf(arg):
    if arg:
        return Path(arg).expanduser().resolve()
    mine = ROOT / "shelf"
    if mine.exists() and scan_shelf(mine):
        return mine
    return ROOT / "demo-shelf"


def build(shelf, out, strict=False):
    shelf, out = Path(shelf), Path(out)
    if not shelf.exists():
        raise SystemExit(f"shelf folder not found: {shelf}")
    cfg_file = shelf / "_archive.meta"
    cfg = parse_meta_text(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
    cfg["noindex"] = (cfg.get("noindex") or "").lower() in ("yes", "true", "1")
    CFG.clear()
    CFG.update(cfg)
    log.info("shelf: %s", shelf)
    works, failed = [], []
    for p in scan_shelf(shelf):
        try:
            works.append(load_work(p, shelf))
        except Exception as ex:
            failed.append((p, ex))
            log.error("could not read %s: %s", p.relative_to(shelf), ex, exc_info=log.isEnabledFor(logging.DEBUG))
    if strict and failed:
        raise SystemExit(f"{len(failed)} file(s) failed")
    seen = Counter(w["id"] for w in works)
    assert all(v == 1 for v in seen.values()), "duplicate work ids"
    add_auto_tags(works)
    tmp = out.with_name(out.name + ".building")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    site = Site(works, tmp, cfg)
    site.build()
    try:
        audits = ROOT / "audits"
        audits.mkdir(exist_ok=True)
        (audits / f"{dt.datetime.now():%Y-%m-%d-%H%M%S}-import-report.md").write_text(site.report_md, encoding="utf-8")
    except OSError as ex:
        log.warning("couldn't write the import report to audits/ (%s)", ex)
    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)
    return works, failed


# ---------------------------------------------------------------- serve

def shelf_fingerprint(shelf):
    h = hashlib.sha1()
    for p in sorted(shelf.rglob("*")):
        if p.is_file():
            st = p.stat()
            h.update(f"{p}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()


def serve(shelf, out, port, open_browser=True, idle_hours=3.0):
    import http.server
    import socketserver
    import webbrowser
    build(shelf, out)
    last_hit = [time.time()]

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(out), **kw)

        def log_message(self, fmt, *args):
            last_hit[0] = time.time()
            log.debug("http %s", fmt % args)

    def watch():
        fp = shelf_fingerprint(shelf)
        while True:
            time.sleep(5)
            try:
                now = shelf_fingerprint(shelf)
                if now != fp:
                    log.info("shelf changed, rebuilding")
                    build(shelf, out)
                    fp = now
                if idle_hours and time.time() - last_hit[0] > idle_hours * 3600:
                    log.info("no page loads for %.0f hours, stopping", idle_hours)
                    os._exit(0)
            except Exception as ex:
                log.error("rebuild failed: %s", ex)

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as httpd:
        url = f"http://127.0.0.1:{port}/"
        log.info("serving %s (only this computer can reach it). Add files to %s and refresh.", url, shelf)
        threading.Thread(target=watch, daemon=True).start()
        if open_browser:
            webbrowser.open(url)
        httpd.serve_forever()


# ---------------------------------------------------------------- selftest

class LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for k in ("href", "src", "action"):
            if a.get(k):
                self.links.append(a[k])
        if tag == "option" and a.get("value", "").endswith("#workskin"):
            self.links.append(a["value"])


def selftest():
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))
        (log.info if ok else log.error)("%s %s %s", "PASS" if ok else "FAIL", name, detail)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        shelf = td / "shelf"
        shutil.copytree(ROOT / "demo-shelf", shelf)
        # extra fixtures for formats and edge cases the demo shelf doesn't cover
        (shelf / "Notes").mkdir()
        (shelf / "Notes" / "_folder.meta").write_text("author: Folder Default\nrating: Teen\n")
        (shelf / "Notes" / "front-matter.md").write_text(
            "---\ntitle: Front Matter Test\ntags: Testing, Edge Cases\ncomplete: no\n---\n# Ignored H1\n\nShort body with *emphasis*.\n")
        (shelf / "Notes" / "tiny.txt").write_text("Just a tiny note. Nothing else here.\n")
        (shelf / "Notes" / "evil.html").write_text(
            "<html><head><title>Evil Page</title><script>alert(1)</script></head><body><nav>menu menu</nav>"
            "<article><h1>Evil Page</h1><p onclick=\"x()\">Safe text <a href=\"javascript:alert(1)\">link</a> "
            "<img src=\"x.png\" onerror=\"alert(1)\"></p>" + "<p>filler words here for length.</p>" * 10
            + "<script>steal()</script><iframe src=\"https://example.com\"></iframe></article></body></html>")
        (shelf / ".hidden.md").write_text("# should be skipped\n")
        (shelf / "Notes" / "cluttered-news.html").write_text(
            "<html><head><title>Town Opens New Library</title><meta property='og:site_name' content='Example Gazette'></head><body>"
            "<article><h1>Town Opens New Library</h1><div class='share'>Share this</div><div>Share on Facebook</div>"
            + "".join(f"<p>The new library on Main Street opened on Saturday with a reading room, sentence {i} of the story.</p>"
                      for i in range(12))
            + "<div class='promo'>Sign up for our newsletter</div><div>Related articles</div>"
              "<ul><li><a href='/a'>Mayor visits school</a></li><li><a href='/b'>Bakery wins award</a></li></ul>"
              "<div>Advertisement</div></article></body></html>")
        out = td / "site"
        works, failed = build(shelf, out)
        by = {w["title"]: w for w in works}
        check("no files failed to read", not failed, ", ".join(str(p.name) for p, _ in failed))
        check("hidden files skipped", "should be skipped" not in json.dumps([w["title"] for w in works]))
        alice = by.get("Alice's Adventures in Wonderland")
        check("txt: Alice has 12 chapters", alice and len(alice["chapters"]) == 12, alice and str(len(alice["chapters"])))
        check("txt: Alice chapter titles", alice and alice["chapters"][0]["title"] == "Down the Rabbit-Hole",
              alice and alice["chapters"][0]["title"])
        check("guessed characters include Alice", alice and "Alice" in alice["characters"], alice and str(alice["characters"]))
        fb = by.get("The Wisdom of Father Brown")
        check("epub: Father Brown has 12 chapters", fb and len(fb["chapters"]) == 12, fb and str(len(fb["chapters"])))
        check("epub: subjects become tags", fb and "Short stories" in fb["freeforms"], fb and str(fb["freeforms"]))
        bb = by.get("The Adventures of Buster Bear")
        check("pdf: Buster Bear chapters from outline", bb and len(bb["chapters"]) >= 20, bb and str(len(bb["chapters"])))
        check("pdf: author from metadata", bb and bb["authors"] == ["Thornton W. Burgess"], bb and str(bb["authors"]))
        fm = by.get("Front Matter Test")
        check("md: front matter title + tags", fm and fm["freeforms"][:2] == ["Testing", "Edge Cases"], fm and str(fm["freeforms"]))
        check("md: complete: no -> WIP", fm and not fm["complete"])
        check("_folder.meta author + rating", fm and fm["authors"] == ["Folder Default"] and fm["rating"] == "Teen And Up Audiences",
              fm and f"{fm['authors']} {fm['rating']}")
        tiny = by.get("Tiny")
        check("txt: filename becomes title, short work gets Drabble", tiny and "Drabble" in tiny["freeforms"], tiny and str(tiny["freeforms"]))
        evil = by.get("Evil Page")
        ev_html = (out / f"works/{evil['id']}/index.html").read_text() if evil else ""
        body_part = ev_html.split("id=\"chapters\"", 1)[-1].split("id=\"feedback\"", 1)[0]
        check("html: scripts, iframes, handlers, js links removed",
              evil and "<script" not in body_part and "<iframe" not in body_part and "onclick" not in body_part
              and "javascript:" not in body_part and "onerror" not in body_part)
        check("html: nav menu removed", evil and "menu menu" not in body_part)
        guide = by.get("How This Archive Works")
        check("md: guide splits into chapters at ## headings", guide and len(guide["chapters"]) >= 3,
              guide and str(len(guide["chapters"])))
        art = next((w for w in works if w["format"] == "HTML" and w["source"].endswith("field-guide-to-ao3-tags.html")), None)
        check("html: og:site_name becomes the fandom", art and art["fandoms"] == ["Demo Notes"], art and str(art["fandoms"]))

        # every page: relative links only, and they all resolve
        pages = [p for p in out.rglob("*.html") if "download" not in p.parts]
        broken, absolute = [], []
        for p in pages:
            lc = LinkCollector()
            lc.feed(p.read_text(encoding="utf-8"))
            for link in lc.links:
                u = urlparse(link)
                if u.scheme in ("http", "https", "mailto") or link.startswith("#"):
                    continue
                if link.startswith("/"):
                    absolute.append(f"{p.relative_to(out)} -> {link}")
                    continue
                target = (p.parent / unquote(u.path)).resolve() if u.path else p
                if not target.exists():
                    broken.append(f"{p.relative_to(out)} -> {link}")
        check(f"all internal links resolve ({len(pages)} pages)", not broken, "; ".join(broken[:5]))
        check("no root-absolute links (works under a GitHub Pages subpath)", not absolute, "; ".join(absolute[:5]))
        css = (out / "assets/ao3.css").read_text()
        imgs = set(re.findall(r'url\("?([^")]+)"?\)', css))
        missing = [i for i in imgs if i != "none" and not (out / "assets" / i).exists()]
        check("stylesheet images all present", not missing, ", ".join(missing))
        check("no AO3 logo files referenced", "ao3_logos" not in css and "logo.png" not in css)
        clean_dir = shelf / CLEAN_DIR
        check("clean copies made for web pages and PDFs", (clean_dir / "Notes" / "evil.html.md").exists()
              and any(clean_dir.rglob("*.pdf.md")), ", ".join(p.name for p in clean_dir.rglob("*.md")))
        try:
            import trafilatura  # noqa: F401
            news = by.get("Town Opens New Library")
            ntext = " ".join(html_text(c["html"]) for c in news["chapters"]) if news else ""
            check("cluttered page: share buttons, newsletter and related links removed",
                  news and "Sign up" not in ntext and "Related articles" not in ntext and "sentence 11" in ntext, ntext[:80])
        except ImportError:
            log.info("trafilatura not installed: skipping the cluttered-page check")
        report = out / "report.html"
        check("import report page written", report.exists() and "Import Report" in report.read_text())
        # edits to a clean copy are kept, even after the original changes
        cp = clean_dir / "Notes" / "evil.html.md"
        cp.write_text(cp.read_text() + "\nEDITED-BY-SELFTEST\n")
        (shelf / "Notes" / "evil.html").write_text((shelf / "Notes" / "evil.html").read_text() + "<!-- changed -->")
        fg = clean_dir / "field-guide-to-ao3-tags.html.md"
        old_sha = split_front_matter(fg.read_text())[0].get("clean_source_sha")
        (shelf / "field-guide-to-ao3-tags.html").write_text((shelf / "field-guide-to-ao3-tags.html").read_text() + "<!-- changed -->")
        works2, _ = build(shelf, out)
        ev2 = next((w for w in works2 if w["title"] == "Evil Page"), None)
        check("an edited clean copy is used, and kept when the original changes",
              ev2 and "EDITED-BY-SELFTEST" in ev2["chapters"][-1]["html"] and ev2["clean_edited"])
        check("an unedited clean copy is remade when the original changes",
              split_front_matter(fg.read_text())[0].get("clean_source_sha") != old_sha)
        raw_html = [p for p in out.rglob("download/*") if p.name == "evil.html"]
        check("saved web pages are not re-published with their scripts", not raw_html)
        dl = list(out.glob("works/*/download/*.html"))
        check("every work has an HTML download", len(dl) == len(works) and all("<script" not in p.read_text() for p in dl),
              f"{len(dl)} downloads")
        cat = json.loads((out / "works.json").read_text())
        check("works.json lists every work", len(cat) == len(works), str(len(cat)))

    passed = sum(ok for _, ok, _ in checks)
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    audit_dir = ROOT / "audits"
    audit_dir.mkdir(exist_ok=True)
    report = audit_dir / f"{stamp}-selftest.md"
    report.write_text(
        f"# Selftest {stamp}\n\nnotao3 {VERSION}, Python {sys.version.split()[0]}. "
        f"{passed}/{len(checks)} checks passed.\n\n"
        + "\n".join(f"- {'PASS' if ok else 'FAIL'} {name}" + (f" ({d})" if d else "") for name, ok, d in checks) + "\n",
        encoding="utf-8")
    log.info("%d/%d checks passed; audit written to %s", passed, len(checks), report.relative_to(ROOT))
    return passed == len(checks)


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="build the site")
    b.add_argument("--shelf", help="folder of files (default: shelf/, or demo-shelf/ if shelf/ is empty)")
    b.add_argument("--out", default=str(ROOT / "site"))
    b.add_argument("--strict", action="store_true", help="fail if any file can't be read")
    s = sub.add_parser("serve", help="build, open in the browser, rebuild when the shelf changes")
    s.add_argument("--shelf")
    s.add_argument("--out", default=str(ROOT / "site"))
    s.add_argument("--port", type=int, default=8033)
    s.add_argument("--no-open", action="store_true")
    s.add_argument("--idle-hours", type=float, default=3.0, help="stop after this long with no page loads (0 = never)")
    sub.add_parser("selftest", help="build the demo shelf and check every page")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    log.debug("notao3 %s, python %s", VERSION, sys.version.split()[0])
    if args.cmd == "build":
        build(pick_shelf(args.shelf), Path(args.out), args.strict)
    elif args.cmd == "serve":
        serve(pick_shelf(args.shelf), Path(args.out), args.port, not args.no_open, args.idle_hours)
    elif args.cmd == "selftest":
        sys.exit(0 if selftest() else 1)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
