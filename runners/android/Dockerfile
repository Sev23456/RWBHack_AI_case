FROM eclipse-temurin:17-jdk-jammy
RUN apt-get update && apt-get install -y curl unzip libpulse0 libnss3 libxcb1 libx11-6 libxcomposite1 libxcursor1 libxi6 libxtst6 libxrandr2 libasound2 libglu1-mesa && rm -rf /var/lib/apt/lists/*
ENV ANDROID_HOME=/opt/android-sdk
ENV PATH=/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:/opt/android-sdk/build-tools/35.0.0:$PATH
RUN mkdir -p /opt/android-sdk/cmdline-tools && curl -fsSL https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -o /tmp/sdk.zip && unzip -q /tmp/sdk.zip -d /opt/android-sdk/cmdline-tools && mv /opt/android-sdk/cmdline-tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest
RUN yes | sdkmanager --licenses >/dev/null; sdkmanager 'platform-tools' 'emulator' 'platforms;android-35' 'platforms;android-36' 'build-tools;35.0.0' 'system-images;android-35;google_apis;x86_64'
RUN echo no | avdmanager create avd -n qa -k 'system-images;android-35;google_apis;x86_64'
