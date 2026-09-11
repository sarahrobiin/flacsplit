#!/usr/bin/env python3
"""
flacsplit.py — deterministic, semi-automatic archival audio splitter.

Purpose
-------
Split one continuous audio recording into numbered FLAC tracks while preserving
all audio between track boundaries. The tool uses FFmpeg's ``silencedetect`` as
one source of boundary candidates, but can also use an expected track count or
approximate/precise track durations to constrain the result.

The design is intentionally narrow:

* no interactive UI;
* no metadata lookup or tagging;
* no normalization, fades, resampling, or silence removal between tracks;
* only leading/trailing dead air may be trimmed, with conservative padding;
* internal boundaries partition the source sample-for-sample: the end sample of
  one output track is the start sample of the next;
* outputs are simply ``01.flac``, ``02.flac``, ... for later processing in a
  dedicated tagger;
* every run writes a JSON split plan which can be inspected, edited, archived,
  or consumed by a future GUI/other program.

Requires Python 3.11+ and FFmpeg/ffprobe available on PATH.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Sequence

VERSION = "2.0.0"
DEFAULT_THRESHOLD_DB = -40.0
DEFAULT_MIN_SILENCE = 0.8
DEFAULT_MERGE_GAP = 1.0
DEFAULT_START_PAD = 0.5
DEFAULT_END_PAD = 2.0
DEFAULT_TOLERANCE = 20.0
DEFAULT_MIN_TRACK = 30.0
DEFAULT_EDGE_TOLERANCE = 1.0
DEFAULT_COMPRESSION_LEVEL = 5


class SplitError(RuntimeError):
    """Expected user-facing failure."""


@dataclass(slots=True)
class AudioInfo:
    path: str
    codec: str
    sample_rate: int
    sample_fmt: str
    bits_per_raw_sample: int | None
    channels: int
    channel_layout: str | None
    total_samples: int
    duration_seconds: float


@dataclass(slots=True)
class SilenceRegion:
    start_sample: int
    end_sample: int
    silent_samples: int
    interruption_samples: int = 0

    @property
    def span_samples(self) -> int:
        return self.end_sample - self.start_sample

    def midpoint_sample(self) -> int:
        return (self.start_sample + self.end_sample) // 2

    def score(self) -> float:
        # Favor genuinely long silence while mildly penalizing short bursts of
        # sound that were bridged by --merge-gap.
        return max(0.0, self.silent_samples - 0.5 * self.interruption_samples)


@dataclass(slots=True)
class Boundary:
    sample: int
    source: str
    expected_sample: int | None = None
    candidate_score: float | None = None


@dataclass(slots=True)
class TrackRange:
    number: int
    start_sample: int
    end_sample: int


# ---------------------------------------------------------------------------
# Utility / process helpers
# ---------------------------------------------------------------------------


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise SplitError(f"required executable not found on PATH: {name}")


def run_checked(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise SplitError(f"executable not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise SplitError(f"command failed: {' '.join(cmd)}\n{detail}") from exc


def seconds_to_samples(seconds: float, sample_rate: int) -> int:
    return int(round(seconds * sample_rate))


def samples_to_seconds(samples: int, sample_rate: int) -> float:
    return samples / sample_rate


def format_time(seconds: float) -> str:
    if seconds < 0:
        sign = "-"
        seconds = abs(seconds)
    else:
        sign = ""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if hours:
        return f"{sign}{hours:02d}:{minutes:02d}:{secs:06.3f}"
    return f"{sign}{minutes:02d}:{secs:06.3f}"


def parse_time_value(value: str) -> float:
    """Parse seconds, MM:SS(.sss), or HH:MM:SS(.sss)."""
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("empty duration")

    try:
        if ":" not in value:
            result = float(value)
        else:
            parts = value.split(":")
            if len(parts) == 2:
                minutes = int(parts[0])
                seconds = float(parts[1])
                if seconds >= 60:
                    raise ValueError
                result = minutes * 60 + seconds
            elif len(parts) == 3:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = float(parts[2])
                if minutes >= 60 or seconds >= 60:
                    raise ValueError
                result = hours * 3600 + minutes * 60 + seconds
            else:
                raise ValueError
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid time '{value}' (use seconds, MM:SS, or HH:MM:SS)"
        ) from exc

    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError(f"duration must be > 0: {value}")
    return result


def parse_durations(value: str) -> list[float]:
    try:
        result = [parse_time_value(part) for part in value.split(",")]
    except argparse.ArgumentTypeError:
        raise
    if not result:
        raise argparse.ArgumentTypeError("at least one duration is required")
    return result


# ---------------------------------------------------------------------------
# Probe and silence analysis
# ---------------------------------------------------------------------------


def probe_audio(path: Path) -> AudioInfo:
    result = run_checked(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            (
                "stream=codec_name,sample_rate,sample_fmt,bits_per_raw_sample,"
                "channels,channel_layout,duration,duration_ts,time_base:"
                "format=duration"
            ),
            "-of",
            "json",
            str(path),
        ]
    )

    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        sample_rate = int(stream["sample_rate"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SplitError(f"could not probe an audio stream from: {path}") from exc

    total_samples: int | None = None
    duration_ts = stream.get("duration_ts")
    time_base = stream.get("time_base")

    if duration_ts is not None and time_base:
        try:
            seconds = float(Fraction(time_base) * int(duration_ts))
            total_samples = round(seconds * sample_rate)
        except (ValueError, ZeroDivisionError):
            total_samples = None

    duration = stream.get("duration") or data.get("format", {}).get("duration")
    if total_samples is None:
        if duration is None:
            raise SplitError(f"could not determine duration of: {path}")
        total_samples = round(float(duration) * sample_rate)

    bits_raw = stream.get("bits_per_raw_sample")
    bits = int(bits_raw) if bits_raw not in (None, "", "0", 0) else None

    return AudioInfo(
        path=str(path.resolve()),
        codec=str(stream.get("codec_name", "unknown")),
        sample_rate=sample_rate,
        sample_fmt=str(stream.get("sample_fmt", "")),
        bits_per_raw_sample=bits,
        channels=int(stream.get("channels", 0)),
        channel_layout=stream.get("channel_layout"),
        total_samples=total_samples,
        duration_seconds=total_samples / sample_rate,
    )


def detect_silences(
    path: Path,
    info: AudioInfo,
    threshold_db: float,
    minimum_duration: float,
) -> list[SilenceRegion]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-af",
        f"silencedetect=noise={threshold_db:g}dB:duration={minimum_duration:g}",
        "-f",
        "null",
        "-",
    ]

    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise SplitError(f"FFmpeg silence analysis failed:\n{proc.stderr.strip()}")

    start_re = re.compile(r"silence_start:\s*([-+0-9.eE]+)")
    end_re = re.compile(r"silence_end:\s*([-+0-9.eE]+)")

    current_start: float | None = None
    regions: list[SilenceRegion] = []

    for line in proc.stderr.splitlines():
        start_match = start_re.search(line)
        if start_match:
            current_start = float(start_match.group(1))
            continue

        end_match = end_re.search(line)
        if end_match and current_start is not None:
            end = float(end_match.group(1))
            start_sample = max(0, seconds_to_samples(current_start, info.sample_rate))
            end_sample = min(
                info.total_samples,
                seconds_to_samples(end, info.sample_rate),
            )
            if end_sample > start_sample:
                regions.append(
                    SilenceRegion(
                        start_sample=start_sample,
                        end_sample=end_sample,
                        silent_samples=end_sample - start_sample,
                    )
                )
            current_start = None

    # Current FFmpeg emits silence_end at EOF. If a future/older build does not,
    # retain a trailing open interval rather than silently losing it.
    if current_start is not None:
        start_sample = max(0, seconds_to_samples(current_start, info.sample_rate))
        if info.total_samples > start_sample:
            regions.append(
                SilenceRegion(
                    start_sample=start_sample,
                    end_sample=info.total_samples,
                    silent_samples=info.total_samples - start_sample,
                )
            )

    return regions


def merge_silences(
    regions: Sequence[SilenceRegion],
    max_interruption_samples: int,
) -> list[SilenceRegion]:
    """Merge silence regions separated by a short burst of sound/noise."""
    if not regions:
        return []

    merged: list[SilenceRegion] = [
        SilenceRegion(
            regions[0].start_sample,
            regions[0].end_sample,
            regions[0].silent_samples,
            regions[0].interruption_samples,
        )
    ]

    for region in regions[1:]:
        previous = merged[-1]
        interruption = region.start_sample - previous.end_sample

        if 0 <= interruption <= max_interruption_samples:
            previous.end_sample = region.end_sample
            previous.silent_samples += region.silent_samples
            previous.interruption_samples += interruption + region.interruption_samples
        else:
            merged.append(
                SilenceRegion(
                    region.start_sample,
                    region.end_sample,
                    region.silent_samples,
                    region.interruption_samples,
                )
            )

    return merged


def determine_programme_edges(
    regions: Sequence[SilenceRegion],
    info: AudioInfo,
    start_pad: float,
    end_pad: float,
    edge_tolerance: float,
) -> tuple[int, int, list[SilenceRegion], bool, bool]:
    """
    Determine useful programme start/end from silence touching either edge.

    Padding is retained *inside* the detected edge silence so low-level onset or
    fade material is less likely to be clipped. Internal silence is never
    removed; it is only used to choose split points.
    """
    internal = list(regions)
    edge_samples = seconds_to_samples(edge_tolerance, info.sample_rate)

    if (
        internal
        and internal[0].start_sample <= edge_samples
        and internal[0].end_sample >= info.total_samples - edge_samples
    ):
        raise SplitError("the source appears to contain no programme audio above the silence threshold")

    start_sample = 0
    end_sample = info.total_samples
    trimmed_start = False
    trimmed_end = False

    if internal and internal[0].start_sample <= edge_samples:
        start_sample = max(
            0,
            internal[0].end_sample
            - seconds_to_samples(start_pad, info.sample_rate),
        )
        internal = internal[1:]
        trimmed_start = start_sample > 0

    if internal and internal[-1].end_sample >= info.total_samples - edge_samples:
        end_sample = min(
            info.total_samples,
            internal[-1].start_sample
            + seconds_to_samples(end_pad, info.sample_rate),
        )
        internal = internal[:-1]
        trimmed_end = end_sample < info.total_samples

    if end_sample <= start_sample:
        raise SplitError("edge trimming would remove the entire programme")

    return start_sample, end_sample, internal, trimmed_start, trimmed_end


# ---------------------------------------------------------------------------
# Boundary selection
# ---------------------------------------------------------------------------


def select_exact_candidate_count(
    candidates: Sequence[SilenceRegion],
    count: int,
    programme_start: int,
    programme_end: int,
    min_track_samples: int,
) -> list[SilenceRegion]:
    """
    Choose exactly ``count`` silence candidates with maximum total score while
    enforcing a minimum distance between every resulting track boundary.

    Dynamic programming is O(track_count * candidate_count).
    """
    if count == 0:
        return []

    usable = [
        c
        for c in candidates
        if c.midpoint_sample() - programme_start >= min_track_samples
        and programme_end - c.midpoint_sample() >= min_track_samples
    ]
    usable.sort(key=lambda c: c.midpoint_sample())

    if len(usable) < count:
        raise SplitError(
            f"need {count} internal cuts but only {len(usable)} usable silence "
            "candidates remain; provide --durations, lower --min-track-duration, "
            "or adjust silence detection"
        )

    n = len(usable)
    times = [c.midpoint_sample() for c in usable]
    scores = [c.score() for c in usable]

    # dp[layer][i] = best score for exactly `layer` selected candidates ending i
    neg_inf = float("-inf")
    dp = [[neg_inf] * n for _ in range(count + 1)]
    back = [[-1] * n for _ in range(count + 1)]

    for i in range(n):
        if times[i] - programme_start >= min_track_samples:
            dp[1][i] = scores[i]

    for layer in range(2, count + 1):
        j = 0
        best_value = neg_inf
        best_index = -1
        for i in range(n):
            limit = times[i] - min_track_samples
            while j < i and times[j] <= limit:
                if dp[layer - 1][j] > best_value:
                    best_value = dp[layer - 1][j]
                    best_index = j
                j += 1
            if best_index >= 0:
                dp[layer][i] = best_value + scores[i]
                back[layer][i] = best_index

    best_final = -1
    best_score = neg_inf
    for i in range(n):
        if programme_end - times[i] >= min_track_samples and dp[count][i] > best_score:
            best_score = dp[count][i]
            best_final = i

    if best_final < 0:
        raise SplitError(
            f"could not place {count} cuts while keeping every track at least "
            f"{min_track_samples} samples long"
        )

    indexes: list[int] = []
    current = best_final
    for layer in range(count, 0, -1):
        indexes.append(current)
        current = back[layer][current]
    indexes.reverse()
    return [usable[i] for i in indexes]


def choose_duration_guided_boundaries(
    candidates: Sequence[SilenceRegion],
    durations: Sequence[float],
    info: AudioInfo,
    programme_start: int,
    programme_end: int,
    tolerance: float,
    min_track_duration: float,
) -> list[Boundary]:
    """
    Place cuts sequentially from expected track durations.

    Each expected boundary is measured from the *previous chosen boundary*, so
    snapping one track to a real local gap does not accumulate timing error into
    every later track. A silence candidate within +/- --tolerance is preferred;
    if none exists, the expected position itself is used (continuous-audio
    fallback). Set --tolerance 0 for exact duration-based cuts.
    """
    if len(durations) < 2:
        return []

    tolerance_samples = seconds_to_samples(tolerance, info.sample_rate)
    min_track_samples = seconds_to_samples(min_track_duration, info.sample_rate)
    max_candidate_score = max((c.score() for c in candidates), default=1.0) or 1.0

    boundaries: list[Boundary] = []
    previous = programme_start
    used: set[int] = set()

    for track_index, track_duration in enumerate(durations[:-1], start=1):
        expected = previous + seconds_to_samples(track_duration, info.sample_rate)

        if expected >= programme_end:
            raise SplitError(
                f"duration guidance places cut {track_index} beyond the programme end "
                f"({format_time(samples_to_seconds(expected, info.sample_rate))})"
            )

        chosen_index: int | None = None
        chosen_combined_score = float("-inf")

        if tolerance_samples > 0:
            for idx, candidate in enumerate(candidates):
                if idx in used:
                    continue
                point = candidate.midpoint_sample()
                distance = abs(point - expected)
                if distance > tolerance_samples:
                    continue
                if point - previous < min_track_samples:
                    continue

                timing_score = 1.0 - (distance / tolerance_samples)
                silence_score = candidate.score() / max_candidate_score
                combined = 0.75 * timing_score + 0.25 * silence_score

                if combined > chosen_combined_score:
                    chosen_combined_score = combined
                    chosen_index = idx

        if chosen_index is not None:
            candidate = candidates[chosen_index]
            point = candidate.midpoint_sample()
            boundaries.append(
                Boundary(
                    sample=point,
                    source="silence+duration",
                    expected_sample=expected,
                    candidate_score=candidate.score(),
                )
            )
            used.add(chosen_index)
            previous = point
        else:
            if expected - previous < min_track_samples:
                raise SplitError(
                    f"duration for track {track_index} is shorter than "
                    f"--min-track-duration ({min_track_duration:g}s)"
                )
            boundaries.append(
                Boundary(
                    sample=expected,
                    source="duration-fallback",
                    expected_sample=expected,
                )
            )
            previous = expected

    if programme_end - previous < min_track_samples:
        raise SplitError(
            "the final track would be shorter than --min-track-duration; "
            "check durations, edge detection, or lower the minimum"
        )

    return boundaries


def build_boundaries(
    candidates: Sequence[SilenceRegion],
    info: AudioInfo,
    programme_start: int,
    programme_end: int,
    tracks: int | None,
    durations: Sequence[float] | None,
    tolerance: float,
    min_track_duration: float,
) -> list[Boundary]:
    if durations is not None:
        inferred_tracks = len(durations)
        if tracks is not None and tracks != inferred_tracks:
            raise SplitError(
                f"--tracks={tracks} conflicts with {inferred_tracks} values in --durations"
            )
        return choose_duration_guided_boundaries(
            candidates,
            durations,
            info,
            programme_start,
            programme_end,
            tolerance,
            min_track_duration,
        )

    if tracks is not None:
        if tracks < 1:
            raise SplitError("--tracks must be at least 1")
        chosen = select_exact_candidate_count(
            candidates,
            tracks - 1,
            programme_start,
            programme_end,
            seconds_to_samples(min_track_duration, info.sample_rate),
        )
        return [
            Boundary(sample=c.midpoint_sample(), source="ranked-silence", candidate_score=c.score())
            for c in chosen
        ]

    # Unconstrained mode: every internal merged silence becomes a cut.
    return [
        Boundary(sample=c.midpoint_sample(), source="silence", candidate_score=c.score())
        for c in candidates
    ]


def validate_boundaries(
    boundaries: Sequence[Boundary],
    programme_start: int,
    programme_end: int,
) -> None:
    points = [programme_start] + [b.sample for b in boundaries] + [programme_end]
    if points != sorted(points) or len(points) != len(set(points)):
        raise SplitError("calculated boundaries are not strictly increasing")
    if points[0] < 0 or points[-1] <= points[0]:
        raise SplitError("invalid programme boundaries")


# ---------------------------------------------------------------------------
# Plan I/O and rendering
# ---------------------------------------------------------------------------


def make_tracks(
    programme_start: int,
    boundaries: Sequence[Boundary],
    programme_end: int,
) -> list[TrackRange]:
    points = [programme_start] + [b.sample for b in boundaries] + [programme_end]
    return [
        TrackRange(number=i + 1, start_sample=points[i], end_sample=points[i + 1])
        for i in range(len(points) - 1)
    ]


def silence_to_json(region: SilenceRegion, sample_rate: int) -> dict[str, object]:
    return {
        "start_sample": region.start_sample,
        "end_sample": region.end_sample,
        "start_seconds": samples_to_seconds(region.start_sample, sample_rate),
        "end_seconds": samples_to_seconds(region.end_sample, sample_rate),
        "span_seconds": samples_to_seconds(region.span_samples, sample_rate),
        "silent_seconds": samples_to_seconds(region.silent_samples, sample_rate),
        "interruption_seconds": samples_to_seconds(region.interruption_samples, sample_rate),
        "score": region.score(),
    }


def create_plan(
    source: Path,
    info: AudioInfo,
    args: argparse.Namespace,
    raw_silences: Sequence[SilenceRegion],
    merged_silences: Sequence[SilenceRegion],
    programme_start: int,
    programme_end: int,
    boundaries: Sequence[Boundary],
    tracks: Sequence[TrackRange],
    trimmed_start: bool,
    trimmed_end: bool,
) -> dict[str, object]:
    return {
        "schema": "flacsplit-plan",
        "version": 2,
        "source": asdict(info),
        "analysis": {
            "threshold_db": args.threshold,
            "minimum_silence_seconds": args.silence_duration,
            "merge_gap_seconds": args.merge_gap,
            "edge_tolerance_seconds": args.edge_tolerance,
            "start_pad_seconds": args.start_pad,
            "end_pad_seconds": args.end_pad,
            "requested_tracks": args.tracks,
            "requested_durations_seconds": args.durations,
            "duration_tolerance_seconds": args.tolerance,
            "minimum_track_duration_seconds": args.min_track_duration,
            "trimmed_leading_silence": trimmed_start,
            "trimmed_trailing_silence": trimmed_end,
        },
        "programme": {
            "start_sample": programme_start,
            "end_sample": programme_end,
            "start_seconds": samples_to_seconds(programme_start, info.sample_rate),
            "end_seconds": samples_to_seconds(programme_end, info.sample_rate),
        },
        "raw_silences": [silence_to_json(s, info.sample_rate) for s in raw_silences],
        "merged_silences": [silence_to_json(s, info.sample_rate) for s in merged_silences],
        "boundaries": [
            {
                "sample": b.sample,
                "seconds": samples_to_seconds(b.sample, info.sample_rate),
                "source": b.source,
                "expected_sample": b.expected_sample,
                "expected_seconds": (
                    samples_to_seconds(b.expected_sample, info.sample_rate)
                    if b.expected_sample is not None
                    else None
                ),
                "candidate_score": b.candidate_score,
            }
            for b in boundaries
        ],
        "tracks": [
            {
                "number": t.number,
                "filename": f"{t.number:02d}.flac",
                "start_sample": t.start_sample,
                "end_sample": t.end_sample,
                "start_seconds": samples_to_seconds(t.start_sample, info.sample_rate),
                "end_seconds": samples_to_seconds(t.end_sample, info.sample_rate),
                "duration_seconds": samples_to_seconds(
                    t.end_sample - t.start_sample, info.sample_rate
                ),
            }
            for t in tracks
        ],
    }


def write_plan(plan: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(plan, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temp, path)


def load_plan(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SplitError(f"could not read split plan {path}: {exc}") from exc

    if plan.get("schema") != "flacsplit-plan" or plan.get("version") != 2:
        raise SplitError(f"unsupported split plan format: {path}")
    return plan


def tracks_from_plan(plan: dict[str, object]) -> list[TrackRange]:
    """
    Rebuild track ranges from the authoritative programme edges + boundaries.

    The duplicated ``tracks`` array in the JSON is intentionally descriptive,
    not authoritative. This makes a generated plan easy for another program to
    edit: change programme.start_sample / programme.end_sample and/or the
    boundary ``sample`` values, then render with --from-plan.
    """
    try:
        programme = plan["programme"]
        assert isinstance(programme, dict)
        start_sample = int(programme["start_sample"])
        end_sample = int(programme["end_sample"])
        entries = plan["boundaries"]
        assert isinstance(entries, list)
        samples = [int(entry["sample"]) for entry in entries]
    except (KeyError, TypeError, ValueError, AssertionError) as exc:
        raise SplitError("split plan contains invalid programme/boundary data") from exc

    points = [start_sample, *samples, end_sample]
    if points != sorted(points) or len(points) != len(set(points)):
        raise SplitError("split plan boundaries are not strictly increasing")
    if start_sample < 0 or end_sample <= start_sample:
        raise SplitError("split plan contains invalid programme edges")

    return [
        TrackRange(number=i + 1, start_sample=points[i], end_sample=points[i + 1])
        for i in range(len(points) - 1)
    ]


def sample_format_args(info: AudioInfo) -> list[str]:
    # FFmpeg's FLAC encoder accepts s16 and s32. A 24-bit FLAC is represented as
    # s32 with bits_per_raw_sample=24 and is preserved by this path.
    if info.sample_fmt in {"s16", "s32"}:
        return ["-sample_fmt", info.sample_fmt]
    if info.bits_per_raw_sample is not None:
        return ["-sample_fmt", "s16" if info.bits_per_raw_sample <= 16 else "s32"]
    return []


def render_tracks(
    source: Path,
    info: AudioInfo,
    tracks: Sequence[TrackRange],
    output_dir: Path,
    compression_level: int,
    overwrite: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    outputs = [output_dir / f"{track.number:02d}.flac" for track in tracks]
    existing = [p for p in outputs if p.exists()]
    if existing and not overwrite:
        raise SplitError(
            f"output already exists: {existing[0]} (use --overwrite to replace numbered outputs)"
        )

    count = len(tracks)
    split_labels = "".join(f"[a{i}]" for i in range(count))
    filters = [f"[0:a:0]asplit={count}{split_labels}"]

    for i, track in enumerate(tracks):
        filters.append(
            f"[a{i}]atrim=start_sample={track.start_sample}:end_sample={track.end_sample},"
            f"asetpts=PTS-STARTPTS[out{i}]"
        )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y" if overwrite else "-n",
        "-i",
        str(source),
        "-filter_complex",
        ";".join(filters),
    ]

    fmt_args = sample_format_args(info)
    for i, output in enumerate(outputs):
        cmd.extend(
            [
                "-map",
                f"[out{i}]",
                "-map_metadata",
                "-1",
                "-c:a",
                "flac",
                "-compression_level",
                str(compression_level),
                *fmt_args,
                str(output),
            ]
        )

    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SplitError("FFmpeg failed while rendering output tracks")

    validate_outputs(outputs, info, tracks)


def validate_outputs(
    outputs: Sequence[Path],
    source_info: AudioInfo,
    tracks: Sequence[TrackRange],
) -> None:
    for output, track in zip(outputs, tracks, strict=True):
        info = probe_audio(output)
        expected_samples = track.end_sample - track.start_sample
        if info.sample_rate != source_info.sample_rate:
            raise SplitError(
                f"output validation failed for {output}: sample rate changed "
                f"({source_info.sample_rate} -> {info.sample_rate})"
            )
        if info.total_samples != expected_samples:
            raise SplitError(
                f"output validation failed for {output}: expected {expected_samples} samples, "
                f"got {info.total_samples}"
            )
        if (
            source_info.bits_per_raw_sample is not None
            and info.bits_per_raw_sample is not None
            and info.bits_per_raw_sample != source_info.bits_per_raw_sample
        ):
            raise SplitError(
                f"output validation failed for {output}: bit depth changed "
                f"({source_info.bits_per_raw_sample} -> {info.bits_per_raw_sample})"
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite value >= 0")
    return number


def compression_level(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 12:
        raise argparse.ArgumentTypeError("must be between 0 and 12")
    return number


def build_parser() -> argparse.ArgumentParser:
    epilog = """examples:
  # Automatic silence-based split
  %(prog)s side-a.flac

  # Expect exactly four tracks; choose the best three detected gaps
  %(prog)s ep.flac --tracks 4

  # Use approximate published durations and snap near them to real gaps
  %(prog)s ep.flac --durations 3:56,5:46,3:46,3:18

  # Precise/continuous material: use timings directly, with no silence snapping
  %(prog)s mix.flac --durations 4:12.3,3:47.8,5:03.1 --tolerance 0

  # Analyze/write JSON plan only; render it later (or edit boundary samples first)
  %(prog)s album.flac --tracks 8 --plan-only
  %(prog)s --from-plan album.split.json

