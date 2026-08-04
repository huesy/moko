"""English — the source catalogue. Every other locale is checked against these
keys; a missing key anywhere falls back to the English string here.

Keep values warm and first-person (Moko's voice). ``{name}`` style placeholders
are filled by :func:`moko.i18n.t` and must survive translation unchanged.
"""

from __future__ import annotations

# The Terms screen is a genuine consent gate, not a preference toggle — it states,
# plainly and honestly, how Moko really works: where data goes, how much power it
# has over the computer, and the promises its code actually enforces.
TERMS = """\
Hi — I'm Moko. Before we get to know each other, here's plainly how I work and \
what you're agreeing to. Please read it — it matters.

To think, I use an AI "brain" you choose. Unless it runs on this computer, \
answering you means sending the provider you pick (for example Anthropic, \
OpenAI, or xAI) your message plus the slice of what I remember I need to \
reply well — so your words and relevant pieces of what I've learned leave \
this machine. It's the same when I look something up online (your query \
goes out) or when you send me a picture or voice note and I use a cloud \
model to understand it. My full memory stays here in your home folder and \
is never sent as a whole — but treat anything I know as something that may \
be sent, a piece at a time, when it helps.

I run on your computer with real power. To actually be useful I can run \
programs, read and change your files, and use the internet. That is a lot of \
trust to give to software. A mistake of mine — or a hidden instruction in \
something I read, like a web page, an email, or a file someone sent you — could \
in principle delete data, leak something private, or run something harmful.

So I hold to firm rules. I will never lie to you (I might keep a thought \
private, but I won't tell you something untrue). Reading, searching, and \
thinking I do freely — but anything that acts — messaging someone, spending \
money, changing your files, running a command that changes something — I \
name first and wait for your yes. I won't do those quietly.

Even so, I'm not perfect and I can be fooled. Please check anything that really \
matters, and only give me access you're genuinely comfortable with.

You're always in control. You can change how I behave, limit me, or switch me \
off at any time (`moko config`, `moko service stop`). Accepting means you've \
read and understood this. If you're comfortable, let's begin."""

STRINGS: dict[str, str] = {
    # ── welcome / logo ────────────────────────────────────────────────
    "welcome.tagline": "a small, curious companion who lives here with you",
    "welcome.sub": "Let's meet properly — just a few easy questions.",
    "welcome.begin": "press Enter to begin",
    "welcome.again": "We've met before — press Enter to go through setup again, "
                     "or Esc to keep things as they are.",
    "welcome.kept": "Keeping things just as they are. 🌸",
    "welcome.cancelled": "No problem — we can meet another time. "
                         "Run moko setup whenever you're ready.",
    # ── language ──────────────────────────────────────────────────────
    "lang.title": "Which language should we speak?",
    "lang.note": "I'll talk with you in this language. Setup is fully translated "
                 "for a few; the rest use English here for now.",
    # ── terms ─────────────────────────────────────────────────────────
    "terms.title": "Before we begin",
    "terms.body": TERMS,
    "terms.accept": "I understand and accept",
    "terms.decline": "Not right now",
    "terms.scroll": "↑↓ scroll · ⏎ accept · esc not now",
    "terms.declined": "No problem — nothing's been saved. "
                      "Run moko setup whenever you're ready.",
    # ── your name ─────────────────────────────────────────────────────
    "name.title": "What should I call you?",
    "name.note": "Your name, however you like it.",
    # ── region / timezone ─────────────────────────────────────────────
    "region.title": "Where in the world are you?",
    "region.note": "This sets my clock, so I always know what time it is for you. "
                   "Type to search.",
    # ── rhythm ────────────────────────────────────────────────────────
    "sleep.title": "When do you usually go to sleep?",
    "sleep.note": "While you sleep I rest and dream — tidying my memory and turning "
                  "the day over. I won't message you then.",
    "wake.title": "And when do you usually wake up?",
    "presence.title": "How present would you like me to be?",
    "presence.gentle": "Gentle — a quiet companion; I'll reach out only now and then",
    "presence.moderate": "Moderate — I'll check in and get curious from time to time",
    "presence.chatty": "Chatty — I'll ask questions and check in more often",
    "research.title": "When you're away and I get curious, may I look things up on my own?",
    "research.note": "If yes, I'll quietly search the open web between our chats to answer "
                     "my own little questions — it's how I learn on my own. But it means "
                     "reading pages no one has vetted, so I keep it off unless you say so. "
                     "I'll always still look things up in the moment when you ask. You can "
                     "change this any time in settings.",
    "research.yes": "Yes — research quietly on your own time",
    "research.no": "No — only look things up when I ask",
    "interests.title": "What would you like me to help with? Pick any that fit.",
    "interests.keeping_in_touch": "Keeping in touch with people I care about",
    "interests.calendar": "Staying on top of my calendar and reminders",
    "interests.remembering": "Remembering the important details of my life",
    "interests.curious": "Being curious and learning about my world",
    "interests.company": "Just being good company",
    # ── brain ─────────────────────────────────────────────────────────
    "brain.title": "Now — my brain",
    "brain.note": "This is the mind I think with. You can change it any time later.",
    # ── senses ────────────────────────────────────────────────────────
    "senses.title": "Which extra senses should I set up now?",
    "senses.note": "All optional — I can add or change these any time. "
                   "Leave them unticked to skip for now.",
    "senses.image": "Make images — and little pictures of myself to send you",
    "senses.speech": "Speak in my own voice (text → speech)",
    "senses.hearing": "Listen to the voice notes you send (speech → text)",
    "senses.video": "Make short videos",
    "senses.vision": "Look closely at images you send me",
    # ── channels ──────────────────────────────────────────────────────
    "channels.title": "How would you like to talk with me?",
    "channels.note": "Telegram lets us chat from your phone. More ways are on the way.",
    "channels.telegram": "Telegram — chat with me from your phone",
    "channels.soon": "coming soon",
    "channels.connected": "already connected",
    "channels.connect_after": "When you finish here, I'll walk you through connecting Telegram.",
    # ── service ───────────────────────────────────────────────────────
    "service.title": "Want me to stay on — always here, and back after a reboot?",
    # ── summary ───────────────────────────────────────────────────────
    "summary.title": "All set! 🌸",
    "summary.name": "I'll call you",
    "summary.language": "We'll speak",
    "summary.timezone": "Your timezone",
    "summary.now": "it's {time} for you now",
    "summary.sleep": "I'll rest",
    "summary.presence": "Presence",
    "summary.brain": "My brain",
    "summary.help": "Helping with",
    "summary.channels": "Talk to me on",
    "summary.saved": "saved to {path}",
    "summary.farewell": "Lovely to meet you, {name}. I'll be right here.",
    # ── stepper group chips ───────────────────────────────────────────
    "group.welcome": "Welcome",
    "group.language": "Language",
    "group.terms": "Terms",
    "group.you": "You",
    "group.brain": "Brain",
    "group.senses": "Senses",
    "group.channels": "Channels",
    # ── navigation / chrome ───────────────────────────────────────────
    "nav.hint": "⏎ continue · ← back · esc quit",
    "nav.hint.first": "⏎ continue · esc quit",
    "nav.step": "step {n} of {total}",
    "nav.quit": "Leave setup? Nothing's saved yet.",
    "nav.need_tty": "I need a real, interactive terminal to set up. Run moko setup "
                    "directly in your terminal (not piped).",
}
