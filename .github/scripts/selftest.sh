#!/bin/bash
# Runs on the Android emulator: installs the APK, runs the app's self-test, saves screenshots.
set -u
APK="$1"
PKG=com.lovedeep.aivideoeditor
mkdir -p out/test
adb install -r -g "$APK" || exit 1
adb logcat -c
start=$(date +%s)
adb shell am start -n $PKG/.MainActivity --ez selftest true
sleep 20
adb exec-out screencap -p > out/test/1-start.png
result=""
while [ $(( $(date +%s) - start )) -lt 1500 ]; do
  if adb logcat -d -s python.stdout:I python.stderr:W | grep -q "SELFTEST: DONE"; then
    result=$(adb logcat -d -s python.stdout:I | grep "SELFTEST: DONE")
    break
  fi
  sleep 10
done
adb logcat -d -s python.stdout:I python.stderr:W Engine:* EditorPage:* AndroidRuntime:E ffmpeg-kit:* > out/test/log.txt
adb logcat -d > out/test/full-log.txt
grep "SELFTEST" out/test/log.txt || true
adb exec-out screencap -p > out/test/2-home.png
# Open the newest video in the editor screen for a screenshot.
adb shell input keyevent KEYCODE_BACK; sleep 1
adb shell am start -n $PKG/.MainActivity; sleep 5
adb exec-out screencap -p > out/test/3-again.png
echo "Took $(( $(date +%s) - start )) s"
if [ -z "$result" ]; then echo "Self-test did not finish"; exit 1; fi
echo "$result" | grep -q "ALL PASSED" || { echo "Self-test failed"; exit 1; }
