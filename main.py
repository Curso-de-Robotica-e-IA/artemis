from artemis import ArtemisRunner
import time
# Target a specific device or omit device_serial to use the first attached device
with ArtemisRunner(device_serial="NMQR240198", profile="flash") as bot:
    # Basic task execution
    start_time = time.time()
    result = bot.run("Open the camera app, turn the flash on, then turn it off, and finally select the automatic option.", return_steps=True)
    end_time = time.time()
    print(f"Execution time: {end_time - start_time:.2f} seconds")

for step in result["steps"]:                                                                                                                                                                
    print(f"\nPasso {step['step_number']}: {step['command']}")                                                                                                                                
    print(f"  Coordenadas (0-1000): {step['coordinates']}")                                                                                                                                   
    print(f"  Pixels reais na tela: {step['pixel_coordinates']}")                                                                                                                             
    print(f"  Argumentos: {step['args']}")                                                                                                                                                    
    print(f"  Status da ação: {step['status']}")