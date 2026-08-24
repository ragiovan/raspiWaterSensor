"""One-time WebREPL enable, run from the PC over USB. Not run on the device
itself -- this drives mpremote to push a small setup script to the Pico."""
import subprocess
import sys
import tempfile
import os


def main():
    password = input("Choose a WebREPL password (used for wireless deploys): ").strip()
    if not password:
        print("No password entered, aborting.")
        sys.exit(1)

    pass_line = f"PASS = {password!r}\n"
    device_script = f'''
cfg = {pass_line!r}
with open("webrepl_cfg.py", "w") as f:
    f.write(cfg)

try:
    with open("boot.py") as f:
        boot = f.read()
except OSError:
    boot = ""

if "webrepl" not in boot:
    with open("boot.py", "a") as f:
        f.write("\\nimport webrepl\\nwebrepl.start()\\n")
    print("boot.py updated to start WebREPL automatically")
else:
    print("boot.py already starts WebREPL")

print("webrepl_cfg.py written")
'''

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, newline="\n") as f:
        f.write(device_script)
        temp_path = f.name

    try:
        result = subprocess.run(["mpremote", "connect", "auto", "run", temp_path])
        if result.returncode != 0:
            print("Failed to run setup script on device. Is the Pico plugged in via USB?")
            sys.exit(1)
        print("\nWebREPL is now enabled. It starts automatically on boot from now on.")
        print("Resetting the Pico to activate it...")
        subprocess.run(["mpremote", "connect", "auto", "reset"])
    finally:
        os.unlink(temp_path)


if __name__ == "__main__":
    main()
