from artemis import ArtemisRunner

# Target a specific device or omit device_serial to use the first attached device
with ArtemisRunner(device_serial="ZF5246RQKB", profile="flash") as bot:
    # Basic task execution
    result = bot.run("Open Settings and check Wi-Fi connection status", output="Text Summary")
    print(result)