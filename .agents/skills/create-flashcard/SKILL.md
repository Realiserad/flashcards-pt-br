---
name: create-flashcard
description: Creates a pair of Brazilian Portuguese flashcards (pt→en and en→pt) for a word and appends them to `flashcards.json`. Use whenever the user wants to add a word, add vocabulary, or create or update flashcards.
---

# Input

One or more words or expressions in Brazilian Portuguese. Process each one
independently, running the full workflow below for each.

If the input is ambiguous (the word has several unrelated meanings, e.g.
`manga` = sleeve / mango), create a pair of flashcards for each one of them.

# Workflow

## 1. Check prerequisites

The word must not already be in `flashcards.json`. Check it:

```bash
test -f flashcards.json && jq -e --arg w "<word>" 'any(.[]; .word == $w)' flashcards.json
```

Exit code `0` (prints `true`) means the word is already there — skip it and
tell the user. Exit code `1`, or a missing file, means you can continue.

Also check whether the word is in the vocabulary list:

```bash
grep -Fxq -- "<word>" words.txt
```

If it is not found, still create the flashcards, but mention it to the user so
they can decide whether to add the word to `words.txt`.

## 2. Write the Brazilian Portuguese sentence

- Keep it short and natural — one clause, roughly 5–10 words, the kind of thing
  someone would actually say in conversation.
- Use Brazilian, not European, Portuguese: `você` rather than `tu`, and the
  gerund (`estou fazendo`) rather than `estou a fazer`.
- Prefer content words that appear in `words.txt`. Check a candidate word with
  `grep -Fxq -- "<word>" words.txt`, looking up its dictionary form
  (infinitive for verbs, singular for nouns). Function words — articles,
  pronouns, prepositions, contractions, conjunctions, numbers, determiners such
  as `no`, `os`, `todos` — are always fine, whether or not they are listed.
- Verbs may be in the past, present, or near future (`ir` + infinitive: `vou
  correr`, `vamos comer`). Avoid the synthetic future (`correrei`).
- For nouns, include the definite article (`o`/`a`) so the gender is visible on
  the card.
- Surround the word being learned with asterisks. Inflect it as the sentence
  requires — the asterisks mark the inflected form, e.g. `*corri*` for `correr`.

## 3. Build the two flashcards

Every flashcard has these fields:

- `word`: the word in Brazilian Portuguese, in its dictionary form, exactly as
  the user gave it.
- `front`: a string, the front of the card.
- `back`: a string, the back of the card.
- `visual_link`: a string, containing a short description of the word in english, useful when searching for/generating an image.
- `frequency`: a JSON **number** (not a string) from 1 to 10, for how often the
  word appears in everyday Brazilian Portuguese. Rough scale: 9–10 core
  everyday words, 7–8 common, 5–6 moderately common, 3–4 occasional, 1–2 rare
  or specialised.
- `tags`: an object. Keys and string values are in `kebab-case`
  (e.g. `"topic": "household-chores"`).
  - `type` (required): `noun`, `verb`, `adjective`, `adverb`, `preposition`,
    `expression`, …
  - `regular` (required for verbs): boolean, `true` if the verb conjugates
    regularly, `false` otherwise.
  - `gender` (required for nouns): `masculine` or `feminine`.
  - `topic` (optional): the topic of the word, e.g. `food`, `travel`.

Both cards for the same word must have identical `word`, `frequency`, and
`tags`. They only differ in `front` and `back`.

Brazilian Portuguese → English:

```json
{
  "word": "<the word in Brazilian Portuguese>",
  "front": "<the Brazilian Portuguese sentence from step 2, with the word in asterisks>",
  "visual_link": "<a short description of the word>",
  "back": "<the English translation of the word alone, not of the sentence, in its dictionary form>",
  "frequency": <1-10>,
  "tags": { "type": "<type>", "topic": "<topic>" }
}
```

English → Brazilian Portuguese:

```json
{
  "word": "<the word in Brazilian Portuguese>",
  "front": "<the English translation of the full sentence, with the English word in asterisks>",
  "visual_link": "<a short description of the word>",
  "back": "<the Brazilian Portuguese sentence from step 2, with the word in asterisks>",
  "frequency": <1-10>,
  "tags": { "type": "<type>", "topic": "<topic>" }
}
```

The asymmetry is deliberate: the pt→en card tests recognition of the word, so
its back is just the word; the en→pt card tests production, so its back is the
whole sentence.

## 4. Append to `flashcards.json`

Do not rewrite the file by hand — append with `jq` so existing entries and
formatting stay intact. Write the new cards to a temporary file as a JSON
array, then:

```bash
test -f flashcards.json || echo '[]' > flashcards.json
jq --indent 2 --slurpfile new /tmp/new-cards.json '. + $new[0]' flashcards.json > flashcards.tmp.json \
  && mv flashcards.tmp.json flashcards.json
```

Then confirm the file is still valid and report the new count:

```bash
jq 'length' flashcards.json
```

# Example

Input is `correr`.

```json
{
  "word": "correr",
  "front": "Eu gosto de *correr* no parque todos os dias.",
  "visual_link": "man running in a park",
  "back": "Run",
  "frequency": 8,
  "tags": {
    "type": "verb",
    "regular": true,
    "topic": "exercise"
  }
}
```

```json
{
  "word": "correr",
  "front": "I like to *run* in the park every day.",
  "back": "Eu gosto de *correr* no parque todos os dias.",
  "visual_link": "man running in a park",
  "frequency": 8,
  "tags": {
    "type": "verb",
    "regular": true,
    "topic": "exercise"
  }
}
```
