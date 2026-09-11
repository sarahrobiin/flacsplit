# flacsplit

### Sample-accurate, semi-automatic track splitting for archival audio

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python\&logoColor=white)
![FFmpeg](https://img.shields.io/badge/FFmpeg-required-007808?logo=ffmpeg\&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)
![Audio](https://img.shields.io/badge/output-FLAC-blue)
![Status](https://img.shields.io/badge/status-beta-orange)

**flacsplit** turns continuous digitized recordings into clean, numbered FLAC tracks without treating the spaces between
them as disposable audio.

It was built for digitized **vinyl, cassette tapes, reel-to-reel recordings, EPs, albums, live recordings and other
continuous archival audio** where ordinary silence splitting is often too simplistic.

Instead of blindly deleting silence, `flacsplit` uses silence as **evidence for possible boundaries**.

It can additionally use:

* the expected number of tracks,
* approximate or precise track durations,
* configurable silence detection,
* conservative start/end padding,
* and editable JSON split plans.

The result is a fast, deterministic workflow that preserves the continuous programme while removing only unwanted
silence at the physical beginning and end.

```text
original recording.flac

     unwanted          track 1       gap       track 2       gap       track 3        unwanted
         │                │           │           │           │           │               │
         ▼                ▼           ▼           ▼           ▼           ▼               ▼
───────────────██████████████████_________██████████████_________████████████████──────────────
               ▲                         ▲                   ▲                         ▲
               │                         │                   │                         │
        programme start              boundary            boundary                programme end


                                      flacsplit
                                          │
                                          ▼

                            ┌────────────────────────┐
                            │ 01.flac                │
                            │ 02.flac                │
                            │ 03.flac                │
                            └────────────────────────┘

                    No internal programme audio discarded.
```

---

## Why?

Digitizing analogue media is easy.

**Splitting it properly is surprisingly tedious.**

Traditional workflows tend to fall into one of two categories:

1. manually find every track boundary in an audio editor, or
2. run a silence detector and hope that silence happens to mean “new track”.

Neither model works particularly well for heterogeneous analogue archives.

A cassette may have a persistent noise floor. Vinyl has surface noise. A track can contain genuinely quiet passages.
Live recordings may contain applause instead of silence. Albums can crossfade seamlessly. Published track durations may
be approximate. Fade-outs can disappear below a simple detection threshold before the music has actually ended.

More importantly:

> **Silence between tracks is still part of the recording.**

If a recording contains:

```text
track 1 ──────── silence ──────── track 2
```

the useful operation is usually not:

```text
track 1                    track 2
         ✕ silence deleted
```

but:

```text
track 1 ─── silence │ silence ─── track 2
                    ▲
                  split
```

`flacsplit` therefore models the recording as **one continuous timeline that needs to be partitioned**, not as isolated
sounds separated by material that can simply be discarded.

---

# Quick start

Requirements:

* modern Python 3
* FFmpeg
* FFprobe
* Linux or another Unix-like environment

Check that FFmpeg is available:

```bash
ffmpeg -version
ffprobe -version
```

Make the script executable:

```bash
chmod +x flacsplit.py
```

Then run:

```bash
./flacsplit.py recording.flac
```

By default, the script will:

1. inspect the source,
2. detect silence candidates,
3. identify leading and trailing silence,
4. merge fragmented silence regions,
5. use internal silence midpoints as track boundaries,
6. retain protective padding around the beginning and end,
7. write a JSON split plan,
8. render numbered FLAC files,
9. verify the resulting sample rate, bit depth and sample counts.

Output:

```text
recording.flac
recording.split.json

recording_tracks/
├── 01.flac
├── 02.flac
├── 03.flac
└── 04.flac
```

No metadata lookup, naming, tagging or album recognition is performed.

That is intentional.

`flacsplit` does one job.

---

# Usage

## Automatic detection

For material with clear gaps:

```bash
./flacsplit.py side-a.flac
```

The script detects internal silence regions and places boundaries at their midpoints.

This is the simplest mode, but it may produce too many boundaries if the recording contains substantial quiet passages.

---

## Expected track count

If you know that an EP contains four tracks:

```bash
./flacsplit.py ep.flac --tracks 4
```

`flacsplit` now knows that it needs exactly:

```text
4 tracks
    ↓
3 internal boundaries
```

Instead of accepting every silence candidate, it selects the best combination of three candidates while respecting the
configured minimum track duration.

This is often substantially more robust than ordinary silence splitting.

Example:

```text
detected candidates

02:17   weak
04:05   strong
06:44   weak
09:51   strong
11:02   weak
13:38   strong

expected tracks: 4
expected cuts:   3

selected:

04:05
09:51
13:38
```

---

## Expected track durations

Published track durations are even more useful.

```bash
./flacsplit.py ep.flac \
    --durations 3:56,5:46,3:46,3:18
```

Supported formats include:

```text
236
3:56
00:03:56
3:56.420
```

Durations are interpreted sequentially.

Given:

```text
Track 1   03:56
Track 2   05:46
Track 3   03:46
Track 4   03:18
```

the expected internal boundaries become approximately:

```text
03:56
09:42
13:28
```

With the default tolerance of ±20 seconds, `flacsplit` searches around each expected position for a real silence
candidate.

```text
                      expected
                         │
                         ▼
─────────────────────────┼─────────────────────────
                    search window
                ◄────────┼────────►
                       ±20 s

                              actual gap
                                  │
                                  ▼
────────────────────────────────__ __──────────────
                                  ▲
                              boundary
```

If a plausible silence exists nearby, the boundary snaps to it.

If not, the expected duration itself becomes the boundary.

This makes duration-guided splitting useful even for:

* continuous albums,
* crossfades,
* DJ mixes,
* live recordings,
* classical works,
* tapes without clean inter-track silence.

---

## Exact durations / continuous audio

If supplied, timings should be used directly:

```bash
./flacsplit.py continuous.flac \
    --durations 4:12.3,3:47.8,5:03.1 \
    --tolerance 0
```

With zero tolerance, silence detection does not move the supplied internal boundaries.

This provides a simple way to partition fully continuous material from known timings.

---

# How boundary detection works

`flacsplit` deliberately separates **detection** from **partitioning**.

The audio is never processed merely because something was classified as silence.

## 1. FFmpeg finds candidate silence regions

By default:

```text
threshold           -40 dBFS
minimum duration      0.8 s
```

Equivalent conceptually to:

```bash
silencedetect=noise=-40dB:duration=0.8
```

A typical result might contain:

```text
silence   243.750 → 247.000
silence   591.025 → 592.465
silence   816.282 → 817.375
silence   817.991 → 819.890
```

---

## 2. Fragmented gaps are merged

Analogue sources are noisy.

A tiny click, burst of tape noise or other interruption should not necessarily turn one inter-track gap into two
separate candidates.

Given:

```text
silence
816.282 ─────────────── 817.375

                          0.616 s noise

                           817.991 ───────────────────── 819.890
                                      silence
```

with the default:

```text
--merge-gap 1.0
```

these regions become:

```text
816.282 ─────────────────────────────────────────────── 819.890
```

One candidate.

Not two.

---

## 3. Internal boundaries use the midpoint

For a silence region:

```text
243.750 → 247.000
```

the boundary becomes:

```text
245.375
```

Visually:

```text
track 1 █████████████▅▃▂______________▂▃▅████████████ track 2
                                │
                                ▲
                           split point
```

This preserves the complete interval.

The end sample of track *n* is also the start boundary of track *n+1*.

Conceptually:

```text
track 1 = programme_start → boundary_1
track 2 = boundary_1      → boundary_2
track 3 = boundary_2      → programme_end
```

There are no gaps between exported tracks.

There are no overlaps.

---

# Program edges

The beginning and end are intentionally treated differently from internal boundaries.

Internal gaps are preserved.

Outer silence may be discarded.

## Beginning

Suppose the file starts with nine seconds of silence:

```text
file start
│
▼
──────────────────────────────████████████████████
                              ▲
                       detected onset
```

Cutting exactly at the detector threshold could remove a subtle onset.

The default therefore retains:

```text
--start-pad 0.5
```

seconds before the detected program start.

```text
──────────────────────────────████████████████████
                         ▲    ▲
                         │    detected onset
                         │
                    actual start
```

---

## End

Fade-outs are particularly vulnerable to threshold-based trimming.

A recording may gradually fall below `-40 dBFS` while useful musical information remains.

Therefore the default end padding is deliberately longer:

```text
--end-pad 2.0
```

Conceptually:

```text
music ███████████████████████▆▄▃▂──────────────
                                  ▲       ▲
                                  │       │
                            threshold     │
                                      actual end
```

Only material after the padded program ends is discarded.

---

# Three operating modes

The boundary solver becomes progressively better as more information is available.

| Input knowledge       | Strategy                                             |
|-----------------------|------------------------------------------------------|
| Audio only            | Use detected internal silence                        |
| Track count           | Choose exactly `N - 1` suitable silence candidates   |
| Approximate durations | Search for real gaps near expected positions         |
| Precise durations     | Use supplied positions directly with `--tolerance 0` |

This allows the same tool to handle very different source material without pretending that a single silence heuristic
can understand every recording.

---

# The split plan

Every analysis produces a JSON split plan.

Default:

```text
recording.split.json
```

The split plan separates:

```text
analysis
    ↓
split plan
    ↓
rendering
```

This is intentional.

A future GUI, script, web interface or other application does not need to reimplement the actual FLAC splitting engine.

It can simply manipulate the plan.

Example structure:

```json
{
  "version": 2,
  "source": {
    "path": "/archive/recording.flac",
    "sample_rate": 44100,
    "bits_per_raw_sample": 16
  },
  "programme": {
    "start_sample": 401078,
    "end_sample": 44764816
  },
  "boundaries": [
    {
      "sample": 10821038,
      "seconds": 245.375,
      "source": "ranked-silence"
    },
    {
      "sample": 26005955,
      "seconds": 589.704,
      "source": "ranked-silence"
    }
  ]
}
```

The authoritative values are the **sample positions**, not formatted timestamps.

---

## Analyze without rendering

```bash
./flacsplit.py album.flac \
    --tracks 8 \
    --plan-only
```

This produces the split plan without writing track files.

That is useful when:

* integrating with another program,
* reviewing boundaries externally,
* testing detection settings,
* building a GUI,
* editing boundary positions programmatically.

---

## Render an existing plan

```bash
./flacsplit.py --from-plan album.split.json
```

Programme edges and boundary sample positions in the plan are treated as authoritative.

No new boundary analysis is required.

This creates a deliberately simple integration point:

```text
                   ┌─────────────────────┐
                   │ flacsplit detection │
                   └──────────┬──────────┘
                              │
                              │
                    ┌─────────▼─────────┐
                    │   split plan      │
                    │      JSON         │
                    └─────────┬─────────┘
                              │
              ┌───────────────┼────────────────┐
              │               │                │
              ▼               ▼                ▼
         manual edit      future GUI      other software
              │               │                │
              └───────────────┼────────────────┘
                              │
                              ▼
                     flacsplit renderer
                              │
                              ▼
                    numbered FLAC tracks
```

---

# Audio integrity

The primary design goal is not merely convenience.

It is **predictable archival behavior**.

## Internal audio is not removed

Between program start and program end:

> Every sample belongs to exactly one output track.

If:

```text
B1 = first boundary
B2 = second boundary
```

then:

```text
track 1: start → B1
track 2: B1    → B2
track 3: B2    → end
```

Therefore:

```text
end(track n) = start(track n + 1)
```

No inter-track material disappears.

---

## Cuts are sample-based

The renderer ultimately operates on sample positions rather than relying purely on approximate timestamp seeking.

This avoids small discrepancies accumulating between independently generated outputs.

Example:

```text
boundary: sample 10,821,038

track 1 ends at:   sample 10,821,038
track 2 begins at: sample 10,821,038
```

---

## Why FLAC is re-encoded

`flacsplit` does **not** rely on:

```bash
-c copy
```

for arbitrary cuts.

Compressed audio formats are organized into codec frames. Arbitrary desired PCM sample boundaries do not necessarily
coincide with those frame boundaries.

Instead:

```text
FLAC
  ↓
lossless PCM decode
  ↓
sample-accurate partition
  ↓
FLAC encode
```

FLAC is lossless.

Therefore FLAC → PCM → FLAC does not introduce psychoacoustic generation loss.

No normalization, EQ, noise reduction, fades, resampling or other DSP is applied by `flacsplit`.

---

## Source properties are validated

After rendering, `flacsplit` verifies important properties of the generated files.

Among them:

* sample rate,
* bit depth,
* expected sample counts.

If those properties unexpectedly change, the command fails rather than silently accepting the output.

This is particularly important for high-resolution archival sources such as:

```text
96 kHz / 24-bit
192 kHz / 24-bit
```

---

# Default parameters

The defaults aim to be conservative for digitized analogue material.

| Parameter              |    Default | Purpose                                                    |
|------------------------|-----------:|------------------------------------------------------------|
| `--threshold`          | `-40 dBFS` | Silence detection threshold                                |
| `--silence-duration`   |    `0.8 s` | Minimum detected silence                                   |
| `--merge-gap`          |    `1.0 s` | Merge briefly interrupted silence                          |
| `--start-pad`          |    `0.5 s` | Protect programme onset                                    |
| `--end-pad`            |    `2.0 s` | Protect low-level fade-outs                                |
| `--edge-tolerance`     |    `1.0 s` | Recognize silence close to physical file edges             |
| `--tolerance`          |     `20 s` | Search radius around expected duration boundaries          |
| `--min-track-duration` |     `30 s` | Reject implausibly short tracks during candidate selection |
| `--compression-level`  |        `5` | FFmpeg FLAC compression level                              |

These are starting points, not universal truths.

Analogue archives vary enormously.

---

# CLI reference

```text
usage: flacsplit.py [-h] [--version]
                    [--tracks TRACKS]
                    [--durations LIST]
                    [--tolerance SEC]
                    [--min-track-duration SEC]
                    [--threshold DB]
                    [--silence-duration SEC]
                    [--merge-gap SEC]
                    [--start-pad SEC]
                    [--end-pad SEC]
                    [--edge-tolerance SEC]
                    [-o OUTPUT_DIR]
                    [--plan PLAN]
                    [--plan-only]
                    [--from-plan JSON]
                    [--compression-level 0..12]
                    [--overwrite]
                    [input]
```

## Boundary guidance

### `--tracks N`

Expected number of tracks.

```bash
./flacsplit.py ep.flac --tracks 4
```

When durations are not supplied, the solver selects exactly `N - 1` internal silence candidates.

---

### `--durations LIST`

Comma-separated expected track durations.

```bash
./flacsplit.py album.flac \
    --durations 4:12,3:47,5:03,4:26
```

Accepted forms:

```text
252
4:12
4:12.500
00:04:12.500
```

Supplying durations implicitly provides the expected track count.

---

### `--tolerance SEC`

Search radius around duration-derived expected boundaries.

Default:

```text
20
```

Example:

```bash
./flacsplit.py album.flac \
    --durations 4:12,3:47,5:03 \
    --tolerance 10
```

Set to zero for exact timings:

```bash
--tolerance 0
```

---

### `--min-track-duration SEC`

Minimum allowed track duration when selecting candidate gaps.

Default:

```text
30
```

For recordings with short interludes:

```bash
./flacsplit.py album.flac \
    --tracks 14 \
    --min-track-duration 8
```

---

# Silence detection

### `--threshold DB`

FFmpeg silence threshold in dBFS.

Default:

```text
-40
```

Example for a noisier recording:

```bash
./flacsplit.py tape.flac --threshold -35
```

---

### `--silence-duration SEC`

Minimum silence duration.

Default:

```text
0.8
```

Example:

```bash
./flacsplit.py record.flac \
    --silence-duration 1.2
```

---

### `--merge-gap SEC`

Merge silence regions interrupted by no more than this amount of sound.

Default:

```text
1.0
```

Example:

```bash
./flacsplit.py tape.flac \
    --merge-gap 1.5
```

This is particularly useful for analogue noise, clicks and brief disturbances inside otherwise obvious inter-track gaps.

---

# Program edge options

### `--start-pad SEC`

Audio retained before the detected beginning (begin of first song).

Default:

```text
0.5
```

---

### `--end-pad SEC`

Audio retained after the detected program ends (end of last song).

Default:

```text
2.0
```

The larger default deliberately protects fade-outs.

---

### `--edge-tolerance SEC`

A detected silence may begin or end this far away from the physical file boundary and still count as leading/trailing
silence.

Default:

```text
1.0
```

---

# Output options

### `-o`, `--output-dir`

Specify the output directory.

```bash
./flacsplit.py album.flac \
    --output-dir ./tracks
```

Default:

```text
<input-stem>_tracks
```

---

### `--plan PATH`

Choose the JSON split-plan location.

```bash
./flacsplit.py album.flac \
    --plan ./plans/album.json
```

Default:

```text
<input-stem>.split.json
```

---

### `--plan-only`

Analyze and create the split plan without rendering FLAC files.

```bash
./flacsplit.py album.flac \
    --tracks 10 \
    --plan-only
```

---

### `--from-plan JSON`

Render using a previously generated or externally modified split plan.

```bash
./flacsplit.py --from-plan album.split.json
```

---

### `--compression-level 0..12`

Set FFmpeg's FLAC compression level.

Default:

```text
5
```

For example:

```bash
./flacsplit.py album.flac \
    --compression-level 8
```

Compression level affects encoding effort and resulting file size.

It does **not** change audio quality: all FLAC compression levels are lossless.

---

### `--overwrite`

Allow existing numbered output files to be replaced.

```bash
./flacsplit.py album.flac --overwrite
```

Without this option, existing output is protected.

---

# Practical recipes

## Clean vinyl EP

You know it has four tracks:

```bash
./flacsplit.py ep.flac --tracks 4
```

---

## Album with published durations

```bash
./flacsplit.py album.flac \
    --durations 4:32,3:51,5:18,4:07,6:12
```

Published timings can be approximate.

The default ±20 second search window allows the script to find nearby real gaps.

---

## Continuous album

```bash
./flacsplit.py mix.flac \
    --durations 5:14.2,4:37.8,7:01.5,6:22.0 \
    --tolerance 0
```

---

## Noisy cassette

A higher silence threshold may work better:

```bash
./flacsplit.py tape.flac \
    --tracks 9 \
    --threshold -34 \
    --silence-duration 1.0 \
    --merge-gap 1.5
```

---

## Preserve more of a long fade

```bash
./flacsplit.py album.flac \
    --tracks 8 \
    --end-pad 4
```

---

## Generate a plan for external review

```bash
./flacsplit.py album.flac \
    --tracks 12 \
    --plan-only
```

Edit or process:

```text
album.split.json
```

Then:

```bash
./flacsplit.py --from-plan album.split.json
```

---

# What flacsplit deliberately does not do

`flacsplit` is intentionally small.

It does not attempt to become a complete music-library application.

It currently does **not**:

* identify songs,
* query MusicBrainz,
* query Discogs,
* fingerprint recordings,
* fetch cover art,
* assign artist/album metadata,
* normalize audio,
* remove noise,
* restore recordings,
* remove internal silence,
* guess musical structure using AI,
* provide an interactive waveform editor.

This separation is intentional.

A practical workflow can therefore look like:

```text
digitized master
      │
      ▼
   flacsplit
      │
      ▼
01.flac
02.flac
03.flac
      │
      ▼
Mp3tag / MusicBrainz Picard / other tagger
      │
      ▼
finished music library
```

Dedicated metadata tools are already very good at metadata.

`flacsplit` focuses on the part they generally do not solve:

> **Where should a continuous archival recording actually be divided?**

---

# Design philosophy

A few principles guide the project.

### Silence is evidence, not truth

A quiet region may indicate a track boundary.

It may also be part of the music.

Therefore, additional information such as track count or expected duration can constrain the detector.

---

### Preserve first, optimize second

For internal boundaries, retaining too much natural gap is preferable to silently deleting source material.

The original program should remain reconstructable.

---

### Human knowledge is useful input

If you already know:

> “This EP has four tracks.”

that information should improve the algorithm.

If you know approximate durations, that should improve it further.

Automation does not have to pretend the operator knows nothing.

---

### Keep the intermediate representation explicit

The JSON split plan makes boundary decisions inspectable, editable and reusable.

Detection and rendering are therefore not inseparably coupled.

---

### Do one job well

Metadata management, restoration and library organization are deliberately kept outside the project.

That keeps the CLI predictable and makes it suitable as a building block for larger workflows.

---

# Architecture

At a high level:

```text
┌──────────────────────────────────────────────────────────────┐
│                         SOURCE AUDIO                         │
│                    FLAC / FFmpeg input                       │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │      FFprobe        │
                    │ source properties   │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │      FFmpeg         │
                    │   silencedetect     │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ candidate merging   │
                    │ + edge detection    │
                    └──────────┬──────────┘
                               │
                 ┌─────────────┼─────────────┐
                 │             │             │
                 ▼             ▼             ▼
             audio only     --tracks    --durations
                 │             │             │
                 └─────────────┼─────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ boundary selection  │
                    │ sample positions    │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  JSON split plan    │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ sample-accurate     │
                    │ FLAC rendering      │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ validation          │
                    └──────────┬──────────┘
                               │
                               ▼
                     01.flac 02.flac …
```

---

# Limitations

No automatic boundary detector can reliably understand every possible recording.

Examples include:

* very long intentional quiet passages,
* completely continuous material without known timings,
* incorrect published track durations,
* recordings with unusually high or changing noise floors,
* damaged recordings,
* arbitrary edits or hidden tracks.

Duration guidance can solve many continuous-material cases, but it is still guidance.

For important archival sources, retain the untouched master recording.

The generated tracks should be considered a convenient representation of that master, not a replacement for it.

---

# Recommended archival workflow

A simple archival structure might be:

```text
Artist - Release/
├── master.flac
├── master.split.json
├── artwork/
│   └── cover.jpg
└── tracks/
    ├── 01.flac
    ├── 02.flac
    ├── 03.flac
    └── 04.flac
```

The master remains canonical.

The split plan records how it was partitioned.

The track files are convenient derivatives for ordinary playback and library management.

---

# Requirements

## Python

A modern Python 3 installation is required.

Check:

```bash
python3 --version
```

## FFmpeg

Both `ffmpeg` and `ffprobe` must be available in `$PATH`.

On Debian/Ubuntu:

```bash
sudo apt install ffmpeg
```

Check:

```bash
ffmpeg -version
ffprobe -version
```

No Python packages outside the standard library are required.

---

# Installation

For now, `flacsplit` is intentionally distributed as a single executable Python script.

```bash
git clone https://github.com/sarahrobiin/flacsplit.git
cd flacsplit

chmod +x flacsplit.py
```

Optionally make it available system-wide:

```bash
sudo install -m 755 flacsplit.py /usr/local/bin/flacsplit
```

Then:

```bash
flacsplit album.flac --tracks 8
```

No virtual environment is required.

No service needs to run.

No audio is uploaded anywhere.

---

# Philosophy in one diagram

```text
                     ┌──────────────────────┐
                     │ What do we KNOW?     │
                     ├──────────────────────┤
                     │ waveform             │
                     │ quiet regions        │
                     │ track count?         │
                     │ durations?           │
                     └──────────┬───────────┘
                                │
                                ▼
                     ┌──────────────────────┐
                     │ What can we INFER?   │
                     ├──────────────────────┤
                     │ likely boundaries    │
                     │ programme edges      │
                     │ confidence through   │
                     │ multiple constraints │
                     └──────────┬───────────┘
                                │
                                ▼
                     ┌──────────────────────┐
                     │ What do we CHANGE?   │
                     ├──────────────────────┤
                     │ partition timeline   │
                     │ trim outer silence   │
                     │ nothing else         │
                     └──────────────────────┘
```

That distinction is the core of `flacsplit`.

---

# Contributing

Bug reports, unusual source examples and improvements to boundary detection are welcome.

Particularly useful cases include recordings where conventional silence detection fails:

* noisy tapes,
* vinyl with substantial surface noise,
* crossfaded albums,
* live material,
* classical recordings,
* unusual mastering,
* tracks containing long quiet passages.

When reporting detection issues, please include relevant command-line parameters and detector output where possible.

Do not upload copyrighted source audio unless you have the right to distribute it.

---

# Roadmap

`flacsplit` is deliberately small, but useful future improvements could include:

* relative/local noise-floor analysis,
* better boundary confidence scoring,
* energy-drop analysis around candidate regions,
* spectral information in boundary scoring,
* machine-readable confidence values in split plans,
* improved handling of changing tape noise floors,
* additional split-plan tooling,
* optional CUE export,
* a lightweight waveform UI built on top of the existing plan format.

The CLI and split-plan model are intended to remain useful even if richer interfaces are built around them.

---

# License

MIT License.

See [`LICENSE`](LICENSE) for details.

---

# About

`flacsplit` started with a very ordinary problem: a collection of digitized tapes and records, and far too much time
being spent manually finding track boundaries.

The useful insight turned out to be simple:

> **A track splitter should partition the recording, not reinterpret it.**

From there, silence detection becomes one input among several rather than the final authority.

The result is a small Unix-style tool: focused, scriptable, inspectable, lossless and easy to integrate into a larger
archival workflow.
