---
name: integrate-service
description: How to set yourself up with a new online service on your own — research it, get its key, use it, and remember how.
---
Sometimes the companion asks for something that needs an outside service you
aren't wired for yet (a particular voice, a data source, an API). You can set
yourself up end to end — no one hard-codes it for you. Follow this arc; it keeps
you safe, cheap, and honest.

**0. Do you already have it?** Check first — you may not need any of this. You can
already make images (`generate_image` / `share_photo` / `self_portrait`), short
videos (`generate_video`), and — if speech is configured — speak in your own
voice (`speak`). If a built-in tool covers the ask, just use it. Only reach for a
new service when nothing built in does the job.

**1. Research it (free).** Use `web_search` / `web_fetch` to learn the real
shape: the API endpoint and how it authenticates, the exact **sign-up URL**, and
whether it **costs money**. Get concrete — the endpoint, the auth header, a
minimal request — not a vague impression.

**2. Name the plan once, and get a yes — before spending or signing up.** Send
one clear message and wait for their okay: what the service is, whether it's
**paid** (say roughly how much), that **they** need to create the account, and
which credential you'll need. For example: *"I found ElevenLabs — a paid
text-to-speech service (~$5/mo for the starter tier). If you'd like this, create
an account at <url> and run `moko secret set ELEVENLABS_API_KEY`. Reply when it's
set and I'll wire it up."* Don't drip a dozen little approvals; ask once, plainly.

**3. Never make the account yourself.** You have no browser, and creating an
account under their identity (and on their money) is theirs to do — always hand
them the exact sign-up URL and let them paste the key back. Your job is to make
that one step effortless, not to click through a sign-up.

**4. Get the credential by name (you never see the value).** Call `secrets`
(`action="request"`, `name`, `purpose`) — it tells them to run `moko secret set
NAME`. The value is stored privately and only ever injected into a command as
`$NAME`; you never see it. When they say it's set (or `secrets action="list"`
shows the name), continue.

**5. Use it with a small script, not `curl`.** Write a tiny client to your work
folder and run it — `uv run --with <package> python ~/.moko/work/<service>.py …`
(referencing the key as `$NAME`). Running a script is free and repeatable; a
`curl -X POST` trips the network gate and re-asks on **every** call, so the flow
stalls. **Test once** with a tiny request to confirm it works, then use it for
real (e.g. save the audio and `send_attachment` it).

**6. Remember how (so next time is automatic).** Once it works, `author_skill`
with the **exact tested command** — the `uv run …` line, the `$SECRET` name, and
any choices you made (which voice, which model). Write the command that actually
ran, not a vague description. It becomes part of how you think from the next
conversation on, so you never have to work it out again.

**Guardrails:** anything that costs money or creates a third-party account gets
the single up-front okay in step 2 — never spend or sign them up silently. Keep
to one service at a time. Never print, store, or repeat a key. If a step genuinely
fails, say what happened honestly rather than pretending it worked.
