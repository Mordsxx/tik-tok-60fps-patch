# TikTok 60FPS Patch

Rewrites MP4 *timing metadata only*: a 60 fps clip is reinterpreted as 30 fps,
duration doubles, every encoded frame/audio sample stays byte-identical.

## Run
    python main.py                      # GUI
    python main.py in.mp4 [-o out.mp4] [--factor 2]   # CLI, same engine
    python tools/compare.py mine.mp4 reference.mp4    # compare against the reference app's output

## Build the EXE (Windows 10/11, 64-bit Python 3.13)
1. Optional: put `ffprobe.exe` next to `main.py` (bundled for independent validation).
2. Double-click `build_windows.bat` -> `dist\TikTok 60FPS Patch.exe` (one file, no console).
   No Windows machine? Push the folder to GitHub and run the *build-exe* workflow.

## Algorithm
1. Parse the box tree by file offset (32/64-bit sizes); `mdat` is never loaded.
2. Fragmented files (`moof`/`mvex`), truncated or corrupt files are rejected with a message.
3. For every track, with factor F (default 2):
   * `mdhd.timescale` T -> T/F when exact (e.g. 15360 -> 7680, 44100 -> 22050). `stts`, `ctts`,
     `mdhd.duration` and `elst.media_time` keep their values, so every timestamp is stretched by exactly F.
   * Otherwise (T not divisible) the timescale is kept and every `stts` delta, `ctts` offset,
     `mdhd.duration` and `elst.media_time` is multiplied by F (rejected if not an exact integer).
   * `tkhd.duration` and `elst.segment_duration` (movie timescale) are multiplied by F.
4. `mvhd.duration` is multiplied by F (movie timescale unchanged).
5. All changed fields are fixed-width, so no box changes size and no `stco/co64` offset moves:
   the output is the input file copied byte-for-byte with those integers overwritten in place.
6. Rational arithmetic (`fractions.Fraction`) is used throughout; nothing is rounded except movie-level
   durations (to the nearest movie tick).
7. Validation (output is deleted if any check fails): box structure re-parsed; output == input + exactly the
   planned edits (proves no re-encode); sample counts unchanged; per-track duration == old x F (exact);
   fps == old / F; identical scale on all tracks (A/V sync); FFprobe duration/frame/fps cross-check if available.

## Caveat
Audio is slowed by the same factor (as required for sync), so without re-encoding its pitch drops
(like a slowed record). Keeping pitch would need a re-encode, which this tool deliberately never does.
