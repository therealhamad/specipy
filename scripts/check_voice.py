"""Make one real synthesis call against Maya and report exactly what came back.

    python -m scripts.check_voice
    python -m scripts.check_voice --save-fallback   # also writes the cached clip

Prints the resolved endpoint, the response content type, and the audio format,
so a shape change shows up here rather than mid-demo.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from orchestrator import config, voice

SAMPLE = (
    "Our payments provider changed how it reports the state of a payment, and "
    "the finance dashboard stopped counting completed payments as revenue. "
    "A fix has been made and verified against a test the agent could not modify."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", help="write the audio to this exact path")
    parser.add_argument(
        "--save-fallback",
        action="store_true",
        help="write to interface/static/audio/fallback.<ext> as the cached clip",
    )
    parser.add_argument("--text", default=SAMPLE)
    args = parser.parse_args(argv)

    url = voice.synthesis_url()
    if not url or not config.MAYA_API_KEY:
        print("MAYA_API_URL and MAYA_API_KEY must be set.", file=sys.stderr)
        return 2

    print(f"POST {url}")
    print(f"  model={config.MAYA_MODEL!r} voice={config.MAYA_VOICE or '(default)'!r} "
          f"language={config.MAYA_LANGUAGE!r}")

    try:
        response = httpx.post(
            url,
            json=voice.payload(args.text),
            headers={
                "Authorization": f"Bearer {config.MAYA_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )
    except Exception as exc:
        print(f"request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    content_type = response.headers.get("content-type", "")
    print(f"\nHTTP {response.status_code}")
    print(f"  content-type: {content_type}")
    print(f"  body bytes:   {len(response.content)}")
    if not content_type.startswith("audio/"):
        print(f"  body preview: {response.text[:600]}")

    if response.status_code >= 400:
        print(
            "\nThe endpoint rejected the request. If it names 'model', use one of the "
            "values it lists and set MAYA_MODEL accordingly.",
            file=sys.stderr,
        )
        return 1

    extracted = voice.audio_payload(response)
    if not extracted:
        print("\nCould not find audio in the response.", file=sys.stderr)
        return 1

    audio, extension = extracted
    print(f"\nExtracted {len(audio)} bytes -> {extension}")
    if extension == ".wav" and content_type.lower().startswith("audio/l16"):
        seconds = (len(audio) - 44) / 2 / 24000
        print(f"  raw PCM wrapped in a WAV container (~{seconds:.1f}s of audio)")

    targets = []
    if args.save:
        targets.append(args.save)
    if args.save_fallback:
        targets.append(os.path.join(config.AUDIO_DIR, f"fallback{extension}"))

    for target in targets:
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(audio)
        print(f"  saved {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
