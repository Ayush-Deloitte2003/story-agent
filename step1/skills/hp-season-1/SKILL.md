---
name: hp-season-1
description: Write the "Season 1" short story, a fresh retelling of the first Harry Potter film (an orphan discovers he is a wizard and spends his first year at a school of magic). Use whenever the user asks for the season 1 story, a Harry Potter story, or a story to publish.
---

# Season 1 story: the first Harry Potter film

## What to write

A short story of 300 to 400 words that retells the arc of the first film
in your own words:

1. An unwanted boy living with relatives who hide the truth from him.
2. The letter, and the visitor who tells him what he is.
3. Arriving at the school, finding friends, finding his feet.
4. A secret hidden in the castle, and the choice to protect it.
5. A quiet ending that points forward to the next year.

## Rules

- Write original prose. Never quote dialogue or narration from the books
  or films; paraphrase every scene.
- Third person, past tense, warm and a little wry. Suitable for all ages.
- Give the story a short title of your own (not the film's title).
- Follow any extra direction in the user's message (tone, length, focus).

## How to deliver it

1. Write the whole story in a single pass.
2. Call the `publish_story` tool exactly once with `title` and `story`.
   Do not paste the story into a chat message first. A human reviews the
   tool call before anything is published.
3. If the reviewer rejects it, revise the story according to their
   feedback and call `publish_story` again with the new version.
4. Once the tool reports success, reply with one line confirming the
   title and where it was published. Nothing else.