notes:
  --durations implies the track count. Boundaries are calculated sequentially.
  With the default --tolerance=20, each expected boundary may snap to a detected
  silence within +/-20 seconds. If none exists, the expected time is used as a
  fallback, which is useful for continuous material.

  Leading/trailing silence is the only audio discarded. --start-pad and
  --end-pad retain audio around the detected programme edges to protect onsets
  and low-level fade-outs. Internal silence is never removed: every sample from
  programme start to programme end belongs to exactly one output track.

  FLAC is re-encoded because arbitrary sample-accurate cuts cannot safely rely
  on codec-frame copying. FLAC encoding remains lossless; sample rate and bit
  depth are validated after export. No DSP, resampling, normalization, fades,
  or metadata tagging is performed.
"""

    parser = argparse.ArgumentParser(
        prog="flacsplit.py",
        description=(
            "Split a continuous recording into numbered, sample-accurate FLAC tracks "
            "using silence candidates plus optional track-count/duration constraints."
        ),
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("input", nargs="?", type=Path, help="source audio file")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {VERSION}"
    )

    guidance = parser.add_argument_group("boundary guidance")
    guidance.add_argument(
        "--tracks",
        type=int,
        help=(
            "expected number of tracks; selects exactly N-1 detected internal gaps "
            "when --durations is not supplied"
        ),
    )
    guidance.add_argument(
        "--durations",
        type=parse_durations,
        metavar="LIST",
        help=(
            "comma-separated track durations; accepts seconds, MM:SS, or HH:MM:SS "
            "(example: 3:56,5:46,3:46,3:18)"
        ),
    )
    guidance.add_argument(
        "--tolerance",
        type=positive_float,
        default=DEFAULT_TOLERANCE,
        metavar="SEC",
        help=(
            f"duration-guided silence search radius (default: {DEFAULT_TOLERANCE:g}s); "
            "0 uses supplied durations exactly"
        ),
    )
    guidance.add_argument(
        "--min-track-duration",
        type=positive_float,
        default=DEFAULT_MIN_TRACK,
        metavar="SEC",
        help=(
            f"minimum allowed track length when choosing candidates (default: {DEFAULT_MIN_TRACK:g}s)"
        ),
    )

    detection = parser.add_argument_group("silence detection")
    detection.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD_DB,
        metavar="DB",
        help=f"FFmpeg silence threshold in dBFS (default: {DEFAULT_THRESHOLD_DB:g})",
    )
    detection.add_argument(
        "--silence-duration",
        type=positive_float,
        default=DEFAULT_MIN_SILENCE,
        metavar="SEC",
        help=(
            f"minimum silence reported by FFmpeg (default: {DEFAULT_MIN_SILENCE:g}s)"
        ),
    )
    detection.add_argument(
        "--merge-gap",
        type=positive_float,
        default=DEFAULT_MERGE_GAP,
        metavar="SEC",
        help=(
            "merge detected silence regions interrupted by at most this much sound "
            f"(default: {DEFAULT_MERGE_GAP:g}s)"
        ),
    )

    edges = parser.add_argument_group("programme edges")
    edges.add_argument(
        "--start-pad",
        type=positive_float,
        default=DEFAULT_START_PAD,
        metavar="SEC",
        help=(
            f"retain this much audio before detected programme onset (default: {DEFAULT_START_PAD:g}s)"
        ),
    )
    edges.add_argument(
        "--end-pad",
        type=positive_float,
        default=DEFAULT_END_PAD,
        metavar="SEC",
        help=(
            f"retain this much audio after detected programme end/fade (default: {DEFAULT_END_PAD:g}s)"
        ),
    )
    edges.add_argument(
        "--edge-tolerance",
        type=positive_float,
        default=DEFAULT_EDGE_TOLERANCE,
        metavar="SEC",
        help=(
            "a silence may begin/end this far from the physical file edge and still "
            f"count as leading/trailing silence (default: {DEFAULT_EDGE_TOLERANCE:g}s)"
        ),
    )

    output = parser.add_argument_group("output / plan")
    output.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="output directory (default: <input-stem>_tracks next to source)",
    )
    output.add_argument(
        "--plan",
        type=Path,
        help="JSON split-plan path (default: <input-stem>.split.json next to source)",
    )
    output.add_argument(
        "--plan-only",
        action="store_true",
        help="analyze and write the split plan, but do not create FLAC files",
    )
    output.add_argument(
        "--from-plan",
        type=Path,
        metavar="JSON",
        help=(
            "render a previously generated/edited v2 JSON plan; programme edges and "
            "boundary sample positions are authoritative"
        ),
    )
    output.add_argument(
        "--compression-level",
        type=compression_level,
        default=DEFAULT_COMPRESSION_LEVEL,
        metavar="0..12",
        help=(
            f"FFmpeg FLAC compression level (default: {DEFAULT_COMPRESSION_LEVEL}); "
            "all levels are lossless"
        ),
    )
    output.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing numbered output FLAC files",
    )

    return parser


def print_plan_summary(
    info: AudioInfo,
    programme_start: int,
    programme_end: int,
    boundaries: Sequence[Boundary],
    tracks: Sequence[TrackRange],
    plan_path: Path,
) -> None:
    print(f"Source:       {Path(info.path).name}")
    bits = f"{info.bits_per_raw_sample}-bit" if info.bits_per_raw_sample else info.sample_fmt
    print(
        f"Audio:        {info.sample_rate} Hz, {bits}, {info.channels} channel(s), {info.codec}"
    )
    print(f"Duration:     {format_time(info.duration_seconds)}")
    print(
        "Programme:    "
        f"{format_time(samples_to_seconds(programme_start, info.sample_rate))} -> "
        f"{format_time(samples_to_seconds(programme_end, info.sample_rate))}"
    )
    print(f"Tracks:       {len(tracks)}")
    print()

    for index, boundary in enumerate(boundaries, start=1):
        actual = samples_to_seconds(boundary.sample, info.sample_rate)
        detail = boundary.source
        if boundary.expected_sample is not None:
            expected = samples_to_seconds(boundary.expected_sample, info.sample_rate)
            delta = actual - expected
            detail += f", expected {format_time(expected)}, delta {delta:+.3f}s"
        print(f"CUT {index:02d}:       {format_time(actual)}  [{detail}]")

    if boundaries:
        print()

    for track in tracks:
        duration = samples_to_seconds(
            track.end_sample - track.start_sample, info.sample_rate
        )
        print(
            f"{track.number:02d}.flac      "
            f"{format_time(samples_to_seconds(track.start_sample, info.sample_rate))} -> "
            f"{format_time(samples_to_seconds(track.end_sample, info.sample_rate))}  "
            f"({format_time(duration)})"
        )

    print()
    print(f"Plan:         {plan_path}")


def execute_from_plan(args: argparse.Namespace) -> int:
    plan_path = args.from_plan.resolve()
    plan = load_plan(plan_path)

    plan_source = Path(str(plan.get("source", {}).get("path", "")))
    source = args.input.resolve() if args.input else plan_source
    if not source.exists():
        raise SplitError(
            f"source audio not found: {source}\n"
            "provide the source explicitly before --from-plan if it has moved"
        )

    info = probe_audio(source)
    try:
        planned_source = plan["source"]
        planned_rate = int(planned_source["sample_rate"])
        planned_total = int(planned_source["total_samples"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SplitError("split plan contains invalid source information") from exc

    if info.sample_rate != planned_rate or info.total_samples != planned_total:
        raise SplitError(
            "source does not match split plan (sample rate or total sample count differs)"
        )

    tracks = tracks_from_plan(plan)
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else source.parent / f"{source.stem}_tracks"
    )

    render_tracks(
        source,
        info,
        tracks,
        output_dir,
        args.compression_level,
        args.overwrite,
    )
    print(f"Rendered:     {len(tracks)} track(s) -> {output_dir}")
    return 0


def execute_analysis(args: argparse.Namespace) -> int:
    if args.input is None:
        raise SplitError("input audio file is required unless --from-plan is used")

    source = args.input.resolve()
    if not source.is_file():
        raise SplitError(f"input file not found: {source}")
    if args.tracks is not None and args.tracks < 1:
        raise SplitError("--tracks must be at least 1")
    if args.silence_duration <= 0:
        raise SplitError("--silence-duration must be > 0")
    if args.min_track_duration <= 0:
        raise SplitError("--min-track-duration must be > 0")
    if not math.isfinite(args.threshold):
        raise SplitError("--threshold must be finite")

    info = probe_audio(source)
    raw = detect_silences(source, info, args.threshold, args.silence_duration)
    merged = merge_silences(
        raw,
        seconds_to_samples(args.merge_gap, info.sample_rate),
    )

    programme_start, programme_end, internal, trimmed_start, trimmed_end = (
        determine_programme_edges(
            merged,
            info,
            args.start_pad,
            args.end_pad,
            args.edge_tolerance,
        )
    )

    boundaries = build_boundaries(
        internal,
        info,
        programme_start,
        programme_end,
        args.tracks,
        args.durations,
        args.tolerance,
        args.min_track_duration,
    )
    boundaries.sort(key=lambda b: b.sample)
    validate_boundaries(boundaries, programme_start, programme_end)
    track_ranges = make_tracks(programme_start, boundaries, programme_end)

    plan_path = (
        args.plan.resolve()
        if args.plan
        else source.with_name(f"{source.stem}.split.json")
    )
    plan = create_plan(
        source,
        info,
        args,
        raw,
        merged,
        programme_start,
        programme_end,
        boundaries,
        track_ranges,
        trimmed_start,
        trimmed_end,
    )
    write_plan(plan, plan_path)

    print_plan_summary(
        info,
        programme_start,
        programme_end,
        boundaries,
        track_ranges,
        plan_path,
    )

    if args.plan_only:
        return 0

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else source.parent / f"{source.stem}_tracks"
    )
    render_tracks(
        source,
        info,
        track_ranges,
        output_dir,
        args.compression_level,
        args.overwrite,
    )
    print(f"Rendered:     {len(track_ranges)} track(s) -> {output_dir}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        require_binary("ffmpeg")
        require_binary("ffprobe")

        if args.from_plan is not None:
            # Analysis-only options are intentionally ignored in plan-render mode;
            # the plan is the boundary authority.
            return execute_from_plan(args)
        return execute_analysis(args)
    except SplitError as exc:
        eprint(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        eprint("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
