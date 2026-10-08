#!/bin/sh
# Builds the APK and, if Gradle fails, says WHY in a GitHub annotation (visible in the failure
# email, on the run page and through the API) instead of just "exit code 1".
# A failure caused by the build machine (out of memory, out of disk, a download that dropped)
# is retried once; a real code error is not, because retrying it would only waste time.
#
# Usage: gradle-build.sh <gradle args...>      (run from the android/ folder)
set -u
LOG="${GRADLE_LOG:-$(mktemp)}"

machine() {  # one line: free disk and memory on the build machine
  disk=$(df -h . | awk 'NR==2 {print $4 " free of " $2}')
  mem=$(free -m 2>/dev/null | awk '/^Mem:/ {print $7 " MB of " $2 " MB"}')
  echo "disk ${disk:-?}, memory available ${mem:-?}"
}

annotate() {  # level title message (multi-line message allowed)
  msg=$(printf '%s' "$3" | sed 's/%/%25/g' | awk 'BEGIN{ORS="%0A"} {print}')
  echo "::$1 title=$2::$msg"
}

reason() {  # classify the failure from Gradle's output and the kernel log
  if grep -qiE "OutOfMemoryError|Java heap space|GC overhead limit|Metaspace" "$LOG" \
     || dmesg 2>/dev/null | grep -qiE "out of memory|oom-kill"; then
    echo "out-of-memory"
  elif grep -qiE "No space left on device|not enough space|disk.*full" "$LOG"; then
    echo "out-of-disk"
  elif grep -qiE "Could not (resolve|download|GET|HEAD)|Connection (reset|refused|timed out)|Read timed out|Remote host terminated|502 Bad Gateway|503 Service|Gradle build daemon disappeared|daemon.*(crashed|disappeared)" "$LOG"; then
    echo "machine-or-network"
  else
    echo "code"
  fi
}

details() {  # the part of Gradle's output that explains the failure
  { grep -E -A12 "What went wrong" "$LOG" | head -30
    grep -iE "OutOfMemoryError|No space left|e: file|error:" "$LOG" | head -10; } | sed '/^$/d' | head -40
}

attempt=1
echo "Build machine before attempt $attempt: $(machine)"
gradle "$@" > "$LOG" 2>&1; status=$?
cat "$LOG"
[ $status -eq 0 ] && { annotate notice "APK built" "Build machine after the build: $(machine)"; exit 0; }

why=$(reason)
first=$(details)
if [ "$why" = "code" ]; then
  annotate error "APK build failed: error in the code" "$first"
  exit 1
fi

annotate warning "APK build failed once ($why), retrying" "Build machine: $(machine)
$first"
# Give the next attempt a clean start: stop Gradle daemons and drop half-written outputs.
gradle --stop > /dev/null 2>&1 || true
rm -rf app/build/intermediates/merged_assets app/build/intermediates/compressed_assets \
       app/build/intermediates/apk app/build/outputs/apk 2>/dev/null || true
attempt=2
echo "Build machine before attempt $attempt: $(machine)"
gradle "$@" > "$LOG" 2>&1; status=$?
cat "$LOG"
if [ $status -eq 0 ]; then
  annotate notice "APK built on the second try" "First try failed with: $why. Build machine now: $(machine)"
  exit 0
fi
annotate error "APK build failed twice ($(reason))" "Build machine: $(machine)
$(details)"
exit 1
