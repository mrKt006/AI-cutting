from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

from ffmpeg_utils import require_tool, run
from safe_json import loads_json
from subtitle_layout import normalize_word_tokens


SUBMIT_URL = "https://openspeech.bytedance.com/api/v1/vc/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v1/vc/query"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit local video/audio to Volcengine ASR and export subtitle JSON")
    parser.add_argument("--video", default="input/video.mp4", help="input video/audio path")
    parser.add_argument("--output", default="input/asr_result.json", help="output JSON path for external subtitle mode")
    parser.add_argument("--appid", default=os.environ.get("VOLC_APP_ID"), help="Volcengine APP ID, or env VOLC_APP_ID")
    parser.add_argument("--token", default=os.environ.get("VOLC_ACCESS_TOKEN"), help="Volcengine Access Token, or env VOLC_ACCESS_TOKEN")
    parser.add_argument("--words-per-line", type=int, default=15, help="Volc subtitle words_per_line parameter")
    parser.add_argument("--max-lines", type=int, default=1, help="Volc subtitle max_lines parameter")
    parser.add_argument("--blocking", type=int, default=1, choices=[0, 1], help="query blocking mode")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="seconds between non-blocking polls")
    parser.add_argument("--timeout", type=float, default=600.0, help="max seconds to wait")
    parser.add_argument("--keep-audio", default=None, help="optional path to keep extracted wav")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if not args.appid:
            raise RuntimeError("Missing APP ID. Set VOLC_APP_ID or pass --appid")
        if not args.token:
            raise RuntimeError("Missing Access Token. Set VOLC_ACCESS_TOKEN or pass --token")

        require_tool("ffmpeg")
        source = Path(args.video)
        if not source.exists():
            raise FileNotFoundError(f"Missing input media: {source}")

        with tempfile.TemporaryDirectory(prefix="ai-cutting-volc-") as tmp:
            audio_path = Path(args.keep_audio) if args.keep_audio else Path(tmp) / "audio.wav"
            extract_wav(source, audio_path)
            task_id = submit_audio(
                audio_path=audio_path,
                appid=args.appid,
                token=args.token,
                words_per_line=args.words_per_line,
                max_lines=args.max_lines,
            )
            result = query_until_done(
                appid=args.appid,
                token=args.token,
                task_id=task_id,
                blocking=args.blocking,
                poll_interval=args.poll_interval,
                timeout=args.timeout,
            )
            segments = convert_utterances(result)
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"ASR result written to {output.resolve()}")
            print(f"Segments: {len(segments)}")
            return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


def extract_wav(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
    )


def submit_audio(audio_path: Path, appid: str, token: str, words_per_line: int, max_lines: int) -> str:
    params = urllib.parse.urlencode(
        {
            "appid": appid,
            "words_per_line": words_per_line,
            "max_lines": max_lines,
        }
    )
    data = audio_path.read_bytes()
    request = urllib.request.Request(
        f"{SUBMIT_URL}?{params}",
        data=data,
        method="POST",
        headers={
            "Content-Type": "audio/wav",
            "Authorization": f"Bearer;{token}",
            "Content-Length": str(len(data)),
        },
    )
    response = _json_request(request, source="Volc submit response")
    if response.get("code") != 0:
        raise RuntimeError(f"Volc submit failed: {response}")
    task_id = response.get("id")
    if not task_id:
        raise RuntimeError(f"Volc submit response missing id: {response}")
    return str(task_id)


def query_until_done(
    appid: str,
    token: str,
    task_id: str,
    blocking: int,
    poll_interval: float,
    timeout: float,
) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        params = urllib.parse.urlencode({"appid": appid, "id": task_id, "blocking": blocking})
        request = urllib.request.Request(
            f"{QUERY_URL}?{params}",
            method="GET",
            headers={"Authorization": f"Bearer;{token}"},
        )
        response = _json_request(request, source="Volc query response")
        code = response.get("code")
        if code == 0 and response.get("utterances"):
            return response
        if code not in (0, 2000, 2001, 2002):
            raise RuntimeError(f"Volc query failed: {response}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for Volc ASR task {task_id}: {response}")
        time.sleep(poll_interval)


def convert_utterances(response: dict) -> list[dict]:
    result: list[dict] = []
    for index, item in enumerate(response.get("utterances", []), start=1):
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        result.append(
            {
                "start_ms": int(item.get("start_time", 0)),
                "end_ms": int(item.get("end_time", 0)),
                "text": text,
                "tokens": normalize_word_tokens(item, index),
            }
        )
    return result


def _json_request(request: urllib.request.Request, source: str) -> dict:
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read().decode("utf-8")
    data = loads_json(body, source)
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid JSON from {source}: expected object")
    return data


if __name__ == "__main__":
    raise SystemExit(main())
