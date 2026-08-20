from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.utils.unity_bridge import UnityBridgeClient


class UnityBridgeClientDiagnosticsTests(unittest.TestCase):
    def _respond_once(self, bridge_dir: Path, fields: dict[str, str]) -> threading.Thread:
        request_dir = bridge_dir / "requests"
        response_dir = bridge_dir / "responses"

        def worker() -> None:
            deadline = time.monotonic() + 2
            request_path: Path | None = None
            while time.monotonic() < deadline:
                requests = list(request_dir.glob("*.request"))
                if requests:
                    request_path = requests[0]
                    break
                time.sleep(0.005)
            if request_path is None:
                return

            request = {}
            for line in request_path.read_text(encoding="utf-8").splitlines():
                key, value = line.split("=", 1)
                request[key] = value

            response = {
                "protocol": "2",
                "id": request["id"],
                **fields,
            }
            temporary_path = response_dir / f"{request['id']}.response.tmp"
            response_path = response_dir / f"{request['id']}.response"
            temporary_path.write_text(
                "".join(f"{key}={value}\n" for key, value in response.items()),
                encoding="utf-8",
            )
            os.replace(temporary_path, response_path)

        thread = threading.Thread(target=worker)
        thread.start()
        return thread

    def test_failed_click_retains_bridge_response_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge_dir = Path(directory)
            client = UnityBridgeClient(str(bridge_dir))
            responder = self._respond_once(
                bridge_dir,
                {
                    "ok": "0",
                    "status": "no-click-handler",
                    "message": "No IPointerClickHandler was found.",
                    "screenWidth": "1920",
                    "screenHeight": "1080",
                    "unityX": "370.5",
                    "unityY": "546",
                    "hitCount": "2",
                    "targetPath": "",
                },
            )

            self.assertFalse(client.click(247, 356))
            responder.join(timeout=2)

            result = client.last_result
            self.assertEqual(result["_action"], "click")
            self.assertEqual(result["status"], "no-click-handler")
            self.assertEqual(result["hitCount"], "2")
            self.assertEqual(result["_requestState"], "responded")
            description = client.describe_last_result()
            self.assertIn("status='no-click-handler'", description)
            self.assertIn("hitCount='2'", description)
            self.assertIn("x=247,y=356", description)

    def test_timeout_retains_request_state_and_cleans_pending_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = UnityBridgeClient(directory)

            self.assertFalse(client.click(247, 356, timeout_ms=10))

            result = client.last_result
            self.assertEqual(result["_action"], "click")
            self.assertEqual(result["status"], "timeout")
            self.assertEqual(result["_requestState"], "pending")
            self.assertEqual(list((Path(directory) / "requests").glob("*.request")), [])
            description = client.describe_last_result()
            self.assertIn("status='timeout'", description)
            self.assertIn("requestState='pending'", description)


if __name__ == "__main__":
    unittest.main()
