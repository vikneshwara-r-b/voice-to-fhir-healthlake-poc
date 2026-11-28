#!/usr/bin/env python3
"""Generate the synthetic consult audio with Amazon Polly (no ffmpeg).

Polly returns 16 kHz signed 16-bit mono PCM (the lowest rate Transcribe Medical
accepts is 16 kHz); this script wraps it in WAV with the stdlib `wave` module and
adds a short silence between turns. Output: samples/audio/*.wav, published to
s3://<data bucket>/samples/ by the stack.

--stereo also writes <name>-stereo.wav with the clinician on the left channel and the
patient on the right (silence on the other side), the convention the app uses for
channel identification (Transcribe Medical) and ChannelDefinitions (AWS HealthScribe).

Usage:
    python3 scripts/generate_sample_audio.py [--profile NAME] [--region us-east-1] [--only consult-01] [--stereo]
"""

import argparse
import array
import sys
import wave
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from consult_scripts import CONSULTS  # noqa: E402

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # bytes (16-bit)
PAUSE_SECONDS = 0.6
OUT_DIR = Path(__file__).resolve().parents[1] / "samples" / "audio"


def synthesize(polly, text: str, voice: str) -> bytes:
    response = polly.synthesize_speech(
        Text=text, VoiceId=voice, Engine="neural", OutputFormat="pcm", SampleRate=str(SAMPLE_RATE)
    )
    return response["AudioStream"].read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--only", help="generate only files whose name starts with this")
    parser.add_argument("--stereo", action="store_true", help="also write a two-channel version")
    args = parser.parse_args()

    polly = boto3.Session(profile_name=args.profile, region_name=args.region).client("polly")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    silence = b"\x00" * int(SAMPLE_RATE * PAUSE_SECONDS) * SAMPLE_WIDTH

    for consult in CONSULTS:
        if args.only and not consult["file"].startswith(args.only):
            continue
        frames, left, right = bytearray(), array.array("h"), array.array("h")
        for speaker, text in consult["turns"]:
            pcm = synthesize(polly, text, consult["voices"][speaker]) + silence
            frames += pcm
            samples = array.array("h", pcm)
            quiet = array.array("h", bytes(len(pcm)))
            left.extend(samples if speaker == "C" else quiet)
            right.extend(quiet if speaker == "C" else samples)
        outputs = [(consult["file"], 1, bytes(frames))]
        if args.stereo:
            interleaved = array.array("h", bytes(len(left) * 2 * SAMPLE_WIDTH))
            interleaved[0::2], interleaved[1::2] = left, right
            outputs.append((consult["file"].replace(".wav", "-stereo.wav"), 2, interleaved.tobytes()))
        for name, channels, data in outputs:
            path = OUT_DIR / name
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(channels)
                wav.setsampwidth(SAMPLE_WIDTH)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(data)
            seconds = len(data) / (SAMPLE_RATE * SAMPLE_WIDTH * channels)
            print(f"{path.name}: {seconds:.0f} s, {channels} ch, {path.stat().st_size / 1e6:.1f} MB  [{consult['patient']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
