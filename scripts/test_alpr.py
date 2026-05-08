#!/usr/bin/env python3
"""
scripts/test_alpr.py — Quick ALPR pipeline test

Usage:
    python scripts/test_alpr.py --image path/to/image.jpg
    python scripts/test_alpr.py --camera --frames 30
    python scripts/test_alpr.py --status          # just print engine status

Requires: pip install -r requirements-alpr.txt
Camera mode also requires: opencv-python (already in requirements.txt)
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

# Allow running from project root or from scripts/
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from modules.alpr_runner import ALPRRunner  # noqa: E402
from modules.multi_frame_voter import MultiFrameVoter  # noqa: E402


def _build_runner(conf: float, config_path: Optional[str] = None) -> ALPRRunner:
    cfg: dict = {"confidence_threshold": conf}
    if config_path:
        try:
            import yaml
            with open(config_path) as f:
                raw = yaml.safe_load(f)
            alpr_cfg = raw.get("alpr", {})
            cfg["yolo_model_path"] = alpr_cfg.get("yolo_model_path", "")
            cfg["models_dir"] = alpr_cfg.get("models_dir", "./data/models")
            cfg["yolo_confidence_threshold"] = alpr_cfg.get("yolo_confidence_threshold", 0.1)
        except Exception as exc:
            print(f"[warn] Could not read config: {exc}; using defaults")
    return ALPRRunner(cfg)


def _print_status(runner: ALPRRunner) -> None:
    s = runner.status_info()
    print(f"  Detector : {s.get('detector', '?')}", end="")
    if s.get("detector_error"):
        print(f"  →  {s['detector_error']}", end="")
    print()
    print(f"  OCR      : {s.get('ocr', '?')}", end="")
    if s.get("ocr_error"):
        print(f"  →  {s['ocr_error']}", end="")
    print()
    print(f"  Ready    : {s.get('ready', False)}")
    mode = s.get("mode")
    if mode:
        label = {
            "detector_ocr": "YOLO detector + OCR",
            "ocr_fallback": "OCR fallback only (works, but not dashcam-grade)",
            "unavailable": "unavailable",
        }.get(mode, str(mode))
        print(f"  Mode     : {label}")


def cmd_status(args: argparse.Namespace) -> None:
    runner = _build_runner(args.conf, args.config)
    runner.initialize()
    print("\n[ALPR Engine Status]")
    _print_status(runner)
    if not runner.is_ready:
        print(
            "\nNo ALPR engines available.\n"
            "Install deps with:  pip install -r requirements-alpr.txt\n"
            "Then set alpr.yolo_model_path in config.yaml (see README)."
        )


def cmd_image(args: argparse.Namespace) -> None:
    try:
        import cv2
    except ImportError:
        print("ERROR: opencv-python required. pip install opencv-python")
        sys.exit(1)

    runner = _build_runner(args.conf, args.config)
    runner.initialize()
    print("\n[ALPR Engine Status]")
    _print_status(runner)

    if not runner.is_ready:
        print(
            "\nNo ALPR engines available — install deps: pip install -r requirements-alpr.txt"
        )
        return

    frame = cv2.imread(args.image)
    if frame is None:
        print(f"ERROR: Could not read image: {args.image}")
        sys.exit(1)

    detections = runner.run_on_frame(frame)
    print(f"\n[Image: {args.image}]")
    print(f"Detections found: {len(detections)}")
    if not detections:
        print("  (none — try lowering --conf or check model path)")
    for d in detections:
        corrected_tag = " [corrected]" if d.get("corrected") else ""
        bbox = d.get("bbox")
        bbox_str = f"  bbox={bbox}" if bbox else "  bbox=full-frame"
        print(
            f"  {d['plate']}  conf={d['confidence']:.3f}"
            f"  src={d['source']}"
            f"  raw='{d['raw_text']}'{corrected_tag}{bbox_str}"
        )


def cmd_camera(args: argparse.Namespace) -> None:
    try:
        import cv2
    except ImportError:
        print("ERROR: opencv-python required. pip install opencv-python")
        sys.exit(1)

    runner = _build_runner(args.conf, args.config)
    runner.initialize()
    print("\n[ALPR Engine Status]")
    _print_status(runner)

    if not runner.is_ready:
        print(
            "\nNo ALPR engines available — install deps: pip install -r requirements-alpr.txt"
        )
        return

    voter = MultiFrameVoter(min_votes=2)
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("ERROR: Could not open camera (device 0). Check config or try a different index.")
        sys.exit(1)

    n = args.frames
    print(f"\nScanning {n} frames from camera (device 0)...")
    for i in range(n):
        ret, frame = cap.read()
        if not ret:
            print(f"  [warn] Frame read failed at {i+1}/{n}")
            break
        dets = runner.run_on_frame(frame)
        voter.add_frame(dets)
        if dets:
            plates = [d["plate"] for d in dets]
            print(f"  Frame {i+1:3d}/{n}: {plates}")

    cap.release()

    print("\n[Vote Results]")
    best = voter.get_best()
    if best:
        print(
            f"  Best: {best['plate']}  "
            f"score={best['confidence']:.3f}  "
            f"votes={best['votes']}/{n}"
        )
    else:
        print("  No plate detected with enough votes (min_votes=2)")

    all_cands = voter.all_candidates()
    if len(all_cands) > 1:
        print(f"\n  All qualifying candidates ({len(all_cands)}):")
        for c in all_cands:
            print(
                f"    {c['plate']}  score={c['confidence']:.3f}  "
                f"votes={c['votes']}  avg_conf={c['avg_confidence']:.3f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test the ALPR pipeline (YOLO + FastPlateOCR)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/test_alpr.py --status\n"
            "  python scripts/test_alpr.py --image plate.jpg\n"
            "  python scripts/test_alpr.py --camera --frames 60 --conf 0.4\n"
        ),
    )
    parser.add_argument("--image", metavar="PATH", help="Run on a single image file")
    parser.add_argument("--camera", action="store_true", help="Run on live camera feed")
    parser.add_argument("--status", action="store_true", help="Print engine status and exit")
    parser.add_argument(
        "--frames", type=int, default=30, help="Number of camera frames to scan (default: 30)"
    )
    parser.add_argument(
        "--conf", type=float, default=0.4, help="Confidence threshold (default: 0.4)"
    )
    parser.add_argument(
        "--config", metavar="PATH", default="config.yaml",
        help="Path to config.yaml (default: config.yaml)"
    )
    args = parser.parse_args()

    if args.status:
        cmd_status(args)
    elif args.image:
        cmd_image(args)
    elif args.camera:
        cmd_camera(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
