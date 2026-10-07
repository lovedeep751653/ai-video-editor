#!/bin/bash
# Runs on the Android emulator: installs the APK, runs the app's self-test, saves screenshots.
# Every adb call has a time limit, so a frozen emulator ends the test (with its log) instead of hanging.
set -u
APK="$1"
PKG=com.lovedeep.aivideoeditor
mkdir -p out/test
a() { timeout "$1" adb "${@:2}"; }
echo "Installing $(du -h "$APK" | cut -f1) APK"
t=$(date +%s)
if ! a 900 install -r -g "$APK"; then
  # Fallback for very big apps: copy the file to the phone first, then install it there.
  echo "Direct install failed or took too long; trying copy-then-install"
  a 900 push "$APK" /data/local/tmp/app.apk && a 900 shell pm install -r -g /data/local/tmp/app.apk \
    || { echo "Install failed"; a 60 shell df -h /data; a 60 logcat -d > out/test/full-log.txt; exit 1; }
  a 60 shell rm -f /data/local/tmp/app.apk
fi
echo "Installed in $(( $(date +%s) - t )) s"
a 30 shell df -h /data || true
a 30 logcat -c
start=$(date +%s)
a 30 shell am start -n $PKG/.MainActivity --ez selftest true
sleep 20
a 30 exec-out screencap -p > out/test/1-start.png
result=""
while [ $(( $(date +%s) - start )) -lt 1500 ]; do
  log=$(a 60 logcat -d -s python.stdout:I python.stderr:W)
  if [ $? -eq 124 ]; then echo "Emulator stopped responding"; break; fi
  if echo "$log" | grep -q "SELFTEST: DONE"; then
    result=$(echo "$log" | grep "SELFTEST: DONE")
    break
  fi
  echo "$log" | grep "SELFTEST" | tail -1
  sleep 15
done
a 120 logcat -d -s python.stdout:I python.stderr:W Engine:* EditorPage:* AndroidRuntime:E ffmpeg-kit:* lowmemorykiller:* ActivityManager:I > out/test/log.txt
a 120 logcat -d > out/test/full-log.txt
grep "SELFTEST" out/test/log.txt || true
a 30 exec-out screencap -p > out/test/2-home.png
# Open the app again for a screenshot.
a 30 shell input keyevent KEYCODE_BACK; sleep 1
a 30 shell am start -n $PKG/.MainActivity; sleep 5
a 30 exec-out screencap -p > out/test/3-again.png
echo "Took $(( $(date +%s) - start )) s"
if [ -z "$result" ]; then echo "Self-test did not finish"; exit 1; fi
echo "$result" | grep -q "ALL PASSED" || { echo "Self-test failed"; exit 1; }
