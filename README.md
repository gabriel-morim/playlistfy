# playlistfy

Build niche Spotify playlists from what you actually listen to, not just what you remembered to like.

playlistfy reads your liked songs, top tracks and listening history, finds what fits a genre or an artist you name, discovers similar music through Last.fm, and writes the result to a Spotify playlist you can refresh every week. An optional Claude step picks the final tracks to match a vibe you describe, and explains each choice.

## Why

- Good songs come up in autoplay while you're working, you don't stop to like them, and they're gone. playlistfy captures what you played (via Last.fm scrobbling or its own `--harvest` log) and uses it.
- Spotify's own recommendation endpoints were removed from the public API in late 2024, so discovery here comes from Last.fm's similar-artist and similar-track data instead.
- Genre filters on big platforms are blunt. playlistfy lets you say exactly what belongs: genres to include and avoid, artists to always count in, songs to pin at the top, an album spread through the list.

## What it does

| | |
|---|---|
| **Genre playlists** | `--tags "house,deep house" --avoid-tags "rap,hip-hop"` builds from the matching part of your library. Tag rank matters: an artist tagged *house, electronic, hip-hop* counts as house; *rap, trap, ..., house* doesn't. |
| **Artist or song playlists** | `--like "Artist - Song, Other Artist"` builds around them. Named songs go to the top; named artists contribute their own deeper cuts. |
| **Albums** | `--album "Artist - Album"` spreads an album through the playlist in order, skipping tracks tagged as genres you don't want (including tracks featuring, say, a rapper on a house album). |
| **Deep cuts** | `--depth hits / mixed / deep` controls how far into each similar artist's catalogue it goes. Picks rotate weekly, and tracks already suggested are scored down so refreshes bring new music. |
| **Clean results** | One version per song, no DJ-mix cuts, strict artist/title matching on Spotify (never swaps an original for a remix). |
| **Vibe (optional)** | `--vibe "late-night afro house, hypnotic, no big drops"` lets Claude choose the final tracks, given your anchor songs, with a one-line reason for each. |
| **Profiles** | `--save-profile afro`, then `--profile afro` refreshes the same playlist with one command. |
| **Transparency** | Every run writes `picks.csv` with why each track is there, `seeds.csv` with which of your tracks counted, and `near_misses.csv` with what was left out and why. |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

1. **Spotify:** create an app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard), tick *Web API*, and set the redirect URI to exactly `http://127.0.0.1:8888/callback`. Copy the Client ID (no secret needed, login uses PKCE).
2. **Last.fm:** get an API key at [last.fm/api/account/create](https://www.last.fm/api/account/create).
3. **Optional:** `LASTFM_USER` if you scrobble Spotify to Last.fm, and `ANTHROPIC_API_KEY` for `--vibe`.

The first run opens a browser to log in to Spotify.

## Usage

Interactive menu:

```bash
python playlistfy.py
```

Or with options:

```bash
# see which genres your library has
python playlistfy.py --list-tags

# preview a genre playlist (writes picks.csv, doesn't touch Spotify)
python playlistfy.py --tags "house,deep house" --avoid-tags "rap,hip-hop" --include-seeds all --dry-run

# build around songs, add an album, let Claude pick for a vibe, save as a profile
python playlistfy.py --like "Da Africa Deep - Personal, &ME - Before The Flood" \
  --album "Drake - Honestly, Nevermind" --album-skip-tags "rap,hip-hop" \
  --vibe "late-night afro and deep house, hypnotic and rolling" \
  --name "Late Night Afro" --save-profile afro

# refresh it any time
python playlistfy.py --profile afro

# save recent plays (Spotify only keeps the last 50, so schedule this every 1-2 h)
python playlistfy.py --harvest
```

Run `python playlistfy.py --help` for every option.

## How it works

1. **Seeds:** liked songs, top tracks, listened tracks (Last.fm scrobbles and/or the harvest log), optional Spotify streaming-history export, or the artists/songs you name.
2. **Filter:** keep seeds whose artists' Last.fm tags fit the niche, respecting tag order and an avoid list.
3. **Discover:** Last.fm similar tracks, falling back to similar artists and a sample of their catalogue at the chosen depth.
4. **Rank:** score by how many of your seeds point to a track and how much you play those seeds, cap tracks per artist, drop duplicate versions, rotate out recent suggestions, optional Claude rerank.
5. **Match:** find each pick on Spotify with strict title/artist matching.
6. **Write:** create a private playlist, or replace the contents of an existing one.

## Limits

- Spotify's Development Mode allows up to five users per app, and access requires Premium. This is a personal tool, not a hosted service.
- Last.fm tags are crowd-sourced. Artists with shared names sometimes get mixed tags; `--force-artists` and `near_misses.csv` help work around it.
- Very small artists may have no Last.fm data at all.

## Privacy

Everything runs locally. Your token, caches, listening log, profiles and reports stay on your machine and are excluded from git by `.gitignore`.
