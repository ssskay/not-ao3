---
author: ssskay
published: 2026-11-10
summary: What happens to a file when you drop it on the shelf, and how to change what the archive says about it.
tags: Documentation, Not Fic
---
# How This Archive Works

This archive is built from a folder of files. Every file becomes a work. The folder it sits in becomes its fandom.

## Dropping files in

Put files in the `shelf` folder. It reads:

- Markdown (`.md`)
- plain text (`.txt`)
- saved web pages (`.html`)
- EPUB books (`.epub`)
- PDFs (`.pdf`)

Folders at the top of the shelf are fandoms. A file in `shelf/Research Papers/` lands in the Research Papers fandom. A file sitting directly in `shelf/` uses the site name from the page if it has one, otherwise Uncategorized Fandom.

Hidden files and anything starting with an underscore are skipped.

## Chapters

Chapters come from the file's own structure:

- Markdown and web pages split at their headings, when there are at least two.
- Text files split at lines like `CHAPTER I` or `IV. The Purple Wig`.
- EPUBs use one chapter per section of the book.
- PDFs use their bookmarks (the outline in the sidebar of a PDF viewer).

A file with none of those is a one-shot.

## Tags

The archive fills in the AO3 fields by itself:

- **Characters** are names that show up again and again, always capitalized.
- **Additional Tags** are words that are common in this work and rare in the rest of the shelf, plus length tags: Drabble (100 words or fewer), Ficlet (under 1,000), One Shot, Long Fic (50,000 and up).
- **Rating** is General Audiences and the warning is Creator Chose Not To Use Archive Warnings, unless you say otherwise.

## Changing what it says

Write a `.meta` file next to any file, with the same name plus `.meta`:

```
title: A Better Title
author: Someone, Someone Else
fandom: Research Papers
rating: Teen
tags: My Own Tag, Another Tag
characters: none
summary: One line about it.
published: 2024-05-01
url: https://example.com/where-it-came-from
complete: no
```

Every line is optional. Markdown and text files can put the same lines at the top between `---` lines instead. A `_folder.meta` file sets defaults for everything in its folder.

## Kudos and Mark for Later

Kudos, Mark for Later and hits are saved in your browser only. Nothing is sent anywhere, and nobody else sees them.
