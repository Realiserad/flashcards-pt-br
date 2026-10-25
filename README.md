# About

Anki deck for building Brazilian Portuguese vocabulary. Over 12 000
flashcards in total, ordered by frequency so you learn the most common words
first. Each word in Brazilian Portuguese is put in a sentence and comes
with audio and a picture. The deck has cards for both PT-BR -> EN for 
comprehension (Wernicke’s area) and EN -> PT-BR for generation and
articulation (Broca’s area).

# How to use

The `apkg` file can be imported into [Anki](https://apps.ankiweb.net).

# Building

Building is a three-stage rocket.

## Create `words.txt`

Create a list of Brazilian Portuguese words, one word per line. I have a curated
list in this repository, but you can create your own.

## Create `flashcards.json` from `words.txt`

I used Claude to build this file. There is a skill file in this repository with 
further instructions. Building from scratch with Claude Opus cost me about $70 (🙈).

## Packaging the Anki deck

Run the Python script:

```bash
uv run create_apkg.py --input flashcards.json --output flashcards.apkg
```

The script adds two things that are not in the JSON: a picture from
[Openverse](https://openverse.org), and an audio of the Portuguese 
sentence from Google Translate's TTS endpoint. Both are cached in 
`--cache-dir`, so re-runs only fetch what is new.
