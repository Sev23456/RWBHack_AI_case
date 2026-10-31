set -euo pipefail
version=$(sed -n 's/.*gradle-\([0-9.]*\)-bin.zip.*/\1/p' gradle/wrapper/gradle-wrapper.properties)
[[ "$version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]
curl -fsSL "https://services.gradle.org/distributions/gradle-${version}-bin.zip" -o /tmp/gradle.zip
unzip -q /tmp/gradle.zip -d /tmp
"/tmp/gradle-${version}/bin/gradle" --no-daemon wrapper --gradle-version "$version"
"/tmp/gradle-${version}/bin/gradle" --no-daemon assembleDebug testDebugUnitTest
emulator -avd qa -no-window -no-audio -no-boot-anim -gpu swiftshader_indirect > /tmp/emulator.log 2>&1 &
adb wait-for-device
for i in $(seq 1 120); do
  [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ] && break
  sleep 2
done
[ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ]
apk=$(find app/build/outputs/apk/debug -name '*.apk' | head -1)
adb install -r "$apk"
package=$(aapt dump badging "$apk" | sed -n "s/package: name='\([^']*\)'.*/\1/p")
adb logcat -c
adb shell monkey -p "$package" -c android.intent.category.LAUNCHER 1
sleep 5
adb shell pidof "$package"
! adb logcat -d -s AndroidRuntime:E | grep 'FATAL EXCEPTION'
printf '{"started":true}\n' > startup.json
sleep infinity
