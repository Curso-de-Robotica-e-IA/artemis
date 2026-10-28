from artemis import ArtemisRunner
import time
# Target a specific device or omit device_serial to use the first attached device
with ArtemisRunner(device_serial="N0OJ460182", profile="flash") as bot:
    # Basic task execution
    start_time = time.time()
    result = bot.run("Go to settings and turn on the airplane mode", output="Text Summary")
    print(result)
    end_time = time.time()
    print(f"Execution time: {end_time - start_time:.2f} seconds")