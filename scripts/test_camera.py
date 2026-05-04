#!/usr/bin/env python3
"""
test_camera.py -- List available cameras and test capture from a device.

Usage:
    python3 scripts/test_camera.py              # scan indices 0-9 and test default
    python3 scripts/test_camera.py --device 2   # test a specific device index
"""

import argparse
import sys
import platform

import cv2


def list_cameras(max_index: int = 10) -> list[int]:
    """Probe camera indices 0..max_index-1 and return those that open."""
    available = []
    for idx in range(max_index):
        if platform.system() == "Linux":
            cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
            if not cap.isOpened():
                cap = cv2.VideoCapture(idx)
        else:
            cap = cv2.VideoCapture(idx)

        if cap.isOpened():
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            backend = cap.getBackendName()
            print(f"  [OK]  index {idx}: {w}x{h} @ {fps:.0f}fps  backend={backend}")
            available.append(idx)
            cap.release()
        else:
            cap.release()

    return available


def test_capture(device_index: int, num_frames: int = 30) -> bool:
    """Open the device, capture num_frames frames, and report results."""
    print(f"\nTesting capture from device {device_index} ({num_frames} frames)...")

    if platform.system() == "Linux":
        cap = cv2.VideoCapture(device_index, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap = cv2.VideoCapture(device_index)
    else:
        cap = cv2.VideoCapture(device_index)

    if not cap.isOpened():
        print(f"  FAILED: Could not open device {device_index}")
        return False

    successes = 0
    for i in range(num_frames):
        ret, frame = cap.read()
        if ret and frame is not None:
            successes += 1

    cap.release()

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    print(f"  Captured {successes}/{num_frames} frames  ({w}x{h})")
    if successes == num_frames:
        print("  PASS: All frames captured successfully")
        return True
    elif successes > 0:
        print(f"  WARN: {num_frames - successes} dropped frames")
        return True
    else:
        print("  FAIL: No frames captured")
        return False


def main():
    parser = argparse.ArgumentParser(description="Test USB camera capture")
    parser.add_argument("--device", type=int, default=None, help="Device index to test")
    parser.add_argument("--frames", type=int, default=30, help="Number of frames to capture")
    parser.add_argument("--scan-max", type=int, default=10, help="Max index to scan")
    args = parser.parse_args()

    print("Scanning for cameras...")
    available = list_cameras(args.scan_max)

    if not available:
        print("\nNo cameras found.")
        sys.exit(1)

    print(f"\nFound {len(available)} camera(s): {available}")

    device = args.device if args.device is not None else available[0]
    ok = test_capture(device, args.frames)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
