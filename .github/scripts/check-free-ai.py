"""Fetches one real picture from each free AI picture service, to show they work from the internet."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
from engine import freeai, genai  # noqa: E402

out = Path("out/free-ai")
out.mkdir(parents=True, exist_ok=True)
for name, fn in (("pollinations", freeai._pollinations), ("horde", freeai._horde)):
    t = time.time()
    try:
        data = fn("a golden retriever running on a beach at sunset", "9:16", 7)
        (out / f"{name}{freeai._is_image(data)}").write_bytes(data)
        print(f"OK {name}: {len(data)} bytes in {time.time() - t:.0f}s")
    except Exception as e:  # noqa: BLE001
        print(f"FAILED {name} after {time.time() - t:.0f}s: {e}")

t = time.time()
try:
    print("OK text:", genai.interpret(genai.FREE, "free", "make it vertical, cinematic, 30 seconds, title 'Goa'"),
          f"in {time.time() - t:.0f}s")
except Exception as e:  # noqa: BLE001
    print(f"FAILED text after {time.time() - t:.0f}s: {e}")
