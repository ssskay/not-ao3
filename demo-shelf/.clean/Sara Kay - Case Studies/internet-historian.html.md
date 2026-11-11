---
title: Internet Historian — A Patient Wayback Machine Archiver
author: Sara Kay
fandom: Sara Kay
summary: Web pages die: 38% of the 2013 web is already gone. This archiver treats a busy Internet Archive as weather and retries until your pages are preserved.
url: https://sarakay.me/case-studies/internet-historian
tags: Wayback Machine archiver, Internet Archive, link rot, web preservation, Save Page Now, digital decay, Python CLI, Claude Code skill
clean_source: Sara Kay - Case Studies/internet-historian.html
clean_converter: built-in
clean_source_sha: 3204f05088cfa12f
clean_body_sha: 44c563426e36c72d
---
<!-- Clean copy of Sara Kay - Case Studies/internet-historian.html, made by built-in. Edit anything below; your edits are kept. Lines like 'chapter: ...' start a chapter. -->

<!-- chapter: Why you should care -->

<div class="nao-raw"> <p> <span> A patient Wayback Machine archiver • MIT, on PyPI • ships with a Claude Code skill </span> </p> <section> <dl> <div><dt>what it is</dt><dd>a patient wayback machine archiver</dd></div> <div><dt>built with</dt><dd>python, sqlite, a claude code skill</dd></div> <div><dt>status</dt><dd>v0.3.0, mit licensed, on pypi</dd></div> <div><dt>repo</dt><dd><a href="https://github.com/ssskay/internet-historian" rel="nofollow noopener" target="_blank">github.com/ssskay/internet-historian</a></dd></div> <div><dt>shipped</dt><dd>early july 2026</dd></div> </dl> </section> <p> The web forgets faster than people think. Pew Research Center measured it in 2024: <a href="https://www.pewresearch.org/data-labs/2024/05/17/when-online-content-disappears/" rel="nofollow noopener" target="_blank">38% of webpages that existed in 2013 were no longer accessible ten years later</a>, and a quarter of all pages that existed at any point between 2013 and 2023 are already gone. Even the durable-looking stuff rots: 23% of news pages contain at least one broken link. </p> <p> The Internet Archive's Wayback Machine can preserve almost any public page, but only if someone asks it to, at the right time, and keeps asking when the Archive is too busy to answer. That "keep patiently asking" part is tedious for a human and perfect for software. Internet Historian is that software: it does the asking, forever, in the background. </p> </div>


<!-- chapter: The story -->

<div class="nao-raw"> <p> It was built for one purpose: saving ちいかわ (Chiikawa) pages before they vanish. Official sites, the anime, the shops, the fan wikis. Fandom pages are exactly the kind of web the statistics above eat first: personally maintained, commercially fragile, gone the day someone stops paying for hosting. </p> <p> It shipped in early July 2026 and promptly outgrew its fandom. The same patience works for your press mentions, your old blog, the recipe site you cite in your dissertation. I now run a self-preservation collection with my own press links in it, on a 90-day refresh, because the archive should not find out about link rot from me at the worst possible moment. </p> </div>


<!-- chapter: How it works -->

<div class="nao-raw"> <p> You feed it pages three ways: paste URLs, import a browser bookmarks export, or use <code>discover</code>, which takes a subject ("Chiikawa") and finds its official site, Wikipedia article, and real external links with zero API keys. The demo recording in the README is a real run: discover found and queued 63 genuine Chiikawa pages. </p> <p> Everything lands in a local SQLite queue that a background job drains every ten minutes. Pages move from queued to submitted to archived, and a page is only marked <em>dead</em> after three failed confirmations spaced a day apart, because a throttled Archive is not a dead URL. If the Archive is busy, the queue waits and tries again until the capture is confirmed. </p> <p> <img src="https://raw.githubusercontent.com/ssskay/internet-historian/main/docs/demo.gif" alt="Terminal demo: internet-historian discover Chiikawa finds and queues 63 real pages" loading="lazy"> </p> </div>


<!-- chapter: Polite by design (the SRE part) -->

<div class="nao-raw"> <p> The Internet Archive is a nonprofit running shared infrastructure, so the archiver runs on an explicit politeness budget, the same way you would budget error rates for a production service: </p> <ul> <li>It always leaves two of your Save Page Now capture slots free, so a greedy queue never locks you out of archiving something by hand.</li> <li>It caps itself at 5 attempts per page per day, half of what the service allows.</li> <li>When throttled, it backs off exponentially, up to a full day.</li> <li>It asks the server to skip pages archived within the last 30 days instead of burning a capture on a duplicate.</li> </ul> <p> None of these numbers are guesses; they are written into the config and verified against it in the README's diagrams. The tool is built to never lose a URL, even when that makes it slow. </p> </div>


<!-- chapter: The hard line -->

<div class="nao-raw"> <p> Every tool I ship with an AI skill gets a line the AI is not allowed to cross. Internet Historian ships with a Claude Code skill, so you can say "archive my Chiikawa stuff" in plain language and walk away. But it only ever archives what you asked for: no auto-crawling, no wandering off to scrape whole sites, no deleting anything, ever. The queue is append-only. </p> </div>


<!-- chapter: Where it is now -->

<div class="nao-raw"> <p> v0.3.0, MIT licensed, installable with <code>pipx install internet-historian</code>. The test suite runs green on a 9-job CI matrix (Ubuntu, macOS, and Windows across Python 3.10 to 3.12), so cross-platform support is tested: macOS gets the fully automatic background setup, and Linux and Windows get working cron and Task Scheduler recipes out of the box. </p> <p> The README carries five verified explainer diagrams (what it does, setup flow, why not by hand, under the hood, polite by design) if you want the deeper mechanics. </p> <p> <a href="https://github.com/ssskay/internet-historian" rel="nofollow noopener" target="_blank"> <span>View on GitHub</span> </a> </p> </div>


<!-- chapter: Sources -->

<div class="nao-raw"> <ul> <li>Pew Research Center, <a href="https://www.pewresearch.org/data-labs/2024/05/17/when-online-content-disappears/" rel="nofollow noopener" target="_blank">"When Online Content Disappears"</a> (May 17, 2024): the link rot and digital decay numbers above.</li> <li><a href="https://github.com/ssskay/internet-historian" rel="nofollow noopener" target="_blank">The internet-historian README</a>: politeness budget, retry policy, discover demo, CI matrix.</li> <li><a href="https://help.archive.org/help/save-pages-in-the-wayback-machine/" rel="nofollow noopener" target="_blank">Internet Archive: Save Pages in the Wayback Machine</a>: the service it submits pages to.</li> </ul> </div>
