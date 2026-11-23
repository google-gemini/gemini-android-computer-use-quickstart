# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-genai",
# ]
# ///

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time

from google import genai

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def setup_android_env():
    paths_to_check = [
        os.environ.get("ANDROID_HOME"),
        "/opt/homebrew/share/android-commandlinetools",
        "/usr/local/share/android-commandlinetools",
    ]
    
    android_home = None
    for p in paths_to_check:
        if p and os.path.exists(p):
            android_home = p
            break
            
    if not android_home:
        print("Error: ANDROID_HOME not found. Run setup_emulator.sh first.", file=sys.stderr)
        sys.exit(1)
        
    os.environ["ANDROID_HOME"] = android_home
    sdk_paths = [
        os.path.join(android_home, "cmdline-tools", "latest", "bin"),
        os.path.join(android_home, "emulator"),
        os.path.join(android_home, "platform-tools"),
    ]
    
    current_path = os.environ.get("PATH", "")
    for p in sdk_paths:
        if p not in current_path:
            current_path = p + os.pathsep + current_path
            
    os.environ["PATH"] = current_path
    return android_home


def start_emulator(avd_name="AI_Agent_Phone"):
    setup_android_env()
    
    try:
        res = subprocess.run(["adb", "devices"], capture_output=True, text=True)
        if "emulator" in res.stdout:
            return
    except FileNotFoundError:
        pass

    print(f"Starting emulator '{avd_name}'...")
    log_file = open(os.path.join(BASE_DIR, "emulator.log"), "w")
    
    subprocess.Popen(
        ["emulator", "-avd", avd_name, "-delay-adb"],
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )
    
    print("Waiting for emulator to boot...")
    for _ in range(60):
        try:
            res = subprocess.run(["adb", "devices"], capture_output=True, text=True)
            if "emulator" in res.stdout:
                boot_res = subprocess.run(
                    ["adb", "shell", "getprop", "sys.boot_completed"],
                    capture_output=True,
                    text=True,
                )
                if boot_res.stdout.strip() == "1":
                    print("Emulator ready.")
                    return
        except Exception:
            pass
        time.sleep(2)
        
    print("Error: Emulator failed to boot.", file=sys.stderr)
    sys.exit(1)


class ADBBridge:
    def __init__(self, device_id=None):
        self.prefix = ["adb"] + (["-s", device_id] if device_id else [])
        self.width, self.height = self._screen_size()

    def _run(self, args, check=True):
        result = subprocess.run(self.prefix + args, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(f"ADB error: {result.stderr.strip()}")
        return result.stdout

    def _screen_size(self):
        output = self._run(["shell", "wm", "size"])
        match = re.search(r"Physical size: (\d+)x(\d+)", output)
        return (int(match.group(1)), int(match.group(2))) if match else (1080, 1920)

    def _px(self, x, y):
        return int(x / 1000 * self.width), int(y / 1000 * self.height)

    def click(self, y, x, **_):
        px, py = self._px(x, y)
        self._run(["shell", "input", "tap", str(px), str(py)])

    def type(self, text, press_enter=False, **_):
        self._run(["shell", "input", "text", text.replace(" ", "%s")])
        if press_enter:
            self._run(["shell", "input", "keyevent", "66"])

    def open_app(self, app_name=None, package_name=None, **_):
        pkg = app_name or package_name
        if not pkg:
            raise ValueError("open_app requires app_name or package_name")
            
        stdout = self._run(["shell", "monkey", "--pct-syskeys", "0", "-p", pkg,
                            "-c", "android.intent.category.LAUNCHER", "1"], check=False)
        if "No activities found" in stdout or "monkey aborted" in stdout:
            raise RuntimeError(f"App {pkg} is not installed or has no launcher activity.")

    def long_press(self, y, x, seconds=2, **_):
        px, py = self._px(x, y)
        self._run(["shell", "input", "swipe", str(px), str(py),
                   str(px), str(py), str(seconds * 1000)])

    def drag_and_drop(self, start_y, start_x, end_y, end_x, **_):
        sx, sy = self._px(start_x, start_y)
        ex, ey = self._px(end_x, end_y)
        self._run(["shell", "input", "swipe", str(sx), str(sy),
                   str(ex), str(ey), "300"])

    def press_key(self, key, **_):
        keymap = {"home": "3", "back": "4", "enter": "66",
                  "app_switch": "187", "menu": "82"}
        self._run(["shell", "input", "keyevent", keymap.get(key.lower(), key)])

    def go_back(self, **_):
        self._run(["shell", "input", "keyevent", "4"])

    def wait(self, seconds=1, **_):
        time.sleep(seconds)

    def list_apps(self, **_):
        output = self._run(["shell", "pm", "list", "packages", "-3"])
        apps = [l.split(":")[1] for l in output.splitlines() if l.startswith("package:")]
        if not apps:
            return {"apps": "No third-party apps installed on this device."}
        return {"apps": apps}

    def take_screenshot(self, **_):
        return None

    def screenshot(self) -> bytes:
        result = subprocess.run(
            self.prefix + ["exec-out", "screencap", "-p"], capture_output=True
        )
        return result.stdout



SYSTEM_PROMPT = """You are operating an Android phone.
* Use the provided tools to complete the task.
* Scroll down to inspect the full screen before assuming an element is missing.
* You can open apps by package name from anywhere.
* Type text only using the `type` tool. Do not use the virtual keyboard.
* If the task is already complete, state that directly.
"""


def run_agent(
    task: str,
    device_id: str = None,
    model: str = "gemini-4-hydrogen",
    thinking_level: str = "medium",
    max_turns: int = 100,
):
    start_emulator()
    client = genai.Client()
    bridge = ADBBridge(device_id)

    print(f"\nTask: {task}")
    print(f"Model: {model}")
    print(f"Thinking Level: {thinking_level}")
    print("-" * 40)

    screenshot_bytes = bridge.screenshot()
    user_input = [
        {"type": "text", "text": task},
        {
            "type": "image",
            "data": base64.b64encode(screenshot_bytes).decode(),
            "mime_type": "image/png",
        },
    ]

    previous_interaction_id = None
    turn = 0

    while turn < max_turns:
        turn += 1
        generation_config = {"thinking_level": thinking_level} if thinking_level else None

        interaction = client.interactions.create(
            model=model,
            system_instruction=SYSTEM_PROMPT,
            input=user_input,
            tools=[{"type": "computer_use", "environment": "mobile"}],
            generation_config=generation_config,
            previous_interaction_id=previous_interaction_id,
        )

        function_calls = [step for step in interaction.steps if step.type == "function_call"]
        if not function_calls:
            print("Agent finished:", interaction.output_text)
            break

        function_responses = []
        for fc in function_calls:
            print(f"[function_call] {fc.name}({fc.arguments})")
            handler = getattr(bridge, fc.name, None)
            result_text = {"status": "ok"}

            if handler:
                try:
                    res = handler(**fc.arguments)
                    if isinstance(res, dict):
                        result_text.update(res)
                except Exception as e:
                    result_text = {"status": "error", "error": str(e)}
            else:
                result_text = {"status": "error", "error": f"Unknown action: {fc.name}"}

            print(f"[function_result] {result_text}")

            if "safety_decision" in fc.arguments:
                result_text["safety_acknowledgement"] = True

            screenshot_bytes = bridge.screenshot()

            fr = {
                "type": "function_result",
                "name": fc.name,
                "call_id": fc.id,
                "result": [
                    {"type": "text", "text": json.dumps(result_text)},
                    {
                        "type": "image",
                        "data": base64.b64encode(screenshot_bytes).decode(),
                        "mime_type": "image/png",
                    },
                ],
            }
            function_responses.append(fr)
            time.sleep(0.5)

        user_input = function_responses
        previous_interaction_id = interaction.id
            
    return interaction


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Android Computer Use AI Agent")
    parser.add_argument(
        "--model",
        "-m",
        default="gemini-4-hydrogen",
        help="Gemini model ID to use (default: gemini-4-hydrogen)",
    )
    parser.add_argument(
        "--thinking-level",
        "-t",
        default="medium",
        help="Thinking level for generation config (default: medium)",
    )
    parser.add_argument(
        "--device-id",
        "-d",
        default=None,
        help="ADB device ID (optional)",
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="Task description for the agent",
    )

    args = parser.parse_args()
    task_desc = (
        " ".join(args.task)
        if args.task
        else "Find the latest blog post from philipp schmid and summarize it."
    )
    run_agent(
        task=task_desc,
        device_id=args.device_id,
        model=args.model,
        thinking_level=args.thinking_level,
    )
